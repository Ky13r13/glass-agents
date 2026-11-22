from __future__ import annotations

import ast
from collections import Counter
import fnmatch
import json
import math
import os
from pathlib import Path
import posixpath
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


# Lexical definition/import extraction. Regex/ast only: no project code runs and
# no model is called. It is a ranking aid, not a type-resolved symbol index.
_JS = r"[A-Za-z_$][\w$]*"
_C_KEYWORDS = {"if", "for", "while", "switch", "return", "sizeof", "catch", "else", "do", "case", "new", "delete", "function"}
DEFINITION_PATTERNS = {
    (".js", ".jsx", ".ts", ".tsx"): [
        rf"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\s*\*?|class|interface|type|enum)\s+({_JS})",
        rf"^\s*(?:export\s+)?(?:const|let|var)\s+({_JS})\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|{_JS}\s*=>)",
        rf"^\s+(?:async\s+)?(?:static\s+)?({_JS})\s*\([^)]*\)\s*\{{"],
    (".go",): [r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)", r"^type\s+([A-Za-z_]\w*)"],
    (".rs",): [r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:fn|struct|enum|trait|type|mod)\s+([A-Za-z_]\w*)"],
    (".java",): [r"^\s*(?:(?:public|private|protected|static|final|abstract)\s+)*(?:class|interface|enum|record)\s+([A-Za-z_]\w*)",
                 r"^\s+(?:(?:public|private|protected|static|final|abstract|synchronized)\s+)+[\w<>\[\],.?\s]+\s([A-Za-z_]\w*)\s*\("],
    (".c", ".cc", ".cpp", ".h", ".hpp"): [r"^\s*(?:class|struct|enum(?:\s+class)?|namespace)\s+([A-Za-z_]\w*)",
                                         r"^[A-Za-z_][\w\s*&:<>,]*?[\s*&:]([A-Za-z_]\w*)\s*\([^;]*\)\s*(?:const\s*)?(?:noexcept\s*)?\{?\s*$"],
    (".sh",): [r"^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{"],
}
_SYMBOL_CACHE = {}


def _python_definitions(text):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    found = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append((node.name, node.lineno, node.end_lineno or node.lineno))
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        span = (item.lineno, item.end_lineno or item.lineno)
                        found += [(item.name, *span), (node.name + "." + item.name, *span)]
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                if isinstance(target, ast.Name) and target.id.isupper():
                    found.append((target.id, node.lineno, node.end_lineno or node.lineno))
    return found


def definitions(f):
    """Return [(name, start, end)] 1-based inclusive spans, cached by path and hash.

    Python spans come from the AST. Other languages (and unparsable Python) use
    line patterns; a span then ends before the next definition, at most 40 lines.
    """
    key = (f["path"], f["sha256"])
    if key not in _SYMBOL_CACHE:
        suffix, text = Path(f["path"]).suffix.lower(), f["text"]
        found = _python_definitions(text) if suffix == ".py" else None
        if found is None:
            patterns = ([r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)"] if suffix == ".py" else
                        next((v for k, v in DEFINITION_PATTERNS.items() if suffix in k), []))
            starts = sorted({(text.count("\n", 0, m.start()) + 1, m.group(1))
                             for pattern in patterns for m in re.finditer("(?m)" + pattern, text)
                             if m.group(1) not in _C_KEYWORDS})
            total = text.count("\n") + 1
            found = [(name, line, min(total, line + 39, *(s - 1 for s, _ in starts if s > line)))
                     for line, name in starts]
        if len(_SYMBOL_CACHE) > 20000:
            _SYMBOL_CACHE.clear()
        _SYMBOL_CACHE[key] = found[:2000]
    return _SYMBOL_CACHE[key]


def imports(f, known):
    """Resolve local import edges to scanned paths. External packages are ignored."""
    rel, text = f["path"], f["text"]
    suffix, folder = Path(rel).suffix.lower(), posixpath.dirname(rel)

    def first(*candidates):
        return next((c for c in (posixpath.normpath(x) for x in candidates) if c in known and c != rel), None)

    found = set()
    if suffix == ".py":
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            return found
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                base = folder
                for _ in range(max(0, node.level - 1)):
                    base = posixpath.dirname(base)
                module = (node.module or "").replace(".", "/")
                roots = [base] if node.level else ["", "src"]
                for r in roots:
                    stem = posixpath.join(r, module) if module else r
                    hit = first(stem + ".py", posixpath.join(stem, "__init__.py")) if module else None
                    if hit:
                        found.add(hit)
                    for alias in node.names:
                        sub = first(posixpath.join(stem, alias.name + ".py"))
                        if sub:
                            found.add(sub)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    stem = alias.name.replace(".", "/")
                    hit = first(stem + ".py", stem + "/__init__.py", "src/" + stem + ".py", "src/" + stem + "/__init__.py")
                    if hit:
                        found.add(hit)
    elif suffix in {".js", ".jsx", ".ts", ".tsx"}:
        for spec in re.findall(r"""(?:from\s+|require\(\s*|import\(\s*|import\s+)['"](\.{1,2}/[^'"]+)['"]""", text):
            stem = posixpath.join(folder, spec)
            hit = first(stem, *(stem + e for e in (".ts", ".tsx", ".js", ".jsx")),
                        *(stem + "/index" + e for e in (".ts", ".tsx", ".js", ".jsx")))
            if hit:
                found.add(hit)
    elif suffix in {".c", ".cc", ".cpp", ".h", ".hpp"}:
        for spec in re.findall(r'(?m)^\s*#\s*include\s+"([^"]+)"', text):
            hit = first(posixpath.join(folder, spec), spec, "include/" + spec, "src/" + spec)
            if not hit:
                matches = [k for k in known if k.endswith("/" + spec)]
                hit = matches[0] if len(matches) == 1 else None
            if hit:
                found.add(hit)
    return found


