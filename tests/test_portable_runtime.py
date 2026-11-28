"""Portable dashboard and process tests using Python executables instead of shell fixtures."""
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agentctl.common import UserError, lock
from agentctl.dashboard import Dashboard, DashboardServer
from agentctl.processes import split_command
from agentctl.project import initialize
from agentctl.runner import stream_process


PLANNER = """
import json, sys, time
from pathlib import Path
prompt = sys.stdin.read()
print(json.dumps({"type": "thread.started"}), flush=True)
if 'cancel-this-draft' in prompt:
    time.sleep(30)
result = dict(summary="Portable fixture", lead="builder", reviewer="", agents=[
    dict(name="builder", backend="codex", instructions="Build the fixture.", write_scope=["**"], peers=[])])
Path(sys.argv[1]).write_text(json.dumps(result), encoding="utf-8")
"""


class PortableRuntime(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_workspace_lock_is_exclusive_and_reusable(self):
        path = self.root / "state.lock"
        with lock(path):
            with self.assertRaises(UserError):
                with lock(path):
                    self.fail("Workspace lock was granted twice.")
        with lock(path):
            self.assertTrue(path.exists())

    def test_process_drains_both_pipes_and_times_out_when_no_output_arrives(self):
        packet = self.root / "stdin"
        packet.write_text("", encoding="utf-8")
        cmd = [sys.executable, "-c", "import sys; print('event'); sys.stderr.write('detail' * 20000)"]
        events = []
        self.assertEqual(stream_process(cmd, self.root, packet, self.root, 10, events.append), 0)
        self.assertEqual([event.rstrip(b"\r\n") for event in events], [b"event"])
        self.assertEqual((self.root / "stderr.log").stat().st_size, 120000)
        with self.assertRaisesRegex(UserError, "time limit"):
            stream_process([sys.executable, "-c", "import time; time.sleep(30)"],
                           self.root, packet, self.root, 0.25, events.append)

    @unittest.skipUnless(sys.platform == "win32", "Windows command line semantics")
    def test_windows_verification_command_keeps_backslashes_and_spaces(self):
        argv = [r"C:\Program Files\Python\python.exe", r"C:\Projects\hello world.py"]
        self.assertEqual(split_command(r'"C:\Program Files\Python\python.exe" "C:\Projects\hello world.py"'), argv)

    def test_dashboard_draft_progress_cancellation_and_completion(self):
        initialize(self.root)
        manager = Dashboard(self.root)
        self.addCleanup(manager.close)
        server = DashboardServer(("127.0.0.1", 0), manager)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)

        def request(path, body=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                conn.request("GET" if body is None else "POST", path,
                             body=None if body is None else json.dumps(body),
                             headers={"X-Agentctl-Token": server.token})
                result = conn.getresponse()
                payload = json.loads(result.read())
                self.assertEqual(result.status, 200, payload)
                return payload
            finally:
                conn.close()

        def wait_for(predicate):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                value = request("/api/team/draft")
                if predicate(value):
                    return value
                time.sleep(0.05)
            self.fail(f"Draft did not reach expected state: {value}")

        def command(cfg, root, output, permission, **kwargs):
            self.assertEqual(permission, "read-only")
            return [sys.executable, "-c", PLANNER, str(output)]

        with patch("agentctl.team_draft.codex_command", side_effect=command):
            path = "/api/team/draft"
            started = request(path, {"goal": "cancel-this-draft", "mode": "build", "backends": ["codex"]})
            self.assertEqual(started["stage"], "starting")
            observed = wait_for(lambda draft: draft["events"] > 0)
            self.assertEqual(observed["stage"], "drafting")
            self.assertEqual(request(path + "/cancel", {"id": started["id"]})["status"], "cancelling")
            wait_for(lambda draft: draft["status"] == "cancelled")
            if manager.drafts.thread:
                manager.drafts.thread.join(timeout=3)
            request(path, {"goal": "build fixture", "mode": "scaffold", "backends": ["codex"]})
            result = wait_for(lambda draft: draft["status"] in {"completed", "failed"})
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(result["stage"], "validating")
            self.assertEqual(result["result"]["workflow"]["lead"], "builder")

    def test_claude_can_draft_without_codex_or_project_tools(self):
        project = self.root / "project"
        project.mkdir()
        initialize(project)
        sdk = self.root / "claude_agent_sdk.py"
        sdk.write_text("""
from pathlib import Path

class ClaudeAgentOptions:
    def __init__(self, **kwargs):
        assert kwargs["tools"] == []
        assert kwargs["allowed_tools"] == []
        assert kwargs["setting_sources"] == []
        assert kwargs["mcp_servers"] == {}
        assert kwargs["output_format"]["type"] == "json_schema"
        assert Path.cwd().name.startswith("agentctl-team-draft-")

class ResultMessage:
    subtype = "success"
    structured_output = dict(summary="Claude fixture", lead="builder", reviewer="", agents=[
        dict(name="builder", backend="claude-sdk", instructions="Build the fixture.", write_scope=["**"], peers=[])])

class ClaudeSDKClient:
    def __init__(self, options): self.options = options
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return None
    async def query(self, prompt): assert "Compose a demo" in prompt
    async def receive_response(self): yield ResultMessage()
""", encoding="utf-8")
        with patch.dict(os.environ, {"PYTHONPATH": str(self.root)}):
            manager = Dashboard(project)
            self.addCleanup(manager.close)
            manager.settings["codex_bin"] = "missing-codex-executable"
            result = manager.draft_team({"provider": "claude-sdk", "goal": "Compose a demo",
                                         "mode": "scaffold", "backends": ["claude-sdk"]})
        self.assertEqual(result["summary"], "Claude fixture")
        self.assertEqual(result["agents"][0]["backend"], "claude-sdk")
        self.assertFalse((project / ".agentctl" / "agents.json").exists())
