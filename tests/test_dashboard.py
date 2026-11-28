from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import socket
import signal
import subprocess
import sys
import threading
import time
import unittest

from agentctl.adapters import connection_info, event_summary, process_connections, public_url
from agentctl.common import Store, UserError, lock, state_dir, write_json
from agentctl.dashboard import Dashboard, DashboardServer, TERMINAL
from agentctl.dashboard_teams import team_state
from agentctl.runner import execute_run, prepare_run
from test_workflows import BaseWorkspace

FAKE_OPENCODE = r'''#!/usr/bin/env python3
import json, sys, time
from pathlib import Path
args = sys.argv
if "--version" in args:
    print("opencode-fixture 2.0")
    raise SystemExit()
if "--help" in args:
    print("--standalone --format --agent --file --model")
    raise SystemExit()
assert args[1] == "run"
assert "--standalone" in args
assert "--auto" not in args
assert args[args.index("--format") + 1] == "json"
assert sys.stdin.read() == ""
packet = Path(args[args.index("--file") + 1]).read_text()
assert packet.startswith("# Task")
print(json.dumps({"type":"step_start", "sessionID":"ses_fixture", "part":{}}), flush=True)
if "SIM_SLEEP" in packet:
    time.sleep(10)
if "SIM_ERROR" in packet:
    print(json.dumps({"type":"error","error":{"message":"fixture failure"}}), flush=True)
else:
    print(json.dumps({"type":"tool_use","part":{"type":"tool","tool":"bash","state":{"status":"completed","input":{"command":"git status"},"output":"clean"}}}), flush=True)
    print(json.dumps({"type":"text","part":{"type":"text","text":"Fixture answer; not an actual provider."}}), flush=True)
    print(json.dumps({"type":"step_finish","part":{"reason":"stop","tokens":{"input":30,"output":7,"reasoning":2,"cache":{"read":9,"write":0}},"cost":0.001}}), flush=True)
'''


