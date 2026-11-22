from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

from . import __version__
from .common import Store, UserError, load_config, lock, new_id, now, project_root, read_text, require_init, safe_terminal, state_dir, write_json
from .context import packet_summary
from .local import Ollama, benchmark, doctor, serve, setup_model
from .project import changed_since, initialize, inventory, report, snapshot
from .runner import checkpoint, execute_run, prepare_run, print_run, verification_state, verify
from .team import make_plan, run_team, validate_plan
from .workflows import catalog, compile_workflow


def parser():
    p = argparse.ArgumentParser(prog="agentctl", description="Prepare focused Codex sessions and bounded local analysis workers.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-C", "--directory", default=".", help="Project directory (put before the command).")
    sub = p.add_subparsers(dest="command", required=True)
    from .agent_commands import add_commands
    add_commands(sub)
    gui = sub.add_parser("dashboard", help="Open the local agent dashboard with live CLI activity.")
    gui.add_argument("--port", type=int, default=8765)
    gui.add_argument("--no-browser", action="store_true")
    gui.add_argument("--demo", action="store_true", help="Use an isolated temporary project and synthetic CLI fixtures; no models.")
    init = sub.add_parser("init", help="Create minimal helpers; preserve existing project guidance.")
    init.add_argument("path", nargs="?")
    scan = sub.add_parser("inspect", help="Inspect project files and recommend helpers without model calls.")
    scan.add_argument("--json", action="store_true")
    sub.add_parser("doctor", help="Report hardware, Codex, and Ollama diagnostics.")
    for name in ["start", "context"]:
        s = sub.add_parser(name, help="Start a task." if name == "start" else "Prepare a context packet without calling a model.")
        s.add_argument("goal")
        add_run_flags(s)
    res = sub.add_parser("resume", help="Start a fresh compact continuation from the saved handoff.")
    res.add_argument("followup", nargs="?", default="Continue the next unfinished action in the handoff; if the task is complete, report that.")
    res.add_argument("--run", dest="run_id")
    add_run_flags(res, resume=True)
    run = sub.add_parser("run", help="Execute a previously prepared, unchanged context packet.")
    run.add_argument("id", nargs="?")
    status = sub.add_parser("status", help="List runs, or inspect a specific run and its evidence.")
    status.add_argument("id", nargs="?")
    status.add_argument("--json", action="store_true")
    cp = sub.add_parser("checkpoint", help="Save source state and your notes for a future session.")
    cp.add_argument("--run", dest="run_id")
    cp.add_argument("--note", default="")
    cp.add_argument("--next", dest="next_action", default="")
    v = sub.add_parser("verify", help="Run an explicit verification command and record its exit status.")
    v.add_argument("id")
    v.add_argument("argv", nargs=argparse.REMAINDER)
    pl = sub.add_parser("plan", help="Recommend a bounded task graph using transparent heuristics.")
    pl.add_argument("goal")
    pl.add_argument("--output")
    pl.add_argument("--local-readers", action="store_true")
    tm = sub.add_parser("team", help="Execute a task graph; readers may overlap, writes are serialized.")
    tm.add_argument("plan")
    tm.add_argument("--dry-run", action="store_true")
    wf = sub.add_parser("workflow", help="List, export or run a reusable workflow agent team.")
    ws = wf.add_subparsers(dest="workflow_command", required=True)
    ws.add_parser("list", help="Show built-in workflows without requiring a project.")
    for action in ("export", "run"):
        w = ws.add_parser(action)
        w.add_argument("name")
        w.add_argument("--goal", required=True)
        w.add_argument("--include", action="append", default=[])
        w.add_argument("--write-scope", action="append", default=[])
        w.add_argument("--output-dir", help="Draft output directory relative to the project.")
        w.add_argument("--local-readers", action="store_true", help="Use the local model for documentation inspection only.")
        if action == "run":
            w.add_argument("--dry-run", action="store_true")
    models = sub.add_parser("models", help="Manage and measure a local Ollama runtime.")
    ms = models.add_subparsers(dest="model_command", required=True)
    ms.add_parser("list")
    ms.add_parser("status")
    ms.add_parser("serve")
    setup = ms.add_parser("setup")
    setup.add_argument("--base")
    setup.add_argument("--model")
    bench = ms.add_parser("benchmark")
    bench.add_argument("--model")
    return p


def add_run_flags(p, resume=False):
    p.add_argument("--backend", choices=["codex", "opencode", "local", "codex-local", "claude-sdk"], default=None if resume else "codex")
    p.add_argument("--agent-workflow", default=None, help="Managed feedback workflow from project agents.json (Claude SDK backend).")
    p.add_argument("--agents-config", help="Project-relative managed agent configuration JSON.")
    p.add_argument("--model", help="Explicit model override; hosted default uses your Codex configuration.")
    p.add_argument("--include", action="append", default=[], help="Include a particular source file (repeatable).")
    p.add_argument("--budget", type=int, help="Approximate packet plus observed instruction budget.")
    p.add_argument("--read-only", action="store_true")
    p.add_argument("--prepare-only", action="store_true", help="Write packets without launching models.")
    p.add_argument("--interactive", action="store_true", help="Open the Codex terminal UI; usage and completion events are unavailable in this mode.")


