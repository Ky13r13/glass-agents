"""Commands for attaching portable, managed agent definitions to any project."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import time

from .agent_config import initialize_agents, load_agents, prepare_feedback, run_succeeded
from .agent_runtime import read_mail, send_mail
from .common import Store, UserError, lock, read_text, require_init, safe_terminal, state_dir
from .project import initialize
from .runner import execute_run, print_run


def add_commands(sub):
    parser = sub.add_parser("agents", help="Project-local agents, tool contracts, delegation, and feedback loops.")
    commands = parser.add_subparsers(dest="agents_command", required=True)
    commands.add_parser("init", help="Create portable project agent definitions, preserving existing files.")
    for name in ("list", "validate"):
        p = commands.add_parser(name)
        p.add_argument("--config", help="Agent definition JSON inside this project.")
    run = commands.add_parser("run", help="Run worker/check/reviewer cycles until accepted, rejected, or bounded.")
    run.add_argument("goal")
    run.add_argument("--workflow", default="default")
    run.add_argument("--config")
    run.add_argument("--include", action="append", default=[])
    run.add_argument("--model", help="Override models for Claude SDK agents in this run.")
    run.add_argument("--replicas", type=int, help="Swarm workflows: copies of each candidate agent (1-10).")
    run.add_argument("--rounds", type=int, help="Swarm workflows: maximum feedback rounds (1-20).")
    run.add_argument("--prepare-only", action="store_true")
    ask = commands.add_parser("ask", help="Give one named agent a task, optionally narrowing its folder or permissions.")
    ask.add_argument("agent", help="Agent name from agents.json.")
    ask.add_argument("task")
    ask.add_argument("--in", dest="folder", help="Project subfolder to work in; narrows the agent's write scope to FOLDER/**.")
    ask.add_argument("--scope", action="append", default=[], help="Narrow the write scope to this path or glob (repeatable).")
    ask.add_argument("--read-only", action="store_true", help="Remove all write access for this task.")
    ask.add_argument("--check", action="append", default=[], help="Configured project tool to run afterwards as a required check (repeatable).")
    ask.add_argument("--config")
    ask.add_argument("--include", action="append", default=[])
    ask.add_argument("--model", help="Override the model when the agent uses the Claude SDK.")
    ask.add_argument("--prepare-only", action="store_true")
    apply = commands.add_parser("apply", help="Apply a finished swarm's winning (or chosen) candidate to this checkout and rerun checks.")
    apply.add_argument("run_id")
    apply.add_argument("--candidate", help="Apply this candidate's final patch instead of the winner.")
    apply.add_argument("--allow-unaccepted", action="store_true", help="Permit applying a candidate the swarm did not accept.")
    apply.add_argument("--no-checks", action="store_true", help="Skip rerunning required checks after applying.")
    demo = commands.add_parser("demo", help="Offline scripted agents exercise real delegation, checks and revision.")
    demo.add_argument("--output", help="New directory in which to keep the demo; otherwise use a temporary project.")
    demo.add_argument("--swarm", action="store_true", help="Run three scripted candidates in isolated worktrees with a scripted critic.")
    messages = commands.add_parser("messages", help="Read a managed run's agent mailbox.")
    messages.add_argument("run_id")
    messages.add_argument("--to")
    message = commands.add_parser("message", help="Send operator feedback to a named agent; no model call.")
    message.add_argument("run_id")
    message.add_argument("--to", required=True)
    message.add_argument("--text", required=True)
    events = commands.add_parser("events", help="Read a managed run's actual event stream.")
    events.add_argument("run_id")
    events.add_argument("--follow", action="store_true")


def output(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


def record_for(root, run_id):
    record = Store(root).get(run_id)
    if record["backend"] != "claude-sdk":
        raise UserError("This command requires a managed feedback run.")
    if not Path(record["folder"]).resolve().is_relative_to(state_dir(root).resolve()):
        raise UserError("Invalid run artifact path.")
    return record


def run_demo(output_path=None, swarm=False):
    from .feedback_demo import initialize_demo, initialize_swarm_demo
    from .common import load_config
    temporary = None
    if output_path:
        root = Path(output_path).expanduser().resolve()
        if root.exists():
            raise UserError("Demo output must be a new directory.")
    else:
        temporary = tempfile.TemporaryDirectory(prefix="agentctl-feedback-demo-")
        root = Path(temporary.name) / "demo-project"
    try:
        if swarm:
            initialize_swarm_demo(root)
            goal = "Make note.txt pass the configured check; candidates compete and a critic judges."
            notice = ("Offline swarm demo: scripted candidates in real Git worktrees, real checks, a scripted critic, "
                      "feedback rounds and integration. No models or external services.")
        else:
            initialize_demo(root)
            goal = "Revise note.txt until the configured check passes, with an independent review."
            notice = "Offline demo: scripted agent fixtures; real project tools, messages, delegation and revision. No models or external services."
        cfg = load_config(root)
        record = prepare_feedback(root, cfg, goal, demo="swarm" if swarm else True)
        print(notice, flush=True)
        with lock(state_dir(root) / "operation.lock"):
            record = execute_run(root, cfg, record)
        print_run(record)
        summary = {"outcome": record.get("loop_outcome"), "iterations": record.get("loop_iteration"),
                   "kept_project": str(root) if output_path else None}
        if swarm:
            summary.update(winner=record.get("swarm_winner"), note_txt=(root / "note.txt").read_text())
        else:
            summary["messages"] = read_mail(record["folder"])
        output(summary)
        return 0 if record["status"] == "completed" and record.get("loop_outcome") == "accepted" else 1
    finally:
        if temporary:
            temporary.cleanup()


def apply_candidate(root, cfg, record, args):
    import asyncio
    from .agent_config import validate_config
    from .common import digest, now, write_json
    from .runner import checkpoint
    from .swarm import integrate
    if record.get("workflow_type") != "swarm":
        raise UserError("agents apply needs a swarm workflow run.")
    if record["status"] in {"prepared", "running"}:
        raise UserError("Wait for the swarm run to finish before applying a candidate.")
    folder = Path(record["folder"])
    state_path = folder / "loop" / "state.json"
    frozen = folder / "agents-config.json"
    if state_path.is_symlink() or frozen.is_symlink() or digest(read_text(frozen)) != record.get("frozen_agents_sha256"):
        raise UserError("Swarm artifacts were modified; refusing to apply.")
    state = json.loads(read_text(state_path))
    if (state.get("applied") or {}).get("applied"):
        raise UserError("A candidate from this swarm was already applied.")
    winner = state.get("winner")
    if args.candidate:
        if args.candidate not in state.get("candidates", {}):
            raise UserError("Unknown candidate for this swarm: " + args.candidate)
        patch = folder / "candidates" / args.candidate / "final.patch"
        if not patch.is_file():
            raise UserError("That candidate has no recorded changes.")
        accepted = bool(winner and winner["slot"] == args.candidate and winner["accepted"])
    else:
        if not winner:
            raise UserError("This swarm recorded no candidate to apply.")
        patch, accepted = Path(winner["patch"]), winner["accepted"]
    if patch.is_symlink() or not patch.resolve().is_relative_to(folder.resolve()):
        raise UserError("Invalid candidate patch path.")
    if not accepted and not args.allow_unaccepted:
        raise UserError("The swarm did not accept this candidate. Review its patch, then pass --allow-unaccepted to apply it anyway.")
    config = validate_config(json.loads(read_text(frozen)))
    workflow = record["agent_workflow"]
    if args.no_checks:
        config["workflows"][workflow]["required_checks"] = []
    with lock(state_dir(root) / "operation.lock"):
        result = asyncio.run(integrate(root, cfg, folder, config, workflow, state["baseline_fingerprint"], patch))
        for e in result.pop("evidence"):
            record.setdefault("checks", []).append({
                "argv": config["tools"][e["tool"]]["argv"], "started": None, "finished": now(),
                "exit_code": e["exit_code"] if e["exit_code"] is not None else 124,
                "contract_passed": e["status"] == "passed", "error": e["error"], "fingerprint": e["fingerprint"],
                "source_changed_during_check": e["source_changed_during_check"],
                "log": str(Path(e["folder"]) / "events.jsonl"), "origin": "swarm_apply"})
        result.update(candidate=args.candidate or winner["slot"], accepted_by_swarm=accepted)
        record["swarm_applied"] = result
        Store(root).save(record)
        state["applied"] = result
        write_json(state_path, state)
        checkpoint(root, cfg, record)
    output({**result, "note": "Applied to the working tree only (not staged or committed). Passing checks cover only what they test."})
    return 0 if result["checks_pass"] in {True, None} else 1


def dispatch_agents(root, cfg, args):
    command = args.agents_command
    if command == "demo":
        return run_demo(args.output, args.swarm)
    if command == "init":
        if not (state_dir(root) / "config.toml").exists():
            initialize(root)
        output(initialize_agents(root))
        return 0
    require_init(root)
    if command in {"list", "validate"}:
        config, path = load_agents(root, args.config)
        output({"path": str(path), "valid": True,
                "agents": [{"name": name, "backend": spec["backend"], "write_scope": spec["write_scope"],
                            "peers": spec["peers"], "project_tools": spec["project_tools"]} for name, spec in config["agents"].items()],
                "tools": list(config["tools"]), "workflows": config["workflows"],
                "claude_sdk_installed": importlib.util.find_spec("claude_agent_sdk") is not None,
                "jsonschema_installed": importlib.util.find_spec("jsonschema") is not None,
                "note": "Validation reads configuration only. No model, tool, CLI peer or MCP server was launched."})
        return 0
    if command in {"run", "ask"}:
        with lock(state_dir(root) / "operation.lock"):
            if command == "ask":
                request = {"agent": args.agent, "folder": args.folder, "scopes": args.scope,
                           "read_only": args.read_only, "checks": args.check}
                if args.folder and not (root / args.folder).is_dir():
                    raise UserError(f"Folder does not exist inside the project: {args.folder}")
                record = prepare_feedback(root, cfg, args.task, "default", args.config, args.include, model=args.model, ask=request)
            else:
                record = prepare_feedback(root, cfg, args.goal, args.workflow, args.config, args.include, model=args.model,
                                          replicas=args.replicas, rounds=args.rounds)
            if args.prepare_only:
                print_run(record)
                print(f"No agents launched. Run with: agentctl run {record['id']}")
                return 0
            record = execute_run(root, cfg, record)
        print_run(record)
        return 0 if run_succeeded(record) else 1
    record = record_for(root, args.run_id)
    if command == "apply":
        return apply_candidate(root, cfg, record, args)
    if command == "message":
        message = send_mail(record["folder"], record["agent_names"], "operator", args.to, args.text)
        output({"queued": message, "delivery": "Agent reads it on its next message-tool call or invocation; this does not interrupt a model response."})
    elif command == "messages":
        if args.to and args.to not in record["agent_names"]:
            raise UserError("Unknown managed agent.")
        output(read_mail(record["folder"], args.to))
    elif command == "events":
        path = Path(record["folder"]) / "events.jsonl"
        if path.is_symlink():
            raise UserError("Event storage cannot be a symlink.")
        offset = 0
        while True:
            if path.exists():
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(offset)
                    for line in handle:
                        print(safe_terminal(line.rstrip()), flush=True)
                    offset = handle.tell()
            if not args.follow or Store(root).get(record["id"])["status"] not in {"prepared", "running"}:
                break
            time.sleep(0.2)
    return 0
