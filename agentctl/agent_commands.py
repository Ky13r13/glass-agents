"""Commands for attaching portable, managed agent definitions to any project."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import time

from .agent_config import initialize_agents, load_agents, prepare_feedback
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
    run.add_argument("--prepare-only", action="store_true")
    demo = commands.add_parser("demo", help="Offline scripted agents exercise real delegation, checks and revision.")
    demo.add_argument("--output", help="New directory in which to keep the demo; otherwise use a temporary project.")
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


def run_demo(output_path=None):
    from .feedback_demo import initialize_demo
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
        initialize_demo(root)
        cfg = load_config(root)
        record = prepare_feedback(root, cfg, "Revise note.txt until the configured check passes, with an independent review.", demo=True)
        print("Offline demo: scripted agent fixtures; real project tools, messages, delegation and revision. No models or external services.", flush=True)
        with lock(state_dir(root) / "operation.lock"):
            record = execute_run(root, cfg, record)
        print_run(record)
        output({"outcome": record.get("loop_outcome"), "iterations": record.get("loop_iteration"),
                "kept_project": str(root) if output_path else None,
                "messages": read_mail(record["folder"])})
        return 0 if record["status"] == "completed" and record.get("loop_outcome") == "accepted" else 1
    finally:
        if temporary:
            temporary.cleanup()


def dispatch_agents(root, cfg, args):
    command = args.agents_command
    if command == "demo":
        return run_demo(args.output)
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
    if command == "run":
        with lock(state_dir(root) / "operation.lock"):
            record = prepare_feedback(root, cfg, args.goal, args.workflow, args.config, args.include, model=args.model)
            if args.prepare_only:
                print_run(record)
                print(f"No agents launched. Run with: agentctl run {record['id']}")
                return 0
            record = execute_run(root, cfg, record)
        print_run(record)
        return 0 if record["status"] == "completed" and record.get("loop_outcome") == "accepted" else 1
    record = record_for(root, args.run_id)
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