class DashboardWorkspace(BaseWorkspace):
    def setUp(self):
        super().setUp()
        self.opencode = self.base / "fake-opencode"
        self.opencode.write_text(FAKE_OPENCODE)
        self.opencode.chmod(0o755)
        self.cfg["opencode_bin"] = str(self.opencode)
        self.dashboard = None

    def tearDown(self):
        if self.dashboard:
            self.dashboard.close()
        super().tearDown()

    def manager(self):
        self.dashboard = Dashboard(self.root, self.cfg)
        return self.dashboard

    def wait_job(self, manager, ident, timeout=7):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with manager.guard:
                job = dict(manager.find(ident))
            if job["status"] in TERMINAL:
                return job
            time.sleep(0.025)
        self.fail("Task did not finish: " + str(manager.find(ident)))

    def wait_running(self, manager, ident):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            with manager.guard:
                job = dict(manager.find(ident))
            if job.get("run_id"):
                rec = Store(self.root).get(job["run_id"])
                if rec.get("pid"):
                    return rec
            time.sleep(0.025)
        self.fail("Task did not start.")

    def test_opencode_uses_existing_runner_records_usage_and_command(self):
        rec = prepare_run(self.root, self.cfg, "Inspect source", "opencode", "workspace-write")
        rec = execute_run(self.root, self.cfg, rec, quiet=True)
        self.assertEqual(rec["status"], "completed")
        self.assertEqual(rec["thread_id"], "ses_fixture")
        self.assertEqual(rec["usage"]["cache_read_tokens"], 9)
        self.assertEqual(rec["usage"]["input_tokens"], 30)
        self.assertEqual(rec["reported_cost_usd"], 0.001)
        self.assertEqual(rec["commands_observed"][0]["command"], "git status")
        self.assertIn("Fixture answer", rec["answer_excerpt"])
        self.assertTrue(rec["pid"] > 0)

    def test_opencode_error_with_zero_exit_is_failure(self):
        rec = prepare_run(self.root, self.cfg, "SIM_ERROR", "opencode", "workspace-write")
        rec = execute_run(self.root, self.cfg, rec, quiet=True)
        self.assertEqual(rec["status"], "failed")
        self.assertIn("fixture failure", str(rec["provider_error"]))

    def test_opencode_cannot_be_mislabeled_a_sandboxed_reader(self):
        with self.assertRaisesRegex(UserError, "CLI-managed"):
            prepare_run(self.root, self.cfg, "Inspect", "opencode", "read-only")

    def test_queue_prepares_fresh_source_after_writer_and_passes_dependency_results(self):
        manager = self.manager()
        with manager.guard:
            a = manager.enqueue({"goal":"SIM_WRITE add result", "permission":"workspace-write"})
            b = manager.enqueue({"goal":"Inspect the written result", "depends_on":[a["id"]]})
        first = self.wait_job(manager, a["id"])
        second = self.wait_job(manager, b["id"])
        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "completed")
        packet = manager.artifact(b["id"], "PACKET.md")
        self.assertIn("result.py", packet)
        self.assertIn("Inspected the supplied task", packet)
        events = manager.events(b["id"])
        self.assertTrue(any(x["kind"] == "message" for x in events["events"]))
        self.assertEqual(manager.events(b["id"], events["offset"])["events"], [])

    def test_readers_overlap_writers_remain_exclusive(self):
        trace = self.base / "trace.jsonl"
        os.environ["AGENTCTL_TEST_TRACE"] = str(trace)
        self.addCleanup(lambda: os.environ.pop("AGENTCTL_TEST_TRACE", None))
        manager = self.manager()
        with manager.guard:
            a = manager.enqueue({"goal":"SIM_PAUSE reader one"})
            b = manager.enqueue({"goal":"SIM_PAUSE reader two"})
            c = manager.enqueue({"goal":"SIM_WRITE writer", "permission":"workspace-write"})
        for job in (a,b,c):
            self.assertEqual(self.wait_job(manager, job["id"])["status"], "completed")
        rows = [json.loads(x) for x in trace.read_text().splitlines()]
        times = {(r["goal"],r["stage"]):r["time"] for r in rows}
        self.assertLess(times[("SIM_PAUSE reader two","start")], times[("SIM_PAUSE reader one","end")])
        self.assertGreater(times[("SIM_WRITE writer","start")], times[("SIM_PAUSE reader two","end")])

    def test_missing_executable_is_visible_and_blocks_dependent_job(self):
        self.cfg["codex_bin"] = "/missing/cli"
        manager = self.manager()
        with manager.guard:
            a = manager.enqueue({"goal":"Inspect"})
            b = manager.enqueue({"goal":"Review", "depends_on":[a["id"]]})
        first = self.wait_job(manager,a["id"])
        self.assertEqual(first["status"], "failed")
        self.assertIn("executable", first["error"])
        self.assertEqual(self.wait_job(manager,b["id"])["status"], "blocked")
        rec = Store(self.root).get(first["run_id"])
        self.assertEqual(rec["status"], "failed")

    def test_cancellation_stops_process_and_followup_carries_previous_result(self):
        manager = self.manager()
        job = manager.enqueue({"goal":"SIM_SLEEP"})
        rec = self.wait_running(manager,job["id"])
        manager.cancel(job["id"])
        self.assertEqual(self.wait_job(manager,job["id"])["status"], "cancelled")
        with self.assertRaises(ProcessLookupError):
            os.kill(rec["pid"], 0)
        ok = manager.enqueue({"goal":"Inspect source"})
        self.wait_job(manager,ok["id"])
        follow = manager.followup(ok["id"],{"goal":"Explain the next step"})
        self.assertEqual(self.wait_job(manager,follow["id"])["status"], "completed")
        packet = manager.artifact(follow["id"],"PACKET.md")
        self.assertIn("Previous task: Inspect source",packet)
        self.assertIn("Inspected the supplied task",packet)

    def test_workflow_reuses_catalog_and_dependencies(self):
        manager = self.manager()
        jobs = manager.launch_workflow({"workflow":"bugfix","goal":"Inspect and repair the export"})
        self.assertEqual(len(jobs),3)
        for job in jobs:
            self.assertEqual(self.wait_job(manager,job["id"])["status"],"completed")
        self.assertEqual(len(manager.find(jobs[-1]["id"])["depends_on"]),1)
        with self.assertRaises(UserError):
            manager.launch_workflow({"workflow":"bugfix","goal":"Fix","backend":"local"})
        self.assertEqual(len(manager.jobs),3)

    def test_team_builder_saves_mixed_roster_and_queues_saved_workflow(self):
        manager = self.manager()
        manager.setup(agents=True)
        original = team_state(self.root)
        roster = original["agents"] + [
            {"name": f"peer-{i}", "backend": ("claude-sdk", "codex", "local")[i % 3],
             "model": None, "instructions": "Analyze the assigned task.", "write_scope": [],
             "peers": ["worker"]} for i in range(17)
        ]
        roster[0]["peers"].append("peer-2")
        body = {"revision": original["revision"], "agents": roster,
                "workflow": {"name": "mixed", "lead": "worker", "reviewer": "reviewer"}}
        saved = manager.save_team(body)
        self.assertEqual(len(saved["agents"]), 20)
        self.assertIn("default", saved["workflows"])
        self.assertIn("peer-2", saved["agents"][0]["peers"])
        with self.assertRaisesRegex(UserError, "changed"):
            manager.save_team(body)
        with manager.guard:
            job = manager.launch_team({"revision": saved["revision"], "workflow": "mixed", "goal": "Inspect source"})
            self.assertEqual(job["agent_workflow"], "mixed")
            self.assertEqual(job["permission"], "workspace-write")
            self.assertEqual(manager.find(job["id"])["team_revision"], saved["revision"])
            manager.cancel(job["id"])

    def test_prompt_drafts_and_saves_200_roles_without_touching_project_files(self):
        self.cfg["codex_bin"] = str(self.base / "fake-planner")
        planner = Path(self.cfg["codex_bin"])
        planner.write_text("""#!/usr/bin/env python3
import json, sys
from pathlib import Path
args = sys.argv
assert args[args.index("--sandbox") + 1] == "read-only"
assert Path.cwd().name.startswith("agentctl-team-draft-")
assert "working inventory app" in sys.stdin.read()
roles = [dict(name=f"role-{i}", backend="local", instructions=f"Analyze inventory requirement {i}.",
              write_scope=[], peers=[]) for i in range(199)]
lead = dict(name="lead", backend="codex", instructions="Build the project using delegated input.",
            write_scope=["**"], peers=[role["name"] for role in roles])
Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(
    dict(lead="lead", reviewer="", summary="Fixture-generated proposal.", agents=[lead, *roles])))
""")
        planner.chmod(0o755)
        manager = self.manager()
        draft = manager.draft_team({"goal": "Build a working inventory app.", "mode": "scaffold",
                                   "backends": ["codex", "local"]})
        self.assertEqual(len(draft["agents"]), 200)
        self.assertIn("minimal runnable project", draft["agents"][0]["instructions"])
        self.assertFalse((state_dir(self.root) / "agents.json").exists())
        self.assertEqual(manager.jobs, [])
        saved = manager.save_team(draft)
        self.assertEqual(len(saved["agents"]), 200)
        from agentctl.agent_config import load_agents
        definitions, _ = load_agents(self.root)
        workflow = definitions["workflows"][draft["workflow"]["name"]]
        self.assertGreaterEqual(workflow["max_agent_calls"], 200)

    def test_external_operation_lock_holds_queue_and_queued_tasks_can_cancel(self):
        manager = self.manager()
        with lock(state_dir(self.root)/"operation.lock"):
            job = manager.enqueue({"goal":"Wait"})
            time.sleep(0.3)
            self.assertEqual(manager.find(job["id"])["status"],"queued")
            manager.cancel(job["id"])
        self.assertEqual(self.wait_job(manager,job["id"])["status"],"cancelled")

    def test_second_dashboard_cannot_interrupt_first_dashboard_records(self):
        manager = self.manager()
        job = manager.enqueue({"goal":"SIM_SLEEP"})
        self.wait_running(manager,job["id"])
        with self.assertRaises(UserError):
            Dashboard(self.root,self.cfg)
        self.assertEqual(manager.find(job["id"])["status"],"running")
        manager.cancel(job["id"])
        self.wait_job(manager,job["id"])

    def test_installed_zipapp_serves_real_static_assets_from_another_directory(self):
        executable = self.base/"agentctl-installed"
        package_root = Path(__file__).resolve().parents[1]
        subprocess.run([sys.executable,str(package_root/"scripts/install.py"),"--output",str(executable)],check=True,capture_output=True)
        proc = subprocess.Popen([sys.executable,str(executable),"-C",str(self.root),"dashboard","--no-browser","--port","0"],
                                cwd=self.base,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            line=proc.stdout.readline().strip()
            self.assertIn("dashboard: http://127.0.0.1:",line)
            port=int(line.rsplit(":",1)[1])
            conn=http.client.HTTPConnection("127.0.0.1",port,timeout=3)
            for path,expected in [("/","/portraits.js"),("/app.js","loadEvents"),("/teams.js","/api/team/launch"),
                                  ("/portraits.js","agentPortrait"),("/style.css",".workspace-grid")]:
                conn.request("GET",path);response=conn.getresponse()
                self.assertEqual(response.status,200)
                self.assertIn(expected,response.read().decode())
            for path in ("/flux.webp", "/droplets.webp"):
                with self.subTest(path=path):
                    conn.request("GET", path)
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("Content-Type"), "image/webp")
                    image = response.read()
                    self.assertEqual(image[:4], b"RIFF")
                    self.assertEqual(image[8:12], b"WEBP")
            conn.close()
        finally:
            proc.send_signal(signal.SIGINT)
            try:
                _,err=proc.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill();proc.communicate();raise
        self.assertEqual(proc.returncode,0,err)

    def test_pre_cancelled_runner_never_launches_the_cli(self):
        cancelled=threading.Event();cancelled.set()
        rec=prepare_run(self.root,self.cfg,"SIM_WRITE must not execute")
        rec=execute_run(self.root,self.cfg,rec,stop=cancelled,quiet=True)
        self.assertEqual(rec["status"],"cancelled")
        self.assertNotIn("pid",rec)
        self.assertFalse((self.root/"result.py").exists())

    def test_restart_does_not_automatically_relaunch_interrupted_work(self):
        self.cfg["codex_bin"]="/missing"
        write_json(state_dir(self.root)/"dashboard-jobs.json",[
            {"id":"saved-job","status":"queued","created":"2026-01-01","name":"Old task","goal":"Do work","backend":"codex","permission":"read-only","run_id":None}
        ])
        manager=self.manager()
        self.assertEqual(manager.find("saved-job")["status"],"interrupted")
        self.assertIn("explicitly retry",manager.find("saved-job")["error"])

    def test_verification_uses_recorded_command_and_is_cancellable_at_shutdown(self):
        manager=self.manager()
        job=manager.enqueue({"goal":"Inspect"})
        self.wait_job(manager,job["id"])
        manager.check(job["id"], shlex_quote(sys.executable) + ' -c "import time; time.sleep(30)"')
        time.sleep(0.15)
        manager.close()
        self.assertFalse(manager.verifying)
        rec=manager.run_record(job["id"])
        self.assertEqual(rec["checks"][-1]["exit_code"],130)
        self.dashboard=None

    def test_artifact_traversal_and_bad_event_offsets_rejected(self):
        manager=self.manager()
        job=manager.enqueue({"goal":"Inspect"})
        self.wait_job(manager,job["id"])
        with self.assertRaises(UserError): manager.artifact(job["id"],"../../README.md")
        with self.assertRaises(UserError): manager.events(job["id"],-1)
        with self.assertRaises(UserError): manager.events(job["id"],10**10)
        with self.assertRaises(UserError): manager.enqueue({"goal":"Inspect","includes":["../outside"]})

    def test_http_requires_session_token_and_local_host_and_origin(self):
        manager=self.manager()
        server=DashboardServer(("127.0.0.1",0),manager)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        def request(path,headers=None,body=None):
            conn=http.client.HTTPConnection("127.0.0.1",server.server_port)
            conn.request("POST" if body is not None else "GET",path,body=json.dumps(body) if body is not None else None,headers=headers or {})
            response=conn.getresponse(); data=response.read();status=response.status;conn.close()
            return status,data
        self.assertEqual(request("/api/state")[0],403)
        self.assertEqual(request("/api/state",{"X-Agentctl-Token":server.token})[0],200)
        self.assertEqual(request("/api/state",{"X-Agentctl-Token":server.token,"Host":"evil.example"})[0],403)
        self.assertEqual(request("/api/jobs",{"X-Agentctl-Token":server.token,"Origin":"https://evil.example"},{"goal":"Bad origin"})[0],403)
        status,html=request("/")
        self.assertEqual(status,200);self.assertIn(server.token.encode(),html)
        self.assertEqual(request("/app.js")[0],200)
        self.assertEqual(request("/style.css")[0],200)
        status,data=request("/api/jobs",{"X-Agentctl-Token":server.token},{"goal":"HTTP task"})
        self.assertEqual(status,200)
        job=json.loads(data);self.assertEqual(self.wait_job(manager,job["id"])["status"],"completed")

    def test_connection_info_exposes_public_fields_without_credentials(self):
        (self.home/"config.toml").write_text('model_provider="localtest"\nmodel="test"\n[model_providers.localtest]\nbase_url="http://user:password@127.0.0.1:1234/v1?token=private"\napi_key="DO_NOT_RETURN"\n[mcp_servers.docs]\nurl="https://docs.example/mcp?api_key=private"\n')
        info=connection_info(self.root,"codex")
        encoded=json.dumps(info)
        self.assertEqual(info["endpoint"],"http://127.0.0.1:1234/v1")
        self.assertNotIn("password",encoded);self.assertNotIn("private",encoded);self.assertNotIn("DO_NOT_RETURN",encoded)
        self.assertEqual(info["integrations"][0]["state"],"configured, not probed")

    def test_socket_observation_is_scoped_to_owned_process(self):
        listener=socket.socket(); listener.bind(("127.0.0.1",0));listener.listen()
        client=socket.socket();client.connect(listener.getsockname())
        accepted,_=listener.accept()
        try:
            endpoints=process_connections(os.getpid())
            self.assertTrue(any(x["port"]==listener.getsockname()[1] for x in endpoints))
        finally:
            accepted.close();client.close();listener.close()


def shlex_quote(text):
    import shlex
    return shlex.quote(text)


if __name__ == "__main__":
    unittest.main()
