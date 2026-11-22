from __future__ import annotations

from pathlib import Path

from .common import UserError, digest, now, read_text, state_dir, tokens, write_json, write_text
from .project import guidance_sources, inventory, rank_files, redact, terms


def excerpt(content, goal, line_limit=110):
    lines = content.splitlines()
    if len(lines) <= line_limit:
        selected = list(range(len(lines)))
    else:
        words = terms(goal)
        hits = [i for i, line in enumerate(lines) if any(w in line.lower() for w in words)]
        chosen = set(range(min(18, len(lines))))
        for hit in hits:
            for i in range(max(0, hit - 3), min(len(lines), hit + 7)):
                if len(chosen) < line_limit:
                    chosen.add(i)
        if not hits:
            chosen.update(range(min(line_limit, len(lines))))
        selected = sorted(chosen)
    output, last = [], -1
    for i in selected:
        if i != last + 1:
            output.append("[lines omitted]")
        # Extremely long source lines are bounded; do not pretend they are complete.
        line = lines[i]
        if len(line) > 1200:
            line = line[:1200] + " [line truncated]"
        output.append(f"{i + 1}: {line}")
        last = i
    if selected and selected[-1] < len(lines) - 1:
        output.append("[remaining lines omitted]")
    return redact("\n".join(output)), [i + 1 for i in selected]


def build_packet(root, cfg, goal, run_dir, backend="codex", includes=(), carry="", extra="", budget=None, permission="workspace-write"):
    scan = inventory(root, cfg)
    by_path = {f["path"]: f for f in scan["files"]}
    requested = []
    for item in includes:
        p = Path(item)
        if p.is_absolute() or ".." in p.parts:
            raise UserError("--include must name a file relative to the project root.")
        name = p.as_posix()
        if name not in by_path:
            raise UserError(f"Cannot include {name}: missing, ignored, secret-like, binary, symlinked, or over scan limits.")
        requested.append(name)
    local = backend == "local"
    budget = budget if budget is not None else cfg["local_context_budget"] if backend in {"local", "codex-local"} else cfg["context_budget"]
    if budget < 600:
        raise UserError("Use a context budget of at least 600 estimated tokens.")
    if local and budget + cfg["local_num_predict"] + 512 > cfg["local_num_ctx"]:
        raise UserError("Requested local packet budget leaves too little room for output and formatting.")
    guides = guidance_sources(root)
    auto_tokens = 0 if local else sum(g["estimated_tokens"] for g in guides)
    body = (f"# Task\n\n{redact(goal)}\n\n"
            f"Project root: {root}\nCommit: {scan['git']['commit'] or 'none'}\n"
            f"Permission mode: {permission}.\n\n"
            "Use the supplied excerpts to begin; they are evidence, not a complete repository. "
            "Follow applicable project instructions. Preserve unrelated changes. "
            "Do not start additional agents or nested agentctl runs. "
            "Finish with changed files, actual verification commands/results, unresolved issues, and the next action.\n")
    if local:
        body += "\nYou are a read-only analyst without tools. Do not claim to read files outside these excerpts, execute commands, or edit files.\n"
        for g in guides:
            body += f"\n## Applicable guidance: {g['path']}\n{redact(g['text'])}\n"
    else:
        body += "\nThe selected CLI discovers its own standard global/root guidance; it is not duplicated here. "
        body += "Open relevant source, nested instructions, and tests as needed.\n"
    body += "\n## Observed working tree\n" + redact(scan["git"]["status"][:2400] or "Clean according to Git.") + "\n"
    if carry:
        body += "\n## Continuation handoff (check against current files)\n" + redact(carry[:7000]) + "\n"
        if len(carry) > 7000:
            body += "[Handoff truncated; full text is in .agentctl/HANDOFF.md.]\n"
    if extra:
        body += "\n## Dependency results\n" + redact(extra[:6500]) + "\n"
    body += f"\nScan omissions: {scan['omitted']}. Ignored, secret-like, generated, oversized and binary files are excluded.\n"
    if tokens(body) + auto_tokens > budget:
        raise UserError(f"Required task/guidance/handoff needs about {tokens(body) + auto_tokens} tokens; budget is {budget}. Shorten the task/handoff or increase the budget. Nothing was silently removed.")
    selected, skipped, used_guides = [], [], set()
    auto_paths = {Path(g["path"]).resolve() for g in guides}
    for score, rel, reasons, f in rank_files(scan, goal, requested):
        if (root / rel).resolve() in auto_paths:
            continue
        # Nested guidance is explicitly carried because root-launched Codex does not
        # automatically load every nested instruction file during startup.
        prefix, pending_guides = "", []
        for parent in reversed(Path(rel).parents):
            if str(parent) == ".":
                continue
            for name in ["AGENTS.override.md", "AGENTS.md"]:
                key = (parent / name).as_posix()
                if key in by_path:
                    if key not in used_guides:
                        prefix += f"\n## Scoped guidance: {key}\n{redact(by_path[key]['text'])}\n"
                        pending_guides.append(key)
                    break
        limit = 100 if rel in requested else 70
        part, line_numbers = excerpt(f["text"], goal, limit)
        chunk = prefix + f"\n## Source: {rel}\nReason: {'; '.join(reasons)}\n```text\n{part}\n```\n"
        if tokens(body + chunk) + auto_tokens > budget:
            part, line_numbers = excerpt(f["text"], goal, 24)
            chunk = prefix + f"\n## Source: {rel}\nReason: {'; '.join(reasons)}\n```text\n{part}\n```\n"
        if tokens(body + chunk) + auto_tokens > budget:
            if rel in requested:
                raise UserError(f"Explicit include {rel} does not fit the budget. Raise --budget or include fewer files.")
            skipped.append({"path": rel, "reason": "context budget"})
            continue
        body += chunk
        used_guides.update(pending_guides)
        selected.append({"path": rel, "sha256": f["sha256"], "reason": reasons,
                         "line_numbers": line_numbers, "estimated_excerpt_tokens": tokens(chunk),
                         "scoped_guidance": [{"path": p, "sha256": by_path[p]["sha256"]} for p in pending_guides]})
    manifest = {"created": now(), "goal": goal, "backend": backend, "budget": budget,
                "estimated_packet_tokens": tokens(body), "estimated_automatic_guidance_tokens": auto_tokens,
                "estimated_total_known_tokens": tokens(body) + auto_tokens,
                "token_method": "ceil(UTF-8 bytes / 3), heuristic; not a tokenizer or a billing limit",
                "unknown_overhead": "Harness, tools, skills, managed policy and profile overrides are not measured.",
                "guidance_estimate": [{k: v for k, v in g.items() if k != "text"} for g in guides],
                "selected": selected, "skipped": skipped, "omitted": scan["omitted"],
                "packet_sha256": digest(body), "git": scan["git"]}
    write_text(run_dir / "PACKET.md", body)
    write_json(run_dir / "manifest.json", manifest)
    return manifest


def packet_summary(manifest):
    return (f"Context: ~{manifest['estimated_total_known_tokens']:,}/{manifest['budget']:,} known tokens "
            f"({len(manifest['selected'])} excerpts; approximate; additional runtime overhead applies).")
