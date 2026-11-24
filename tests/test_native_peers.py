"""Offline provider fixtures exercising Glass handoffs and their boundaries."""
import asyncio
import json
from pathlib import Path
import sys
from unittest.mock import patch

from agentctl.agent_config import ask_config, prepare_feedback, starter_config, validate_config
from agentctl.agent_runtime import AgentRuntime, BudgetReached, read_mail
from agentctl.common import UserError, new_id, write_json
from agentctl.runner import execute_run
from test_agents import RuntimeWorkspace, fake_sdk
from test_workflows import LocalWorkspace


CLI_FIXTURE = r'''#!/usr/bin/env python3
import json
from pathlib import Path
import sys

def argument(flag):
    return sys.argv[sys.argv.index(flag) + 1]

if "--help" in sys.argv:
    print("--standalone")
    sys.exit(0)
codex = "--output-schema" in sys.argv
packet = sys.stdin.read() if codex else Path(argument("--file")).read_text()
path = packet.split("Read the full role and response contract in ", 1)[1].split(". Follow it", 1)[0]
contract = Path(path).read_text()
if codex:
    assert argument("--sandbox") == "read-only"
    schema = json.loads(Path(argument("--output-schema")).read_text())
    if "Your name: reviewer\n" in contract:
        assert "decision" in schema["properties"]
        response = {"decision": "accept", "feedback": "Checked the received evidence."}
    elif "Turn 1 of" in contract:
        assert "Assigned by: worker\n" in contract and "Which text should I use?" in contract
        assert "turn" in schema["properties"]
        response = {"turn": {"send_message": {"agent": "worker", "message": "Use lowercase."}}}
    else:
        assert '"queued"' in contract
        response = {"turn": {"final": {"summary": "Write cobalt.", "artifacts": []}}}
    Path(argument("--output-last-message")).write_text(json.dumps(response))
    print(json.dumps({"type": "turn.completed", "usage": {"output_tokens": 2}}))
else:
    print(json.dumps({"type": "step_start"}))
    print(json.dumps({"type": "text", "part": {"text": "Inspecting the role contract."}}))
    print(json.dumps({"type": "step_finish", "part": {"reason": "tool-calls"}}))
    print(json.dumps({"type": "step_start"}))
    if "Turn 1 of" in contract:
        response = {"turn": {"call_agent": {"agent": "advisor", "task": "Which text should I use?"}}}
    else:
        assert "Write cobalt." in contract and "Use lowercase." in contract
        Path("note.txt").write_text("cobalt\n")
        response = {"turn": {"final": {"summary": "Wrote the requested note.", "artifacts": ["note.txt"]}}}
    print(json.dumps({"type": "text", "part": {"text": json.dumps(response)}}))
    print(json.dumps({"type": "step_finish", "part": {"reason": "stop", "tokens": {"output": 3}, "cost": 0.01}}))
'''