def interactive(root, cfg, record):
    if record["backend"] != "codex":
        raise UserError("Interactive mode currently supports hosted Codex only.")
    prompt = f"Read {record['folder']}/PACKET.md and carry out the task it describes. Do not launch nested agents."
    cmd = [cfg["codex_bin"], "--cd", str(root), "--sandbox", record["permission"], "-c", "features.multi_agent=false"]
    if record.get("model"):
        cmd += ["--model", record["model"]]
    cmd += [prompt]
    record.update(status="running", started=now())
    Store(root).save(record)
    try:
        rc = subprocess.call(cmd, cwd=root)
        record.update(status="interactive_closed" if rc == 0 else "failed", exit_code=rc)
    except KeyboardInterrupt:
        record.update(status="cancelled", error="Interactive session interrupted.")
    except OSError as exc:
        record.update(status="failed", error=str(exc))
    finally:
        record["after"] = snapshot(root, cfg)
        record["changes"] = changed_since(record["before"], record["after"])
        record["finished"] = now()
        record["answer_excerpt"] = "Interactive Codex session ended. agentctl cannot observe its final answer, usage, or tests; record your own notes and run agentctl verify."
        Store(root).save(record)
        checkpoint(root, cfg, record)
    return record


def print_json(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


def dispatch(args):
    root = project_root(args.path if args.command == "init" and args.path else args.directory)
    if args.command == "agents" and args.agents_command == "init":
        root = Path(args.directory).expanduser().resolve()
        if not root.is_dir():
            raise UserError("Agent project directory does not exist.")
    cfg = load_config(root)
    cmd = args.command
    if cmd == "agents":
        from .agent_commands import dispatch_agents
        return dispatch_agents(root, cfg, args)
    if cmd == "dashboard":
        from .dashboard import serve_dashboard
        return serve_dashboard(root, args.port, not args.no_browser, args.demo)
    if cmd == "init":
        print_json(initialize(root))
        print("Ready. Try: agentctl context \"your task\"")
        return 0
    if cmd == "inspect":
        r = report(inventory(root, cfg))
        if args.json:
            print_json(r)
        else:
            print(f"{r['files_scanned']} text files scanned. Languages: {r['languages']}")
            for h in r["helpers"]:
                print(safe_terminal(f"{h['action']}: {h['file']} — {h['reason']}"))
            print("Command candidates (not executed):")
            for c in r["commands"]:
                print(safe_terminal(f"  {c['command']} [cwd: {c['cwd']}; source: {c['source']} ]"))
            print(f"Omissions: {r['omitted']}")
        return 0
    if cmd == "doctor":
        print_json(doctor(cfg))
        return 0
    if cmd == "workflow" and args.workflow_command == "list":
        print_json(catalog())
        return 0
    if cmd == "models":
        action = args.model_command
        if action == "serve":
            return serve(cfg)
        if action == "setup":
            print_json(setup_model(cfg, args.base, args.model))
        elif action == "list":
            print_json(Ollama(cfg).request("/api/tags"))
        elif action == "status":
            print_json(Ollama(cfg).request("/api/ps"))
        elif action == "benchmark":
            result = benchmark(cfg, args.model)
            base = state_dir(root) if (state_dir(root) / "config.toml").exists() else Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "agentctl"
            out = base / "benchmarks" / (new_id() + ".json")
            write_json(out, result)
            print_json(result)
            print(f"Saved: {out}")
            return 0 if result["basic_analysis_passed"] else 1
        return 0
    require_init(root)
    store = Store(root)
    if cmd == "status":
        if args.id:
            rec = store.get(args.id)
            current = snapshot(root, cfg)
            if args.json:
                print_json(rec)
            else:
                print_run(rec)
                print(verification_state(rec, current))
                print("Changes since run: " + json.dumps(changed_since(rec.get("after", rec["before"]), current)))
        else:
            records = store.list()
            if args.json:
                print_json([{k: r.get(k) for k in ["id", "created", "goal", "status", "backend", "usage"]} for r in records])
            else:
                for r in records:
                    print(safe_terminal(f"{r['id']}  {r['status']:18} {r['backend']:11} {r['goal'][:75]}"))
                if not records:
                    print("No runs yet.")
        return 0
    with lock(state_dir(root) / "operation.lock"):
        if cmd == "workflow":
            plan = compile_workflow(args.name, args.goal, args.include, args.write_scope, args.output_dir, args.local_readers)
            path = state_dir(root) / "plans" / (new_id() + "-" + args.name + ".json")
            write_json(path, plan)
            if args.workflow_command == "export" or args.dry_run:
                print_json(plan)
                print(f"Saved: {path}\nNo models launched. Execute with: agentctl team {shlex.quote(str(path))}")
                return 0
            result = run_team(root, cfg, plan)
            result["human_review_required"] = True
            print_json(result)
            print("Inspect the final reviewer findings and draft artifacts before accepting or delivering this work.")
            return 0 if result["successful"] else 1
        if cmd in {"start", "context", "resume"}:
            carry, extra, parent = "", "", None
            if cmd == "resume":
                handoff_path = state_dir(root) / "handoff.json"
                saved = json.loads(read_text(handoff_path)) if handoff_path.exists() else {}
                previous = store.get(args.run_id or saved.get("run_id"))
                parent = previous["id"]
                objective = previous.get("objective", previous["goal"])
                goal = f"Original task: {objective}\nNext instruction: {args.followup}"
                backend = args.backend or previous["backend"]
                if saved.get("run_id") == parent:
                    carry = read_text(state_dir(root) / "HANDOFF.md")
                    before = saved["snapshot"]
                else:
                    carry = f"Run {parent}: {previous['status']}\n{previous.get('answer_excerpt', '')}\nArtifacts: {previous['folder']}"
                    before = previous.get("after", previous["before"])
                extra = "Changes since prior snapshot: " + json.dumps(changed_since(before, snapshot(root, cfg)))
            else:
                goal, backend = args.goal, args.backend
            permission = previous["permission"] if cmd == "resume" else "workspace-write"
            if args.read_only or backend in {"local", "codex-local"}:
                permission = "read-only"
            model = args.model or (previous.get("model") if cmd == "resume" and backend == previous["backend"] else None)
            if backend == "claude-sdk":
                if permission != "workspace-write" or args.interactive:
                    raise UserError("Managed feedback uses exclusive scheduling and project-defined agent scopes; use agents.json to configure readers and writers.")
                from .agent_config import prepare_feedback
                workflow = args.agent_workflow or (previous.get("agent_workflow") if cmd == "resume" else None) or "default"
                agent_config = args.agents_config or (previous.get("agents_config_source") if cmd == "resume" and previous["backend"] == "claude-sdk" else None)
                rec = prepare_feedback(root, cfg, goal, workflow, agent_config, args.include,
                                       carry=carry, extra=extra, parent=parent, model=model, budget=args.budget)
            else:
                rec = prepare_run(root, cfg, goal, backend, permission, args.include, args.budget, carry, extra, parent, model)
            if cmd == "resume":
                rec["objective"] = objective
                store.save(rec)
            print(packet_summary(rec["manifest"]), flush=True)
            if cmd == "context" or args.prepare_only:
                print_run(rec)
                print(f"To execute: agentctl run {rec['id']}")
                return 0
            rec = interactive(root, cfg, rec) if args.interactive else execute_run(root, cfg, rec)
            print_run(rec)
            return 0 if rec["status"] in {"completed", "interactive_closed"} and (rec["backend"] != "claude-sdk" or rec.get("loop_outcome") == "accepted") else 1
        if cmd == "run":
            rec = execute_run(root, cfg, store.get(args.id))
            print_run(rec)
            return 0 if rec["status"] == "completed" and (rec["backend"] != "claude-sdk" or rec.get("loop_outcome") == "accepted") else 1
        if cmd == "checkpoint":
            print(checkpoint(root, cfg, store.get(args.run_id), args.note, args.next_action))
            return 0
        if cmd == "verify":
            argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            result = verify(root, cfg, args.id, argv)
            print_json(result)
            return result["exit_code"] if result["exit_code"] in range(1, 126) else 0 if result["exit_code"] == 0 else 1
        if cmd == "plan":
            plan = make_plan(root, cfg, args.goal, args.local_readers)
            path = Path(args.output).expanduser().resolve() if args.output else state_dir(root) / "plans" / (new_id() + ".json")
            if path.exists():
                raise UserError("Plan output already exists; choose another --output path.")
            write_json(path, plan)
            print_json(plan)
            print(f"Saved: {path}\nExecute with: agentctl team {shlex.quote(str(path))}")
            return 0
        if cmd == "team":
            path = Path(args.plan).expanduser()
            if not path.is_absolute():
                path = root / path
            plan = validate_plan(json.loads(read_text(path)))
            if args.dry_run:
                print_json(plan)
                print(f"Valid graph. Up to {cfg['max_workers']} readers; one writer or local request at a time. No jobs launched.")
                return 0
            result = run_team(root, cfg, plan)
            print_json(result)
            return 0 if result["successful"] else 1
    return 0


def main(argv=None):
    try:
        return dispatch(parser().parse_args(argv))
    except (UserError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(safe_terminal(f"agentctl: {exc}"), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
