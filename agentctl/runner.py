from __future__ import annotations

import contextlib
import fnmatch
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time

from .common import Store, UserError, digest, local_lock, new_id, now, read_text, safe_terminal, state_dir, write_json, write_text
from .context import build_packet, packet_summary
from .local import Ollama
from .project import changed_since, guidance_sources, redact, snapshot
from .adapters import opencode_command


def stop_process(process, grace=3):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)


def stream_process(argv, cwd, input_path, folder, timeout, consume, stop=None, env=None, on_start=None, termination_grace=3):
    """Drain both pipes without blocking deadlines on readline or large output."""
    stop = stop or threading.Event()
    if stop.is_set():
        raise UserError("Run cancelled.")
    deadline = time.monotonic() + timeout
    total = 0
    with open(input_path, "rb") as prompt, open(folder / "events.jsonl", "wb") as output, open(folder / "stderr.log", "wb") as errors:
        try:
            proc = subprocess.Popen(argv, cwd=cwd, stdin=prompt, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True, env=env)
        except OSError as exc:
            raise UserError(f"Cannot launch {argv[0]}: {exc}") from exc
        pending = b""
        try:
            if on_start:
                on_start(proc.pid)
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
                selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map() or proc.poll() is None:
                    if stop.is_set():
                        raise UserError("Run cancelled.")
                    if time.monotonic() > deadline:
                        raise UserError(f"Run exceeded the {timeout}s time limit.")
                    for key, _ in selector.select(timeout=0.2):
                        data = os.read(key.fileobj.fileno(), 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(data)
                        if total > 16 * 1024 * 1024:
                            raise UserError("Run exceeded the 16 MiB combined output limit.")
                        if key.data == "stderr":
                            errors.write(data)
                            errors.flush()
                        else:
                            output.write(data)
                            output.flush()
                            pending += data
                            while b"\n" in pending:
                                line, pending = pending.split(b"\n", 1)
                                consume(line)
                if pending.strip():
                    consume(pending)
            return proc.wait(timeout=3)
        except BaseException:
            stop_process(proc, termination_grace)
            raise
        finally:
            proc.stdout.close()
            proc.stderr.close()


def codex_command(cfg, root, output_path, permission, model=None, backend="codex", output_schema=None):
    cmd = [cfg["codex_bin"], "--ask-for-approval", "never", "exec", "--json", "--color", "never",
           "--sandbox", permission, "-c", "features.multi_agent=false", "--cd", str(root),
           "--output-last-message", str(output_path)]
    if not (root / ".git").exists():
        cmd.append("--skip-git-repo-check")
    if backend == "codex-local":
        cmd += ["--oss", "--local-provider", "ollama", "--model", model or cfg["local_model"]]
    elif model:
        cmd += ["--model", model]
    if output_schema:
        cmd += ["--output-schema", str(output_schema)]
    return cmd + ["-"]


def prepare_run(root, cfg, goal, backend="codex", permission="workspace-write", includes=(), budget=None,
                carry="", extra="", parent=None, model=None, write_scope=None, managed_contract=None):
    if backend == "opencode" and permission != "workspace-write":
        raise UserError("OpenCode runs use CLI-managed permissions and require workspace-write exclusive scheduling.")
    if backend == "claude-sdk" and permission != "workspace-write":
        raise UserError("Managed feedback workflows run exclusively; individual agent scopes are defined in agents.json.")
    if backend == "local" and permission != "read-only":
        raise UserError("The local backend is a read-only analyst. Use codex for edits.")
    if backend == "codex-local" and permission != "read-only":
        raise UserError("v0.1 codex-local is an experimental read-only compatibility route. Use hosted codex for edits.")
    run_id = new_id()
    folder = state_dir(root) / "runs" / run_id
    folder.mkdir(parents=True, exist_ok=False)
    manifest = build_packet(root, cfg, goal, folder, backend, includes, carry, extra, budget, permission, managed_contract)
    record = {"id": run_id, "created": now(), "goal": goal, "objective": goal, "backend": backend, "permission": permission,
              "model": model, "status": "prepared", "parent": parent, "folder": str(folder),
              "root": str(root), "includes": list(includes), "budget": budget,
              "write_scope": write_scope or ["**"], "manifest": manifest,
              "before": snapshot(root, cfg), "commands_observed": [], "checks": [], "usage": {}, "thread_id": None}
    Store(root).save(record)
    return record


def execute_run(root, cfg, record, stop=None, handoff=True, quiet=False, observer=None):
    folder = Path(record["folder"])
    expected_root = state_dir(root).resolve()
    if not folder.resolve().is_relative_to(expected_root):
        raise UserError("Run artifacts must be inside this project's .agentctl directory.")
    packet = folder / "PACKET.md"
    if digest(read_text(packet)) != record["manifest"]["packet_sha256"]:
        raise UserError("The prepared packet changed. Prepare a fresh run instead of running modified state.")
    if record["status"] != "prepared":
        raise UserError("This run already started. Use resume to create a new continuation.")
    # Rebuild packets rather than launching evidence from an outdated snapshot.
    if snapshot(root, cfg)["fingerprint"] != record["before"]["fingerprint"]:
        raise UserError("Repository files changed since preparation. Use start/resume to prepare fresh context.")
    current_guides = [{k: v for k, v in g.items() if k != "text"} for g in guidance_sources(root)]
    if current_guides != record["manifest"]["guidance_estimate"]:
        raise UserError("Observed Codex guidance changed since preparation. Prepare a fresh context packet.")
    backend = record["backend"]
    if record.get("output_schema_file"):
        schema = Path(record["output_schema_file"])
        if (schema.is_symlink() or not schema.resolve().is_relative_to(folder.resolve())
                or not schema.is_file() or digest(read_text(schema)) != record.get("output_schema_sha256")):
            raise UserError("Managed peer output schema changed. Prepare a fresh run.")
    binary = cfg["opencode_bin"] if backend == "opencode" else cfg["codex_bin"]
    if backend not in {"local", "claude-sdk"} and not shutil.which(binary):
        raise UserError(f"CLI executable was not found: {binary}. Install/authenticate it or update Connections.")
    if backend == "claude-sdk":
        frozen = folder / "agents-config.json"
        source = Path(record.get("agents_config_source", ""))
        if (not record.get("agent_workflow") or not frozen.is_file() or frozen.is_symlink()
                or digest(read_text(frozen)) != record.get("frozen_agents_sha256")):
            raise UserError("Missing or modified managed agent configuration. Prepare a fresh agents run.")
        if not source.is_file() or digest(read_text(source)) != record.get("agents_config_sha256"):
            raise UserError("Project agent definitions changed since preparation. Prepare a fresh agents run.")
    if backend == "codex-local":
        if cfg["ollama_url"].rstrip("/") not in {"http://127.0.0.1:11434", "http://localhost:11434"}:
            raise UserError("The experimental codex-local route currently requires Ollama on port 11434. The plain local backend supports custom loopback ports.")
        data = Ollama(cfg).request("/api/show", {"model": record["model"] or cfg["local_model"]})
        if "tools" not in data.get("capabilities", []):
            raise UserError("This model does not advertise tool support. Use --backend local for bounded analysis.")
    store = Store(root)
    record.update(status="running", started=now())
    store.save(record)
    seen_completed, seen_failed, last_message, parse_errors = False, False, "", 0
    elapsed_start = time.monotonic()
    workflow_commands = {}

    def notify(event=None):
        store.save(record)
        if observer:
            observer(record, event)

    def started(pid):
        record["pid"] = pid
        notify({"type": "process.started", "pid": pid})

    def consume(raw):
        nonlocal seen_completed, seen_failed, last_message, parse_errors
        try:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError("not an event")
        except (ValueError, UnicodeError):
            parse_errors += 1
            return
        kind = event.get("type", "")
        if kind.startswith("agentctl."):
            actor = event.get("actor", "controller")
            if kind == "agentctl.project_tool.started" and actor == "controller":
                workflow_commands[event.get("call_id")] = {"argv": event.get("argv", []), "started": event.get("time")}
            elif kind == "agentctl.project_tool.completed" and actor == "controller":
                command = workflow_commands.get(event.get("call_id"), {})
                record.setdefault("checks", []).append({
                    "argv": command.get("argv", [event.get("tool", "project-tool")]),
                    "started": command.get("started"), "finished": event.get("time"),
                    "exit_code": event.get("exit_code") if event.get("exit_code") is not None else 124,
                    "contract_passed": event.get("status") == "passed", "error": event.get("error"),
                    "fingerprint": event.get("fingerprint"),
                    "source_changed_during_check": event.get("source_changed_during_check", True),
                    "log": str(Path(event.get("folder", str(folder))) / "events.jsonl"),
                    "origin": "managed_workflow",
                })
            elif kind in {"agentctl.agent.started", "agentctl.agent.completed", "agentctl.agent.failed"}:
                ident = event.get("call_id")
                if isinstance(ident, str):
                    activity = record.setdefault("agent_activity", {}).setdefault(ident, {"name": actor})
                    activity.update({k: event[k] for k in ("backend", "model", "parent", "started", "finished", "error", "task") if k in event})
                    activity["status"] = {"agentctl.agent.started": "running", "agentctl.agent.completed": "completed",
                                          "agentctl.agent.failed": "failed"}[kind]
                    activity["started" if kind == "agentctl.agent.started" else "finished"] = event.get("time")
            elif kind == "agentctl.agent.session":
                record.setdefault("agent_sessions", {})[actor] = event.get("session_id")
            elif kind == "agentctl.loop.finished":
                record.update(loop_outcome=event.get("outcome"), loop_iteration=event.get("iteration"),
                              reported_cost_usd=event.get("reported_cost_usd"))
                if event.get("winner"):
                    record["swarm_winner"] = event["winner"]
            if not quiet and kind in {"agentctl.agent.started", "agentctl.review.decision", "agentctl.revision.requested", "agentctl.loop.finished",
                                      "agentctl.swarm.round", "agentctl.candidate.dropped"}:
                detail = event.get("summary") or event.get("reason") or event.get("outcome", event.get("review", ""))
                print(safe_terminal(f"[{actor}] {kind.removeprefix('agentctl.')} " + str(detail)[:200]), flush=True)
            notify(event)
            return
        if backend == "opencode":
            record["thread_id"] = event.get("sessionID", record.get("thread_id"))
            part = event.get("part", {})
            part = part if isinstance(part, dict) else {}
            if kind == "step_start":
                seen_completed = False
                if record.get("output_schema_file"):
                    # Earlier tool steps can contain progress prose before the structured reply.
                    last_message = ""
            elif kind == "text":
                text = part.get("text", "")
                if isinstance(text, str):
                    last_message += text
            elif kind == "step_finish":
                seen_completed = part.get("reason") in ("stop", "end_turn", "completed")
                usage = part.get("tokens", {})
                usage = usage if isinstance(usage, dict) else {}
                for key in ("input", "output", "reasoning"):
                    value = usage.get(key)
                    if isinstance(value, (int, float)):
                        name = key + "_tokens"
                        record["usage"][name] = record["usage"].get(name, 0) + value
                cache = usage.get("cache", {})
                cache = cache if isinstance(cache, dict) else {}
                for key in ("read", "write"):
                    value = cache.get(key)
                    if isinstance(value, (int, float)):
                        name = "cache_" + key + "_tokens"
                        record["usage"][name] = record["usage"].get(name, 0) + value
                if isinstance(part.get("cost"), (int, float)):
                    record["reported_cost_usd"] = record.get("reported_cost_usd", 0) + part["cost"]
            elif kind == "tool_use":
                state = part.get("state", {})
                state = state if isinstance(state, dict) else {}
                tool_input = state.get("input", {})
                tool_input = tool_input if isinstance(tool_input, dict) else {}
                if part.get("tool") in {"bash", "shell"} and state.get("status") in {"completed", "error"}:
                    record["commands_observed"].append({"command": tool_input.get("command"),
                                                         "status": state.get("status"), "exit_code": None})
            elif kind in {"error", "session.error"}:
                seen_failed = True
                record["provider_error"] = event.get("error", event)
            notify(event)
            return
        if kind == "thread.started":
            record["thread_id"] = event.get("thread_id")
        elif kind == "turn.completed":
            seen_completed = True
            for key, value in event.get("usage", {}).items():
                if isinstance(value, (int, float)):
                    record["usage"][key] = record["usage"].get(key, 0) + value
        elif kind in {"turn.failed", "error"}:
            seen_failed = True
            record["provider_error"] = event.get("error", event.get("message", event))
        elif kind == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                last_message = item.get("text", "")
            elif item.get("type") == "command_execution":
                record["commands_observed"].append({"command": item.get("command"), "exit_code": item.get("exit_code"), "status": item.get("status")})
                if not quiet:
                    print(safe_terminal(f"[{record['id']}] command exit={item.get('exit_code')}: {str(item.get('command', ''))[:140]}"), flush=True)
        elif kind == "local.response":
            response = event.get("response", {})
            last_message = response.get("message", {}).get("content", "")
            record["usage"] = Ollama(cfg).usage(response)
            seen_completed = response.get("done") is True
            if response.get("done_reason") == "length":
                record["output_truncated"] = True
                seen_failed = True
        notify(event)

    try:
        if backend == "claude-sdk":
            request_path = folder / "feedback-request.json"
            agents = json.loads(read_text(folder / "agents-config.json"))
            timeout = agents["workflows"][record["agent_workflow"]]["timeout_seconds"]
            write_json(request_path, {"root": str(root), "folder": str(folder), "config": cfg, "agents": agents,
                                      "workflow": record["agent_workflow"], "goal": record["goal"],
                                      "context": read_text(packet), "demo": record.get("loop_demo", False)})
            cmd = [sys.executable, "-m", "agentctl.feedback_worker"]
            env = dict(os.environ)
            package_path = str(Path(__file__).parent.parent)
            env["PYTHONPATH"] = package_path + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            input_path = request_path
        elif backend == "local":
            request_path = folder / "local-request.json"
            request = {"config": cfg, "prompt": read_text(packet), "model": record["model"]}
            if record.get("output_schema_file"):
                request["schema"] = json.loads(read_text(schema))
            write_json(request_path, request)
            cmd = [sys.executable, "-m", "agentctl.local_worker"]
            env = dict(os.environ)
            package_path = str(Path(__file__).parent.parent)
            env["PYTHONPATH"] = package_path + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            input_path = request_path
        elif backend == "opencode":
            cmd = opencode_command(cfg, root, packet, record["permission"], record["model"])
            env = None
            input_path = os.devnull
        else:
            cmd = codex_command(cfg, root, folder / "answer.md", record["permission"], record["model"], backend,
                                record.get("output_schema_file"))
            env = None
            input_path = packet
        write_json(folder / "launch.json", {"argv": cmd, "cwd": str(root), "backend": backend})
        record["argv"] = cmd
        notify()
        with local_lock() if backend in {"local", "codex-local"} else contextlib.nullcontext():
            rc = stream_process(cmd, root, input_path, folder, timeout + 10 if backend == "claude-sdk" else cfg["timeout_seconds"],
                                consume, stop, env, started, 8 if backend == "claude-sdk" else 3)
        record["exit_code"] = rc
        record["status"] = "completed" if rc == 0 and seen_completed and not seen_failed else "failed"
        if record["status"] == "failed" and not record.get("provider_error"):
            record["error"] = "Provider did not report a clean completed turn. Inspect events.jsonl and stderr.log."
        if not (folder / "answer.md").exists():
            write_text(folder / "answer.md", last_message)
        record["answer_excerpt"] = redact(read_text(folder / "answer.md")[:6000])
    except KeyboardInterrupt:
        record.update(status="cancelled", error="Interrupted by user.")
    except (UserError, OSError) as exc:
        record.update(status="cancelled" if stop and stop.is_set() else "failed", error=str(exc))
    finally:
        record["finished"] = now()
        record["elapsed_seconds"] = round(time.monotonic() - elapsed_start, 3)
        record["unparsed_event_lines"] = parse_errors
        record["after"] = snapshot(root, cfg)
        record["changes"] = changed_since(record["before"], record["after"])
        changed = record["changes"]["added"] + record["changes"]["changed"] + record["changes"]["removed"]
        out_of_scope = [p for p in changed if not any(fnmatch.fnmatch(p, pat) or p.startswith(pat.rstrip("/") + "/") for pat in record["write_scope"])]
        if out_of_scope:
            record.update(status="scope_violation", out_of_scope=out_of_scope)
        if record["permission"] == "read-only" and changed:
            record.update(status="scope_violation", error="Observed source changes during a read-only run; inspect concurrent user activity or provider behavior.")
        store.save(record)
        if observer:
            observer(record, {"type": "process.finished", "status": record["status"]})
        if handoff:
            checkpoint(root, cfg, record)
    return record


def verification_state(record, current):
    checks = record.get("checks", [])
    if not checks:
        return "No explicit verification recorded. Model completion is not proof of task correctness."
    recent = {}
    for check in checks:
        recent[json.dumps(check["argv"])] = check
    if any(c.get("fingerprint") != current["fingerprint"] or c.get("source_changed_during_check") for c in recent.values()):
        return "Recorded checks are stale or source changed during verification. Rerun relevant checks."
    if any(c["exit_code"] != 0 or not c.get("contract_passed", True) for c in recent.values()):
        return "One or more recorded checks failed on the current scanned source."
    return "Recorded checks passed on the current scanned source; acceptance criteria still determine task completion."


def checkpoint(root, cfg, record=None, note="", next_action=""):
    record = record or Store(root).get()
    current = snapshot(root, cfg)
    baseline = record.get("after", record["before"])
    changes = changed_since(baseline, current)
    lines = ["# Session handoff", "", f"Updated: {now()}", f"Run: {record['id']}", f"Goal: {redact(record['goal'])}",
             f"Run status: {record['status']} (execution status, not a correctness verdict)",
             f"Branch: {current['git'].get('branch')}", f"Commit: {current['git'].get('commit')}",
             f"Verification: {verification_state(record, current)}", "", "## Current working tree", current["git"]["status"] or "Clean according to Git.",
             "", "## Changes after this run's last snapshot", json.dumps(changes, ensure_ascii=False),
             "", "## Result reported by the agent (not independently verified)", record.get("answer_excerpt", "No answer recorded.")[:3500],
             "", "## Explicit verification evidence"]
    lines += [f"- {c['argv']}: exit {c['exit_code']}; log: {c['log']}" for c in record.get("checks", [])[-6:]] or ["- None recorded with agentctl verify."]
    lines += ["", "## User notes", note or "No additional notes.", "", "## Next action",
              next_action or "Inspect the result and outstanding issues. If work remains, continue the smallest unfinished step and run relevant verification. If complete, report that; do not repeat completed actions.",
              "", f"Full artifacts: {record['folder']}", ""]
    folder = state_dir(root)
    write_text(folder / "HANDOFF.md", redact("\n".join(lines)))
    write_json(folder / "handoff.json", {"run_id": record["id"], "snapshot": current, "note": note, "next_action": next_action, "updated": now()})
    return folder / "HANDOFF.md"


def verify(root, cfg, run_id, argv, stop=None):
    if not argv:
        raise UserError("Supply a verification command after --.")
    record = Store(root).get(run_id)
    if record["status"] == "running":
        raise UserError("Wait for the run to finish before verifying.")
    folder = Path(record["folder"])
    path = folder / f"check-{new_id()}.log"
    before = snapshot(root, cfg)
    started = now()
    with open(path, "wb") as log:
        try:
            proc = subprocess.Popen(argv, cwd=root, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                deadline = time.monotonic() + cfg["timeout_seconds"]
                while proc.poll() is None:
                    if stop and stop.is_set():
                        stop_process(proc)
                        rc = 130
                        break
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(argv, cfg["timeout_seconds"])
                    try:
                        proc.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        continue
                else:
                    rc = proc.returncode
            except subprocess.TimeoutExpired:
                stop_process(proc)
                rc = 124
            except KeyboardInterrupt:
                stop_process(proc)
                rc = 130
        except OSError as exc:
            log.write(str(exc).encode())
            rc = 127
    after = snapshot(root, cfg)
    evidence = {"started": started, "finished": now(), "argv": argv, "exit_code": rc, "log": str(path),
                "fingerprint": after["fingerprint"], "source_changed_during_check": before["fingerprint"] != after["fingerprint"],
                "scope": "Only files included by the scanner are fingerprinted; exclusions are documented in the snapshot."}
    record.setdefault("checks", []).append(evidence)
    Store(root).save(record)
    checkpoint(root, cfg, record)
    return evidence


def print_run(record):
    print(packet_summary(record["manifest"]))
    print(f"Run {record['id']}: {record['status']} ({record['backend']})")
    print(f"Artifacts: {record['folder']}")
    if record.get("error"):
        print(safe_terminal(record["error"]))
    if record.get("usage"):
        print("Provider-reported usage: " + json.dumps(record["usage"]))
    if record.get("answer_excerpt"):
        print("\n" + safe_terminal(record["answer_excerpt"][:3000]))