class NativePeers(RuntimeWorkspace):
    def test_two_clis_exchange_mail_delegate_and_use_the_result_before_checked_review(self):
        self.fake.write_text(CLI_FIXTURE)
        self.cfg["opencode_bin"] = str(self.fake)
        config = starter_config()
        config["agents"]["worker"].update(backend="opencode", write_scope=["note.txt"])
        config["agents"]["advisor"].update(backend="codex", peers=["worker"])
        config["agents"]["reviewer"]["backend"] = "codex"
        check = "from pathlib import Path; assert Path('note.txt').read_text() == 'cobalt\\n'"
        config["tools"]["note"] = {"argv": [sys.executable, "-c", check]}
        config["workflows"]["default"]["required_checks"] = ["note"]
        write_json(self.root / ".agentctl/agents.json", config)
        record = prepare_feedback(self.root, self.cfg, "Write the advisor's text in note.txt")
        record = execute_run(self.root, self.cfg, record, quiet=True)
        folder = Path(record["folder"])
        state = json.loads((folder / "loop/state.json").read_text())
        self.assertEqual(record["loop_outcome"], "accepted", state["error"])
        self.assertEqual(record["checks"][0]["exit_code"], 0)
        self.assertEqual(record["usage"]["output_tokens"], 12)
        self.assertAlmostEqual(record["reported_cost_usd"], .02)
        self.assertEqual(state["agent_calls"], 3)
        self.assertEqual(state["tool_calls"], 3)
        self.assertEqual(read_mail(folder, "worker")[0]["from"], "advisor")
        events = [json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()]
        links = [(e["actor"], e["turn"]) for e in events if e["type"] == "agentctl.peer.link"]
        self.assertEqual(links, [("worker", 1), ("advisor", 1), ("advisor", 2), ("worker", 2), ("reviewer", 1)])
        self.assertTrue(any(e.get("actor") == "advisor" and e.get("parent") == "worker" for e in events))

    def test_invalid_actions_and_exhausted_turns_have_no_peer_side_effects(self):
        config = starter_config()
        config["agents"]["advisor"].update(backend="codex", peers=["worker"])
        forbidden = {"turn": {"send_message": {"agent": "reviewer", "message": "Hello"}}}
        wider = {"turn": {"call_agent": {"agent": "worker", "task": "Write a file"}}}
        allowed = {"turn": {"send_message": {"agent": "worker", "message": "Hello"}}}
        for raw, limit, error in [("plain text", 2, UserError), (json.dumps(forbidden), 2, UserError),
                                  (json.dumps(wider), 2, UserError), (json.dumps(allowed), 1, BudgetReached)]:
            with self.subTest(raw=raw):
                runtime = self.make_runtime(driver=None, config=config)
                runtime.workflow["max_turns_per_agent"] = limit
                answer = self.base / "answer.json"
                answer.write_text(raw)
                with patch("agentctl.native_peer.run_turn", return_value=answer), self.assertRaises(error):
                    asyncio.run(runtime.call_agent("advisor", "Report back"))
                self.assertEqual(read_mail(runtime.folder), [])
                self.assertEqual(runtime.state["agent_calls"], 1)

    def test_narrowing_caller_never_expands_peer_write_scope(self):
        config = starter_config()
        config["agents"]["advisor"]["write_scope"] = ["src/one.py"]
        narrowed = ask_config(config, "worker", folder="src")
        self.assertEqual(narrowed["agents"]["advisor"]["write_scope"], ["src/one.py"])
        config["agents"]["worker"]["write_scope"] = ["src/?.py"]
        with self.assertRaises(UserError):
            ask_config(config, "worker", scopes=["src/*.py"])
        config["agents"]["worker"]["write_scope"] = ["src/*.py"]
        narrowed = ask_config(config, "worker", scopes=["src/export*.py"])
        self.assertEqual(narrowed["agents"]["worker"]["write_scope"], ["src/export*.py"])


class LocalPeer(LocalWorkspace):
    def chat_answer(self, body):
        if len(self.requests) == 1:
            return json.dumps({"turn": {"call_agent": {"agent": "advisor", "task": "Check the CSV interface."}}})
        return json.dumps({"turn": {"final": {"summary": "Received review.", "artifacts": []}}})

    def test_local_agent_calls_sdk_and_receives_both_its_result_and_message(self):
        config = starter_config()
        config["agents"]["worker"].update(backend="local", write_scope=[])
        config["agents"]["advisor"]["peers"] = ["worker"]
        folder = self.root / ".agentctl/runs" / new_id()
        runtime = AgentRuntime(self.root, self.cfg, folder, validate_config(config), "default", "Inspect CSV")

        async def advise(client):
            self.assertIn("Assigned by: worker", client.prompt)
            tools = {tool.name: tool for tool in client.options.mcp_servers["agentctl"]["tools"]}
            receipt = await tools["send_message"].handler({"agent": "worker", "message": "Quote fields."})
            self.assertNotIn("is_error", receipt)

        with patch.dict(sys.modules, {"claude_agent_sdk": fake_sdk(advise)}):
            result = asyncio.run(runtime.call_agent("worker", "Get advice on CSV"))
        self.assertEqual(result["summary"], "Received review.")
        self.assertEqual(len(self.requests), 2)
        for request in self.requests:
            self.assertIn("turn", request["format"]["properties"])
            self.assertNotIn("tools", request)
        prompt = self.requests[-1]["messages"][-1]["content"]
        self.assertIn("SDK result", prompt)
        self.assertIn("Quote fields.", prompt)
        self.assertIn("Your task: Get advice on CSV", prompt)