def goal_identifiers(goal):
    """Lower-case goal identifiers mapped to whether they look like code names."""
    result = {}
    for raw in re.findall(r"`?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*`?", goal):
        quoted, token = raw.startswith("`"), raw.strip("`")
        codey = quoted or "_" in token or "." in token or bool(re.search(r"[a-z][A-Z]", token))
        for name in {token, *token.split(".")}:
            low = name.lower()
            if len(low) >= 3 and low not in STOPWORDS:
                result[low] = result.get(low, False) or codey
    return result


def goal_symbol_spans(f, goal):
    """(start, end) spans of definitions in this file whose names appear in the goal."""
    wanted = goal_identifiers(goal)
    return sorted({(start, end) for name, start, end in definitions(f) if name.lower() in wanted})


def rank_files(scan, goal, includes=()):
    words = terms(goal)
    wanted = goal_identifiers(goal)
    exact = set(re.findall(r"[A-Za-z_]\w*", goal))
    by_path = {f["path"]: f for f in scan["files"]}
    defined = {}
    for f in scan["files"]:
        for name, *_ in definitions(f):
            if name.lower() in wanted:
                defined.setdefault(name.lower(), set()).add(f["path"])
    scored = {}
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
        # A definition of a named symbol is stronger evidence than a mention of it.
        hits = sorted({n for n, *_ in definitions(f) if n.lower() in defined and p in defined[n.lower()]})
        bonus, strong, weak = 0, [], []
        for symbol in hits:
            owners = len(defined[symbol.lower()])
            # Plain English words ("check", "run") often collide with method names; an
            # exact-case capitalized match ("Ollama" -> class Ollama) is a code name.
            if wanted[symbol.lower()] or (symbol[:1].isupper() and symbol in exact):
                bonus += 40 if owners <= 3 else 15
                strong.append(symbol)
            elif owners == 1:
                bonus += 4
                weak.append(symbol)
        if bonus:
            score += min(80, bonus)
            if strong:
                reasons.append("defines: " + ", ".join(strong[:5]))
            if weak:
                reasons.append("defines common word: " + ", ".join(weak[:5]))
        if Path(p).name.lower().startswith("readme"):
            score += 8
            reasons.append("project overview")
        if Path(p).name in {"pyproject.toml", "package.json", "CMakeLists.txt", "Cargo.toml", "Makefile"}:
            score += 5
            reasons.append("build/package manifest")
        scored[p] = [score, reasons]
    # One import hop from the strongest code matches. Widely imported modules get a
    # smaller boost because an edge to them says little about this particular task.
    known = set(by_path)
    graph = {p: imports(by_path[p], known) for p in known if Path(p).suffix.lower() in
             {".py", ".js", ".jsx", ".ts", ".tsx", ".c", ".cc", ".cpp", ".h", ".hpp"}}
    indegree = Counter(dep for deps in graph.values() for dep in deps)
    seeds = sorted((p for p in graph if scored[p][0] >= 12), key=lambda p: (-scored[p][0], p))[:3]
    boosts = {}
    for seed in seeds:
        base = min(15, max(3, scored[seed][0] // 4))
        neighbours = [(dep, dep, "imported by " + seed) for dep in graph[seed]]
        neighbours += [(src, seed, "imports " + seed) for src, deps in graph.items() if seed in deps]
        for target, imported, reason in neighbours:
            if target in seeds:
                continue
            value = max(1, int(base / (1 + math.log2(1 + indegree[imported]))))
            if value > boosts.get(target, (0, ""))[0]:
                boosts[target] = (value, reason)
    for target, (value, reason) in boosts.items():
        scored[target][0] += value
        scored[target][1].append(reason)
    ranked = []
    for p, (score, reasons) in scored.items():
        if p in includes:
            score += 10000
            reasons.insert(0, "explicitly included")
        if score:
            ranked.append((score, p, reasons, by_path[p]))
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
