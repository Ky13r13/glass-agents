"""Offline demonstration: real tool/check feedback, scripted agents, no model."""
from __future__ import annotations

import json
from pathlib import Path
import sys

from .agent_config import starter_config
from .common import write_json
from .project import initialize


def initialize_demo(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("# Agent feedback demo\nSynthetic agents; no model or external service.\n")
    (root / "note.txt").write_text("draft\n")
    (root / "check.py").write_text(
        "import json\nfrom pathlib import Path\n"
        "ok = 'verified' in Path('note.txt').read_text()\n"
        "print(json.dumps({'contains_verified_marker': ok}))\n"
        "raise SystemExit(0 if ok else 1)\n")
    initialize(root)
    config = starter_config()
    config["agents"]["worker"]["write_scope"] = ["note.txt"]
    config["tools"]["check"] = {"argv": [sys.executable, "check.py"], "format": "json",
                               "description": "Check the demo note for the required marker.",
                               "input_schema": {"type": "object", "additionalProperties": False}}
    config["workflows"]["default"]["required_checks"] = ["check"]
    write_json(root / ".agentctl/agents.json", config)
    return root


async def demo_driver(runtime, actor, prompt, stack, reviewer):
    runtime.emit("agent.message", actor, text="Synthetic agent fixture; this is an offline controller demonstration.")
    if actor == "advisor":
        result = await runtime.tool(actor, "read_file", {"path": "note.txt"}, stack)
        return {"summary": "The actual note currently contains: " + result["text"], "artifacts": []}
    if reviewer:
        evidence = list(runtime.state["tool_results"].values())
        latest = evidence[-1] if evidence else {}
        passes = latest.get("status") == "passed"
        return {"decision": "accept" if passes else "revise",
                "feedback": "The required check passed." if passes else "The check failed. Add the verified marker to note.txt."}
    await runtime.tool(actor, "send_message", {"agent": "advisor", "message": "Inspect the demo note before I revise it."}, stack)
    await runtime.tool(actor, "call_agent", {"agent": "advisor", "task": "Read note.txt and report its contents."}, stack)
    messages = await runtime.tool(actor, "read_messages", {}, stack)
    revised = any("check failed" in m["body"].lower() for m in messages["messages"])
    await runtime.tool(actor, "write_file", {"path": "note.txt", "content": "verified\n" if revised else "draft revision\n"}, stack)
    return {"summary": "Updated the demo note using reviewer feedback." if revised else "Produced an initial draft.",
            "artifacts": ["note.txt"]}
