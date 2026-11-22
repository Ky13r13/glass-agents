from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agentctl.cli import main
from agentctl.common import DEFAULTS, Store, UserError, load_config, lock, write_text
from agentctl.context import build_packet
from agentctl.local import Ollama, benchmark
from agentctl.project import guidance_sources, initialize, inventory, snapshot
from agentctl.runner import execute_run, prepare_run, verification_state, verify
from agentctl.team import run_team, validate_plan


FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
if "--version" in sys.argv:
    print("codex-test-fixture 0.1")
    raise SystemExit(0)
args = sys.argv
text = sys.stdin.read()
cwd = Path(args[args.index("--cd") + 1])
assert args[args.index("--sandbox") + 1] in ("read-only", "workspace-write")
assert "features.multi_agent=false" in args
assert "--dangerously-bypass-approvals-and-sandbox" not in args
assert text.startswith("# Task")
trace = os.environ.get("AGENTCTL_TEST_TRACE")
ident = text.splitlines()[2]
def log(stage):
    if trace:
        with open(trace, "a") as f:
            f.write(json.dumps({"stage":stage,"goal":ident,"time":time.time()}) + "\n")
log("start")
print(json.dumps({"type":"thread.started", "thread_id":"test-thread-123"}), flush=True)
if "SIM_SLEEP" in ident:
    time.sleep(4)
if "SIM_PAUSE" in ident:
    time.sleep(0.25)
if "SIM_FAIL" in ident:
    print(json.dumps({"type":"turn.failed", "error":{"message":"simulated failure"}}), flush=True)
    raise SystemExit(0)
if "SIM_WRITE" in ident:
    (cwd / "result.py").write_text("answer = 42\n")
if "SIM_FLOOD" in ident:
    sys.stderr.write("diagnostic\n" * 12000)
    sys.stderr.flush()
