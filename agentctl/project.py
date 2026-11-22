from __future__ import annotations

from collections import Counter
import fnmatch
import json
import os
from pathlib import Path
import re
import tomllib

from .common import CONFIG_TEXT, UserError, digest, load_config, read_text, run_command, state_dir, tokens, write_json, write_text


SKIP_DIRS = {".git", ".agentctl", ".venv", "venv", "node_modules", "vendor", "dist", "build", "target", "__pycache__", ".pytest_cache", ".mypy_cache", ".next", ".cache"}
SKIP_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "poetry.lock", "Cargo.lock", "composer.lock"}
SOURCE_EXT = {".py", ".cpp", ".cc", ".c", ".h", ".hpp", ".rs", ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".sh", ".md", ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".txt", ".html", ".css", ".sql", ".csv", ".tsv"}
LANGUAGES = {".py": "Python", ".cpp": "C++", ".hpp": "C++", ".cc": "C++", ".c": "C", ".h": "C/C++", ".rs": "Rust", ".js": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".go": "Go", ".java": "Java"}
STOPWORDS = {"the", "and", "for", "with", "this", "that", "from", "into", "add", "implement", "create", "fix", "please", "project", "code", "need", "using", "what", "how", "all"}


def secret_path(rel):
    p = Path(rel)
    parts = {x.lower() for x in p.parts}
    name = p.name.lower()
    return (name.startswith(".env") or name.startswith("id_rsa") or name.startswith("id_ed25519")
            or p.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".keystore"}
            or bool(parts & {".ssh", ".aws", ".azure", ".gnupg", "secrets", "credentials"})
            or name in {"auth.json", "credentials.json", ".netrc", ".npmrc", ".pypirc"})


def redact(text):
    text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----", "[REDACTED PRIVATE KEY]", text)
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b", "[REDACTED TOKEN]", text)
    return re.sub(r'''(?im)((?:api[_-]?key|access[_-]?token|password|secret)\s*[=:]\s*)["']?[^\s"',;]+''', r"\1[REDACTED]", text)


def allowed(root, rel, patterns):
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        return False
    if any(part in SKIP_DIRS for part in p.parts) or p.name in SKIP_FILES or secret_path(rel):
        return False
    full = root / p
    # Reject symlinks at every level, including links whose target is inside the repo.
    if any((root.joinpath(*p.parts[:i])).is_symlink() for i in range(1, len(p.parts) + 1)):
        return False
    return not any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(p.name, pat) for pat in patterns)


def git_state(root):
    result = run_command(["git", "rev-parse", "HEAD"], root)
    if not result or result.returncode:
        return {"commit": None, "branch": None, "status": "No Git commit found."}
    branch = run_command(["git", "branch", "--show-current"], root)
    status = run_command(["git", "status", "--short", "--untracked-files=normal"], root)
    return {"commit": result.stdout.decode().strip(),
            "branch": branch.stdout.decode().strip() if branch else None,
            "status": status.stdout.decode(errors="replace").strip() if status else "unknown"}


def inventory(root, cfg=None):
    cfg = cfg or load_config(root)
    ignore = root / ".agentctlignore"
    patterns = [x.strip() for x in read_text(ignore).splitlines() if x.strip() and not x.startswith("#")] if ignore.is_file() else []
    git = run_command(["git", "ls-files", "-c", "-o", "--exclude-standard", "-z"], root)
    if git and git.returncode == 0:
        candidates = sorted(set(git.stdout.decode(errors="replace").split("\0")) - {""})
    else:
        candidates = []
        for folder, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (Path(folder) / d).is_symlink())
            candidates.extend((Path(folder) / n).relative_to(root).as_posix() for n in sorted(names))
            if len(candidates) > cfg["max_files"] * 3:
                break
    files, omitted, total = [], Counter(), 0
    for index, rel in enumerate(candidates):
        if len(files) >= cfg["max_files"]:
            omitted["file limit"] += len(candidates) - index
            break
        if not allowed(root, rel, patterns):
            omitted["excluded"] += 1
            continue
        p = root / rel
        try:
            if not p.is_file():
                continue
            size = p.stat().st_size
            if size > cfg["max_file_bytes"]:
                omitted["oversize"] += 1
                continue
            if total + size > cfg["max_scan_bytes"]:
                omitted["scan byte limit"] += 1
                continue
            raw = p.read_bytes()
            if b"\0" in raw or (p.suffix.lower() not in SOURCE_EXT and p.name not in {"Makefile", "Dockerfile", "justfile", ".gitignore"}):
                omitted["binary or unsupported"] += 1
                continue
            content = raw.decode("utf-8")
        except (OSError, UnicodeError):
            omitted["unreadable"] += 1
            continue
        total += len(raw)
        files.append({"path": rel, "bytes": len(raw), "sha256": digest(raw), "text": content})
    return {"files": files, "omitted": dict(omitted), "scanned_bytes": total, "git": git_state(root)}


