"""An explicitly synthetic, offline demo of the real subprocess/event path."""
from pathlib import Path

SCRIPT = r'''#!/usr/bin/env python3
import json, sys, time
from pathlib import Path
args = sys.argv
if "--version" in args:
    print("agentctl synthetic CLI fixture 0.3 (not a model connection)")
    raise SystemExit()
if "--help" in args:
    print("run --standalone --format --agent --file --model")
    raise SystemExit()
is_opencode = len(args) > 1 and args[1] == "run"
packet = Path(args[args.index("--file") + 1]).read_text() if is_opencode else sys.stdin.read()
def emit(event):
    print(json.dumps(event), flush=True)
if not is_opencode:
    emit({"type": "thread.started", "thread_id": "synthetic-demo-session"})
messages = [
    ("message", "Synthetic demo: reading the supplied context packet. No model, market feed, or trading account is connected."),
    ("tool", "inspect strategy interface"),
    ("message", "The input contract should identify the dataset, strategy version, parameter ranges and evaluation protocol."),
    ("tool", "inspect validation boundaries"),
    ("message", "Keep the final holdout outside the experiment feedback loop. Record all attempted strategies and parameter trials."),
    ("tool", "prepare experiment summary"),
]
for index, (kind, text) in enumerate(messages):
    time.sleep(2)
    if is_opencode:
        part = {"type": "text", "text": text} if kind == "message" else {"type": "tool", "tool": "fixture.inspect", "state": {"status": "completed", "input": {"task": text}, "output": "Synthetic fixture output; no command executed."}}
        emit({"type": "text" if kind == "message" else "tool_use", "sessionID": "synthetic-demo-session", "part": part})
    else:
        item = {"type": "agent_message", "text": text} if kind == "message" else {"type": "mcp_tool_call", "tool_name": "fixture.inspect", "arguments": {"task": text}, "status": "completed", "result": "Synthetic fixture output; no command executed."}
        emit({"type": "item.completed", "item": item})
answer = "Synthetic demonstration completed. Suggested integration: a versioned experiment specification in, a machine-generated result bundle out. No strategy was evaluated and no correctness checks were run."
if is_opencode:
    emit({"type": "text", "sessionID": "synthetic-demo-session", "part": {"text": answer}})
    emit({"type": "step_finish", "part": {"reason": "stop", "tokens": {}}})
else:
    Path(args[args.index("--output-last-message") + 1]).write_text(answer)
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": answer}})
    emit({"type": "turn.completed", "usage": {}})
'''


def make_runtime(root):
    path = Path(root) / ".agentctl" / "synthetic-cli"
    path.write_text(SCRIPT)
    path.chmod(0o700)
    return path