answer = "Inspected the supplied task. Verification has not been run."
Path(args[args.index("--output-last-message") + 1]).write_text(answer)
print(json.dumps({"type":"item.completed", "item":{"type":"agent_message", "text":answer}}), flush=True)
print(json.dumps({"type":"turn.completed", "usage":{"input_tokens":200,"cached_input_tokens":50,"output_tokens":30}}), flush=True)
log("end")
'''


class BaseWorkspace(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "project with spaces"
        self.root.mkdir()
        self.home = self.base / "codex-home"
        self.home.mkdir()
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.home), "XDG_STATE_HOME": str(self.base / "state")})
        self.env.start()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "README.md").write_text("# Example\nCSV export lives in src/export.py.\n")
        (self.root / "src").mkdir()
        (self.root / "src/export.py").write_text("def export_csv(rows):\n    return ','.join(rows)\n")
        self.fake = self.base / "fake-codex"
        self.fake.write_text(FAKE_CODEX)
        self.fake.chmod(0o755)
        initialize(self.root)
        self.cfg = load_config(self.root)
        self.cfg["codex_bin"] = str(self.fake)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = main(["-C", str(self.root), *args])
        return result, out.getvalue(), err.getvalue()


class Workspace(BaseWorkspace):
    def test_init_preserves_existing_guidance_and_is_idempotent(self):
        guide = self.root / "AGENTS.md"
        guide.write_text("User's exact guidance.\n")
        initialize(self.root)
        initialize(self.root)
        self.assertEqual(guide.read_text(), "User's exact guidance.\n")

    def test_override_prevents_extra_root_guidance(self):
        (self.root / "AGENTS.md").unlink()
        (self.root / "AGENTS.override.md").write_text("Override instructions.\n")
        initialize(self.root)
        self.assertFalse((self.root / "AGENTS.md").exists())

    def test_scan_excludes_gitignore_secrets_links_and_generated_files(self):
        (self.root / ".gitignore").write_text("ignored.py\n")
        for name in ["ignored.py", ".env", "secret.pem"]:
            (self.root / name).write_text("PRIVATE DATA")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules/leak.py").write_text("PRIVATE DATA")
        (self.base / "outside.py").write_text("PRIVATE DATA")
        (self.root / "link.py").symlink_to(self.base / "outside.py")
        paths = [x["path"] for x in inventory(self.root, self.cfg)["files"]]
        for name in ["ignored.py", ".env", "secret.pem", "node_modules/leak.py", "link.py"]:
            self.assertNotIn(name, paths)

    def test_packet_has_budget_sources_and_redacted_credentials(self):
        secret = "sk-" + "a" * 40
        (self.root / "src/export.py").write_text('API_KEY = "' + secret + '"\ndef export_csv(rows): return rows\n')
        r = prepare_run(self.root, self.cfg, "Explain CSV export", includes=["src/export.py"])
        text = (Path(r["folder"]) / "PACKET.md").read_text()
        self.assertNotIn(secret, text)
        self.assertIn("REDACTED", text)
        self.assertLessEqual(r["manifest"]["estimated_total_known_tokens"], 6000)
        self.assertTrue(any(x["path"] == "src/export.py" for x in r["manifest"]["selected"]))

    def test_explicit_include_cannot_escape_project(self):
        with self.assertRaises(UserError):
            prepare_run(self.root, self.cfg, "Read source", includes=["../outside.py"])

    def test_oversized_required_guidance_is_not_silently_truncated(self):
        (self.home / "AGENTS.md").write_text("Mandatory guidance. " * 2000)
        with self.assertRaisesRegex(UserError, "Required task/guidance"):
            prepare_run(self.root, self.cfg, "Read source", budget=1000)

    def test_nested_guidance_is_included(self):
        (self.root / "src/AGENTS.md").write_text("Use semicolon CSV delimiters in this folder.")
        rec = prepare_run(self.root, self.cfg, "Explain CSV export", includes=["src/export.py"])
        text = (Path(rec["folder"]) / "PACKET.md").read_text()
        self.assertIn("Use semicolon CSV delimiters", text)

    def test_run_records_real_process_events_and_keeps_completion_distinct_from_verification(self):
        rec = prepare_run(self.root, self.cfg, "SIM_WRITE add CSV result")
        rec = execute_run(self.root, self.cfg, rec, quiet=True)
        self.assertEqual(rec["status"], "completed")
        self.assertEqual(rec["thread_id"], "test-thread-123")
        self.assertEqual(rec["usage"]["input_tokens"], 200)
        self.assertIn("result.py", rec["changes"]["added"])
        self.assertIn("No explicit verification", verification_state(rec, snapshot(self.root, self.cfg)))
        self.assertTrue((self.root / ".agentctl/HANDOFF.md").is_file())

    def test_turn_failed_with_exit_zero_is_failure(self):
        rec = execute_run(self.root, self.cfg, prepare_run(self.root, self.cfg, "SIM_FAIL"), quiet=True)
        self.assertEqual(rec["status"], "failed")

    def test_timeout_terminates_child_and_records_failure(self):
        cfg = dict(self.cfg, timeout_seconds=1)
        started = time.monotonic()
        rec = execute_run(self.root, cfg, prepare_run(self.root, cfg, "SIM_SLEEP"), quiet=True)
        self.assertEqual(rec["status"], "failed")
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn("time limit", rec["error"])

    def test_stderr_is_drained_without_deadlock(self):
        rec = execute_run(self.root, self.cfg, prepare_run(self.root, self.cfg, "SIM_FLOOD"), quiet=True)
        self.assertEqual(rec["status"], "completed")
        self.assertGreater((Path(rec["folder"]) / "stderr.log").stat().st_size, 64000)

    def test_prepared_context_refuses_stale_source(self):
        rec = prepare_run(self.root, self.cfg, "Explain CSV")
        (self.root / "src/export.py").write_text("# changed\n")
        with self.assertRaisesRegex(UserError, "changed since preparation"):
            execute_run(self.root, self.cfg, rec)

    def test_prepared_context_refuses_changed_global_guidance(self):
        rec = prepare_run(self.root, self.cfg, "Explain CSV")
        (self.home / "AGENTS.md").write_text("New instructions")
        with self.assertRaisesRegex(UserError, "guidance changed"):
            execute_run(self.root, self.cfg, rec)

    def test_write_scope_violation_is_reported(self):
        rec = prepare_run(self.root, self.cfg, "SIM_WRITE", write_scope=["src/**"])
        rec = execute_run(self.root, self.cfg, rec, quiet=True)
        self.assertEqual(rec["status"], "scope_violation")
        self.assertIn("result.py", rec["out_of_scope"])

    def test_readonly_detects_changed_source_even_if_provider_claims_success(self):
        rec = prepare_run(self.root, self.cfg, "SIM_WRITE", permission="read-only")
        rec = execute_run(self.root, self.cfg, rec, quiet=True)
        self.assertEqual(rec["status"], "scope_violation")

    def test_check_success_and_staleness_are_recorded(self):
        rec = execute_run(self.root, self.cfg, prepare_run(self.root, self.cfg, "SIM_WRITE"), quiet=True)
        proof = verify(self.root, self.cfg, rec["id"], [sys.executable, "-c", "import result; assert result.answer == 42"])
        self.assertEqual(proof["exit_code"], 0)
        rec = Store(self.root).get(rec["id"])
        self.assertIn("checks passed", verification_state(rec, snapshot(self.root, self.cfg)))
        (self.root / "result.py").write_text("answer = 0\n")
        self.assertIn("stale", verification_state(rec, snapshot(self.root, self.cfg)))

    def test_failing_check_is_not_reported_as_passed(self):
        rec = prepare_run(self.root, self.cfg, "Read CSV")
        proof = verify(self.root, self.cfg, rec["id"], [sys.executable, "-c", "raise SystemExit(4)"])
        self.assertEqual(proof["exit_code"], 4)
        self.assertIn("failed", verification_state(Store(self.root).get(rec["id"]), snapshot(self.root, self.cfg)))

    def test_resume_uses_handoff_and_observes_new_changes(self):
        rec = execute_run(self.root, self.cfg, prepare_run(self.root, self.cfg, "Read CSV"), quiet=True)
        (self.root / "src/export.py").write_text("# changed since session\n")
        rc, out, err = self.cli("resume", "Check the new changes", "--prepare-only")
        self.assertEqual(rc, 0, err)
        latest = Store(self.root).get()
        self.assertEqual(latest["parent"], rec["id"])
        self.assertIn("src/export.py", (Path(latest["folder"]) / "PACKET.md").read_text())

    def test_repeated_resume_preserves_objective_model_and_readonly(self):
        rec = prepare_run(self.root, self.cfg, "Explain CSV", permission="read-only", model="chosen-model")
        rec = execute_run(self.root, self.cfg, rec, quiet=True)
        for _ in range(2):
            rc, _, err = self.cli("resume", "Continue analysis", "--run", rec["id"], "--prepare-only")
            self.assertEqual(rc, 0, err)
            rec = Store(self.root).get()
        self.assertEqual(rec["objective"], "Explain CSV")
        self.assertEqual(rec["goal"].count("Original task:"), 1)
        self.assertEqual(rec["permission"], "read-only")
        self.assertEqual(rec["model"], "chosen-model")

    def test_operation_lock_prevents_two_writers(self):
        with lock(self.root / ".agentctl/operation.lock"):
            rc, _, err = self.cli("context", "Read code")
        self.assertEqual(rc, 2)
        self.assertIn("Another operation", err)

    def test_state_symlink_is_rejected(self):
        (self.root / ".agentctl/runs").symlink_to(self.base)
        with self.assertRaises(UserError):
            load_config(self.root)

    def test_dependency_cycle_is_rejected(self):
        with self.assertRaisesRegex(UserError, "cycle"):
            validate_plan({"version": 1, "jobs": [{"id":"a", "goal":"a", "depends_on":["b"]}, {"id":"b", "goal":"b", "depends_on":["a"]}]})

    def test_team_parallel_readers_then_writer(self):
        trace = self.base / "trace.jsonl"
        plan = {"version":1, "goal":"Team test", "jobs":[
            {"id":"a", "goal":"SIM_PAUSE reader A", "permission":"read-only"},
            {"id":"b", "goal":"SIM_PAUSE reader B", "permission":"read-only"},
            {"id":"c", "goal":"SIM_WRITE implementation", "permission":"workspace-write", "depends_on":["a","b"]}]}
        with patch.dict(os.environ, {"AGENTCTL_TEST_TRACE": str(trace)}), redirect_stdout(io.StringIO()):
            result = run_team(self.root, self.cfg, plan)
        self.assertTrue(result["successful"], result)
        events = [json.loads(x) for x in trace.read_text().splitlines()]
        starts = {e["goal"]: e["time"] for e in events if e["stage"] == "start"}
        ends = {e["goal"]: e["time"] for e in events if e["stage"] == "end"}
        self.assertLess(max(starts["SIM_PAUSE reader A"], starts["SIM_PAUSE reader B"]), min(ends["SIM_PAUSE reader A"], ends["SIM_PAUSE reader B"]))
        self.assertGreaterEqual(starts["SIM_WRITE implementation"], max(ends["SIM_PAUSE reader A"], ends["SIM_PAUSE reader B"]))

    def test_failed_dependency_blocks_writer(self):
        plan = {"version":1, "jobs":[{"id":"a", "goal":"SIM_FAIL"}, {"id":"b", "goal":"SIM_WRITE", "permission":"workspace-write", "depends_on":["a"]}]}
        with redirect_stdout(io.StringIO()):
            result = run_team(self.root, self.cfg, plan)
        self.assertEqual(result["status"]["b"], "blocked")
        self.assertFalse((self.root / "result.py").exists())


class LocalAPI(BaseWorkspace):
    def setUp(self):
        super().setUp()
        self.requests = []
        requests = self.requests
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.send_value({"models": []} if self.path == "/api/ps" else {"version":"fixture"})
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                answer = json.dumps({"result":15,"mutation":False}) if "format" in body else "Read-only fixture analysis."
                self.send_value({"message":{"role":"assistant","content":answer},"done":True,"done_reason":"stop", "prompt_eval_count":30, "eval_count":12, "eval_duration":1000000000})
            def send_value(self, value):
                raw = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cfg["ollama_url"] = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def test_local_worker_uses_context_limit_and_no_tools(self):
        rec = prepare_run(self.root, self.cfg, "Explain CSV export", backend="local", permission="read-only")
        rec = execute_run(self.root, self.cfg, rec, quiet=True)
        self.assertEqual(rec["status"], "completed", rec.get("error"))
        self.assertEqual(rec["usage"]["output_tokens"], 12)
        self.assertEqual(self.requests[-1]["options"]["num_ctx"], 8192)
        self.assertNotIn("tools", self.requests[-1])

    def test_benchmark_reports_measured_usage_and_limited_scope(self):
        result = benchmark(self.cfg)
        self.assertTrue(result["basic_analysis_passed"])
        self.assertEqual(result["generation_tokens_per_second"], 12)
        self.assertIn("does not certify", result["scope"])

    def test_remote_endpoint_rejected(self):
        with self.assertRaises(UserError):
            Ollama(dict(self.cfg, ollama_url="https://example.com"))

    def test_installed_zipapp_launches_local_worker_from_another_directory(self):
        source = Path(__file__).resolve().parents[1]
        installed = self.base / "bin/agentctl"
        result = subprocess.run([sys.executable, str(source / "scripts/install.py"), "--output", str(installed)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.root / ".agentctl/config.toml"
        config.write_text(config.read_text().replace("http://127.0.0.1:11434", self.cfg["ollama_url"]))
        result = subprocess.run([str(installed), "-C", str(self.root), "start", "Explain CSV", "--backend", "local"],
                                cwd=self.base, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("completed (local)", result.stdout)


if __name__ == "__main__":
    unittest.main()