def command_candidates(scan):
    files = {f["path"]: f["text"] for f in scan["files"]}
    found = []
    for name, content in files.items():
        if Path(name).name == "package.json":
            try:
                for script in json.loads(content).get("scripts", {}):
                    if script in {"test", "lint", "build", "typecheck"}:
                        found.append({"cwd": str(Path(name).parent), "command": f"npm run {script}", "source": name})
            except (ValueError, AttributeError):
                pass
        if Path(name).name in {"Makefile", "justfile"}:
            for target in re.findall(r"(?m)^([A-Za-z][A-Za-z0-9_-]*)\s*:", content):
                if target in {"test", "check", "lint", "build"}:
                    found.append({"cwd": str(Path(name).parent), "command": f"{'make' if Path(name).name == 'Makefile' else 'just'} {target}", "source": name})
        if Path(name).name == "Cargo.toml":
            found.append({"cwd": str(Path(name).parent), "command": "cargo test", "source": name})
    if any("pytest" in x["text"] for x in scan["files"] if Path(x["path"]).name in {"pyproject.toml", "pytest.ini", "requirements-dev.txt"}):
        found.append({"cwd": ".", "command": "python3 -m pytest", "source": "Python test configuration"})
    elif any("unittest" in f["text"] for f in scan["files"] if Path(f["path"]).name.startswith("test")):
        found.append({"cwd": ".", "command": "python3 -m unittest discover -s tests", "source": "unittest imports (verify discovery directory)"})
    return found


def report(scan):
    paths = [x["path"] for x in scan["files"]]
    languages = Counter(LANGUAGES[Path(p).suffix] for p in paths if Path(p).suffix in LANGUAGES)
    docs = [p for p in paths if p.lower().endswith(".md")]
    architecture = [p for p in docs if any(k in p.lower() for k in ("architecture", "design", "dataflow", "data-flow"))]
    root_guidance = next((p for p in ["AGENTS.override.md", "AGENTS.md"] if p in paths), None)
    recommendations = [{"file": root_guidance or "AGENTS.md", "action": "reuse" if root_guidance else "create",
                        "reason": "A short entry point for project rules and command references."}]
    recommendations.append({"file": ".agentctl/HANDOFF.md", "action": "maintain", "reason": "Carry observed state and explicit next actions between sessions."})
    if architecture:
        recommendations.append({"file": architecture[0], "action": "reuse", "reason": "Existing architecture/design document found."})
    elif len(languages) > 1 or len(paths) > 70:
        recommendations.append({"file": "docs/ARCHITECTURE.md", "action": "recommend", "reason": "Multiple languages or a larger repository; explain component boundaries after inspecting source."})
    recommendations.append({"file": "docs/DECISIONS.md", "action": "only when needed", "reason": "Record consequential decisions as they occur; no empty file is generated."})
    return {"files_scanned": len(paths), "languages": dict(languages), "documentation": docs,
            "commands": command_candidates(scan), "helpers": recommendations, "omitted": scan["omitted"], "git": scan["git"]}


def map_text(scan):
    r = report(scan)
    lines = ["# Repository map", "", "Generated from filenames and manifests; not a verified architectural interpretation.", "",
             f"Commit: {r['git']['commit'] or 'none'}", f"Files scanned: {r['files_scanned']}", f"Languages: {r['languages']}", "", "## Command candidates (not executed)"]
    lines += [f"- `{c['command']}` in `{c['cwd']}` — source: `{c['source']}`" for c in r["commands"]] or ["- No commands inferred. Read the project's README."]
    lines += ["", "## Documentation"] + [f"- `{p}`" for p in r["documentation"][:40]]
    lines += ["", "## Source locations"] + [f"- `{f['path']}`" for f in scan["files"][:100]]
    lines += ["", f"Omissions: {r['omitted']}", ""]
    return "\n".join(lines)


def initialize(root):
    cfg = load_config(root)
    scan = inventory(root, cfg)
    folder = state_dir(root)
    folder.mkdir(mode=0o700, exist_ok=True)
    for name, content in {"config.toml": CONFIG_TEXT, ".gitignore": "*\n",
                          "HANDOFF.md": "# Handoff\n\nNo task has been run. Use `agentctl start` to begin.\n"}.items():
        if not (folder / name).exists():
            write_text(folder / name, content)
    created_guidance = False
    if not (root / "AGENTS.md").exists() and not (root / "AGENTS.override.md").exists():
        commands = "\n".join(f"- `{x['command']}` in `{x['cwd']}` (inferred from `{x['source']}`; verify before use)." for x in command_candidates(scan))
        content = ("# Project guidance\n\n"
                   "Read the README and relevant source before changing behavior.\n"
                   "Keep changes within the current task and preserve unrelated user work.\n"
                   "Use `.agentctl/REPO_MAP.md` as a navigation aid when it exists.\n"
                   "When resuming a task, read `.agentctl/HANDOFF.md` and confirm its claims against current files.\n"
                   "Read more detailed documents only when relevant. Report what was actually verified.\n"
                   "Follow applicable nested AGENTS.md instructions when editing their directories.\n\n"
                   "## Command candidates\n\n" + (commands or "No commands inferred; consult project documentation.") + "\n")
        write_text(root / "AGENTS.md", content)
        created_guidance = True
    write_text(folder / "REPO_MAP.md", map_text(inventory(root, cfg)))
    return {"root": str(root), "created_agents_md": created_guidance, "recommendations": report(scan)["helpers"]}


def terms(goal):
    return set(re.findall(r"[a-zA-Z][a-zA-Z0-9_]{2,}", goal.lower())) - STOPWORDS


def rank_files(scan, goal, includes=()):
    words = terms(goal)
    ranked = []
    for f in scan["files"]:
        p = f["path"]
        name = p.lower()
        reasons, score = [], 0
        matched = sorted(w for w in words if w in name)
        if matched:
            score += 12 * len(matched)
            reasons.append("path matches: " + ", ".join(matched[:5]))
        matches = sum(min(3, f["text"].lower().count(w)) for w in words)
        if matches:
            score += matches
            reasons.append("content matches task terms")
        if Path(p).name.lower().startswith("readme"):
            score += 8
            reasons.append("project overview")
        if Path(p).name in {"pyproject.toml", "package.json", "CMakeLists.txt", "Cargo.toml", "Makefile"}:
            score += 5
            reasons.append("build/package manifest")
        if p in includes:
            score += 10000
            reasons.insert(0, "explicitly included")
        if score:
            ranked.append((score, p, reasons, f))
    return sorted(ranked, key=lambda x: (-x[0], x[1]))


def guidance_sources(root):
    # Estimates standard discovery from user config and the launch directory.
    # Profiles, managed policy, skills, and harness text are not introspectable here.
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    merged = {}
    for cfg_path in [codex_home / "config.toml", root / ".codex" / "config.toml"]:
        try:
            merged.update(tomllib.loads(read_text(cfg_path)))
        except (OSError, tomllib.TOMLDecodeError):
            pass
    fallbacks = merged.get("project_doc_fallback_filenames", [])
    if not isinstance(fallbacks, list):
        fallbacks = []
    names = ["AGENTS.override.md", "AGENTS.md"] + [p for p in fallbacks if isinstance(p, str) and Path(p).name == p]
    roots = [codex_home]
    p = run_command(["git", "rev-parse", "--show-toplevel"], root)
    git_root = Path(p.stdout.decode().strip()).resolve() if p and p.returncode == 0 else root
    if root.is_relative_to(git_root):
        parts = root.relative_to(git_root).parts
        roots += [git_root.joinpath(*parts[:i]) for i in range(len(parts) + 1)]
    else:
        roots += [root]
    result = []
    for index, folder in enumerate(roots):
        for name in names[:2] if index == 0 else names:
            path = folder / name
            try:
                if path.is_symlink() or path.stat().st_size > 1048576:
                    continue
                content = read_text(path)
            except OSError:
                continue
            if content.strip():
                result.append({"path": str(path), "sha256": digest(content), "estimated_tokens": tokens(content), "text": content})
                break
    return result


def snapshot(root, cfg):
    scan = inventory(root, cfg)
    hashes = {x["path"]: x["sha256"] for x in scan["files"]}
    return {"git": scan["git"], "files": hashes, "fingerprint": digest(json.dumps(hashes, sort_keys=True)), "omitted": scan["omitted"]}


def changed_since(before, after):
    old, new = before.get("files", {}), after.get("files", {})
    return {"added": sorted(set(new) - set(old)), "removed": sorted(set(old) - set(new)),
            "changed": sorted(p for p in set(old) & set(new) if old[p] != new[p]),
            "commit_changed": before.get("git", {}).get("commit") != after.get("git", {}).get("commit")}
