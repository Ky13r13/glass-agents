from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import copy
import io
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

from agentctl.agent_config import (initialize_agents, load_agents, prepare_feedback,
                                  starter_config, validate_config)
from agentctl.agent_runtime import AgentRuntime, BudgetReached, read_mail, send_mail
from agentctl.claude_bridge import sdk_call
from agentctl.common import Store, UserError, new_id, write_json
from agentctl.dashboard import Dashboard
from agentctl.feedback_demo import demo_driver, initialize_demo
from agentctl.runner import execute_run
from test_workflows import BaseWorkspace


async def accept_driver(runtime, actor, prompt, stack, reviewer):
    return {"decision":"accept","feedback":"Reviewed actual supplied evidence."} if reviewer else {"summary":"Reported result.","artifacts":[]}


class RuntimeWorkspace(BaseWorkspace):
    def make_runtime(self, driver=accept_driver, config=None):
        config = validate_config(config or starter_config())
        folder = self.root/".agentctl/runs"/new_id()
        folder.mkdir(parents=True)
        return AgentRuntime(self.root,self.cfg,folder,config,"default","Improve this project",driver=driver)

    def run_async(self, value):
        return asyncio.run(value)


class ManagedAgents(RuntimeWorkspace):
    def test_initialization_is_portable_idempotent_and_preserves_existing_guidance(self):
        guide=self.root/"AGENTS.md";original=guide.read_bytes()
        first=initialize_agents(self.root)
        config=json.loads(Path(first["path"]).read_text())
        config["agents"]["worker"]["instructions"]="Existing custom project role."
        write_json(Path(first["path"]),config)
        second=initialize_agents(self.root)
        self.assertFalse(second["created"])
        self.assertEqual(guide.read_bytes(),original)
        self.assertEqual(load_agents(self.root)[0]["agents"]["worker"]["instructions"],"Existing custom project role.")
        self.assertFalse(any("backtest" in str(v).lower() for v in config.values()))
        ignored=subprocess.run(["git","check-ignore",".agentctl/agents.json"],cwd=self.root,capture_output=True)
        self.assertNotEqual(ignored.returncode,0)

    def test_invalid_peers_scopes_commands_reviewer_and_external_refs_rejected(self):
        mutations=[
            lambda c:c["agents"]["worker"].update(peers=["missing"]),
            lambda c:c["agents"]["worker"].update(write_scope=["../escape"]),
            lambda c:c["agents"]["reviewer"].update(write_scope=["**"]),
            lambda c:c["agents"]["advisor"].update(backend="opencode"),
            lambda c:c["workflows"]["default"].update(max_iterations=True),
            lambda c:c["tools"].update(bad={"argv":"echo unsafe"}),
            lambda c:c["tools"].update(bad={"argv":["echo"],"input_schema":{"$ref":"https://example.com/schema"}}),
            lambda c:c["workflows"]["default"].update(required_checks=["missing"]),
        ]
        for change in mutations:
            config=starter_config();change(config)
            with self.subTest(config=config),self.assertRaises(UserError):
                validate_config(config)

    def test_real_check_feedback_triggers_revision_and_managed_peer_calls(self):
        other=self.base/"unrelated-node-project"
        initialize_demo(other)
        config,_=load_agents(other)
        folder=other/".agentctl/runs"/new_id();folder.mkdir(parents=True)
        runtime=AgentRuntime(other,self.cfg,folder,config,"default","Fix the note",driver=demo_driver)
        state=self.run_async(runtime.run())
        self.assertEqual(state["status"],"accepted")
        self.assertEqual(state["iteration"],2)
        self.assertEqual(state["agent_calls"],6)
        self.assertFalse(state["history"][0]["checks_pass"])
        self.assertTrue(state["history"][1]["checks_pass"])
        self.assertEqual((other/"note.txt").read_text(),"verified\n")
        self.assertTrue(any(m["from"]=="reviewer" and m["to"]=="worker" for m in read_mail(folder)))
        events=[json.loads(line) for line in (folder/"loop/events.jsonl").read_text().splitlines()]
        self.assertTrue(any(e["type"]=="agentctl.agent.started" and e["actor"]=="advisor" for e in events))
        self.assertTrue(any(e["type"]=="agentctl.revision.requested" for e in events))

    def test_failed_machine_check_cannot_be_overridden_by_reviewer_acceptance(self):
        config=starter_config();config["tools"]["fail"]={"argv":[sys.executable,"-c","raise SystemExit(7)"]}
        config["workflows"]["default"].update(required_checks=["fail"],max_iterations=1)
        runtime=self.make_runtime(config=config)
        state=self.run_async(runtime.run())
        self.assertEqual(state["status"],"limit_reached")
        self.assertEqual(state["history"][0]["review"]["decision"],"accept")
        self.assertFalse(state["history"][0]["accepted"])
        evidence=list(state["tool_results"].values())[0]
        self.assertEqual(evidence["exit_code"],7)

    def test_reviewer_rejection_stops_without_an_automatic_retry(self):
        async def driver(rt,actor,prompt,stack,reviewer):
            return {"decision":"reject","feedback":"Task lacks the required inputs."} if reviewer else {"summary":"Inspected.","artifacts":[]}
        state=self.run_async(self.make_runtime(driver).run())
        self.assertEqual(state["status"],"rejected")
        self.assertEqual(state["agent_calls"],2)

    def test_reviewer_receives_fresh_source_after_worker_changes(self):
        seen=[]
        async def driver(rt,actor,prompt,stack,reviewer):
            if reviewer:
                current=prompt.split("Current project evidence:",1)[1]
                seen.append(current)
                return {"decision":"accept","feedback":"Reviewed current README contents."}
            await rt.tool(actor,"write_file",{"path":"README.md","content":"# CURRENT_WORKER_OUTPUT_493\n"},stack)
            return {"summary":"Updated README.","artifacts":["README.md"]}
        state=self.run_async(self.make_runtime(driver).run())
        self.assertEqual(state["status"],"accepted")
        self.assertIn("CURRENT_WORKER_OUTPUT_493",seen[0])

    def test_iteration_and_agent_call_limits_are_enforced_in_code(self):
        async def revise(rt,actor,prompt,stack,reviewer):
            return {"decision":"revise","feedback":"Another pass is needed."} if reviewer else {"summary":"Draft.","artifacts":[]}
        config=starter_config();config["workflows"]["default"]["max_iterations"]=2
        state=self.run_async(self.make_runtime(revise,config).run())
        self.assertEqual(state["status"],"limit_reached");self.assertEqual(state["iteration"],2)
        config=starter_config();config["workflows"]["default"]["max_agent_calls"]=1
        state=self.run_async(self.make_runtime(config=config).run())
        self.assertEqual(state["status"],"limit_reached");self.assertEqual(state["agent_calls"],1)

    def test_tool_budget_and_unauthorized_peer_are_enforced(self):
        config=starter_config();config["workflows"]["default"]["max_tool_calls"]=1
        rt=self.make_runtime(config=config)
        self.run_async(rt.tool("worker","list_files",{}))
        with self.assertRaises(BudgetReached):
            self.run_async(rt.tool("worker","list_files",{}))
        rt=self.make_runtime()
        with self.assertRaises(UserError):
            self.run_async(rt.tool("reviewer","call_agent",{"agent":"worker","task":"Change files"}))
        with self.assertRaises(UserError):
            self.run_async(rt.tool("worker","call_agent",{"agent":"missing","task":"Do work"}))

    def test_recursive_delegation_and_depth_limit_are_blocked(self):
        rt=self.make_runtime()
        with self.assertRaisesRegex(UserError,"Recursive"):
            self.run_async(rt.call_agent("worker","Again",stack=("worker","advisor")))
        rt.workflow["max_depth"]=1
        with self.assertRaises(BudgetReached):
            self.run_async(rt.call_agent("advisor","Inspect",stack=("worker",)))

    def test_readers_cannot_write_and_file_tools_reject_escapes_and_symlinks(self):
        rt=self.make_runtime()
        with self.assertRaises(UserError):
            self.run_async(rt.tool("reviewer","write_file",{"path":"README.md","content":"changed"}))
        with self.assertRaises(UserError):
            self.run_async(rt.tool("worker","read_file",{"path":"../outside"}))
        outside=self.base/"outside.txt";outside.write_text("private")
        (self.root/"linked.txt").symlink_to(outside)
        with self.assertRaises(UserError):
            self.run_async(rt.tool("worker","read_file",{"path":"linked.txt"}))
        (self.root/".gitignore").write_text("private.txt\n")
        (self.root/"private.txt").write_text("private")
        with self.assertRaises(UserError):
            self.run_async(rt.tool("worker","read_file",{"path":"private.txt"}))
        helper=self.run_async(rt.tool("worker","read_file",{"path":".agentctl/REPO_MAP.md"}))
        self.assertIn("text",helper)

    def test_command_inputs_use_typed_stdin_and_literal_argument_arrays(self):
        config=starter_config()
        config["tools"]["echo"]={
            "argv":[sys.executable,"-c","import json,sys; x=json.load(sys.stdin); print(json.dumps({'argument':sys.argv[1], 'input':x['input'], 'context':x['context']}))","{input.text}"],
            "format":"json","context":{"fixed_protocol":"v1"},
            "input_schema":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"],"additionalProperties":False},
            "output_schema":{"type":"object","required":["argument","input","context"]},
        }
        config["agents"]["worker"]["project_tools"]=["echo"]
        rt=self.make_runtime(config=config)
        value="$(touch SHOULD_NOT_EXIST); echo shell"
        result=self.run_async(rt.tool("worker","project_echo",{"text":value}))
        self.assertEqual(result["status"],"passed")
        self.assertEqual(result["result"]["argument"],value)
        self.assertEqual(result["result"]["context"]["fixed_protocol"],"v1")
        self.assertFalse((self.root/"SHOULD_NOT_EXIST").exists())
        with self.assertRaises(UserError):
            self.run_async(rt.tool("worker","project_echo",{"text":4}))
        with self.assertRaises(UserError):
            self.run_async(rt.tool("worker","project_echo",{"text":"hello","context":{"fixed_protocol":"changed"}}))

    def test_command_output_schema_failure_is_recorded_as_failed_evidence(self):
        config=starter_config()
        config["tools"]["bad"]={"argv":[sys.executable,"-c","print('{}')"],"format":"json",
                                "output_schema":{"type":"object","required":["measured_metric"]}}
        config["agents"]["worker"]["project_tools"]=["bad"]
        rt=self.make_runtime(config=config)
        evidence=self.run_async(rt.tool("worker","project_bad",{}))
        self.assertEqual(evidence["status"],"failed")
        self.assertEqual(evidence["exit_code"],0)
        self.assertIn("measured_metric",evidence["error"])

    def test_source_changes_by_readonly_project_command_fail_integrity_gate(self):
        config=starter_config()
        config["tools"]["bad"]={"argv":[sys.executable,"-c","from pathlib import Path; Path('README.md').write_text('changed')"]}
        config["agents"]["reviewer"]["project_tools"]=["bad"]
        rt=self.make_runtime(config=config)
        with self.assertRaisesRegex(UserError,"out-of-scope"):
            self.run_async(rt.tool("reviewer","project_bad",{}))
        self.assertTrue(rt.state["integrity_failure"])

    def test_command_timeout_and_cancellation_terminate_real_subprocesses(self):
        config=starter_config()
        config["tools"]["slow"]={"argv":[sys.executable,"-c","import time; time.sleep(30)"],"timeout_seconds":1}
        config["agents"]["worker"]["project_tools"]=["slow"]
        rt=self.make_runtime(config=config)
        started=time.monotonic()
        result=self.run_async(rt.tool("worker","project_slow",{}))
        self.assertEqual(result["status"],"failed")
        self.assertIn("time limit",result["error"])
        self.assertLess(time.monotonic()-started,4)
        config["tools"]["slow"]["timeout_seconds"]=30
        rt=self.make_runtime(config=config)
        async def cancel():
            async def stopper():
                await asyncio.sleep(.1);rt.stop.set()
            stopper_task=asyncio.create_task(stopper())
            try:
                with self.assertRaises(UserError):
                    await rt.tool("worker","project_slow",{})
            finally: await stopper_task
        started=time.monotonic();self.run_async(cancel())
        self.assertLess(time.monotonic()-started,3)

    def test_reviewer_source_mutation_and_invalid_json_decisions_never_accept(self):
        async def mutating(rt,actor,prompt,stack,reviewer):
            if reviewer:
                (rt.root/"README.md").write_text("concurrent mutation")
                return {"decision":"accept","feedback":"Claims success"}
            return {"summary":"Draft","artifacts":[]}
        state=self.run_async(self.make_runtime(mutating).run())
        self.assertEqual(state["status"],"failed")
        self.assertTrue(state["integrity_failure"])
        async def invalid(rt,actor,prompt,stack,reviewer):
            return {"decision":"looks good"} if reviewer else {"summary":"Draft","artifacts":[]}
        state=self.run_async(self.make_runtime(invalid).run())
        self.assertEqual(state["status"],"failed")

    def test_prepared_run_rejects_mutated_source_and_frozen_agent_definitions(self):
        initialize_agents(self.root)
        record=prepare_feedback(self.root,self.cfg,"Inspect project")
        path=Path(record["agents_config_source"])
        config=json.loads(path.read_text());config["agents"]["worker"]["instructions"]="Changed"
        write_json(path,config)
        with self.assertRaisesRegex(UserError,"definitions changed"):
            execute_run(self.root,self.cfg,record,quiet=True)
        record=prepare_feedback(self.root,self.cfg,"Inspect project")
        Path(record["folder"],"agents-config.json").write_text("{}")
        with self.assertRaisesRegex(UserError,"modified"):
            execute_run(self.root,self.cfg,record,quiet=True)

    def test_cli_prepare_validation_and_messages_do_not_launch_a_model(self):
        self.assertEqual(self.cli("agents","init")[0],0)
        with patch("agentctl.runner.stream_process",side_effect=AssertionError("must not execute")):
            self.assertEqual(self.cli("agents","validate")[0],0)
            rc,output,err=self.cli("agents","run","Inspect the project","--prepare-only")
        self.assertEqual(rc,0,err)
        record=Store(self.root).get()
        rc,output,err=self.cli("agents","message",record["id"],"--to","advisor","--text","Focus on interfaces")
        self.assertEqual(rc,0,err)
        self.assertEqual(read_mail(record["folder"],"advisor")[0]["body"],"Focus on interfaces")
        self.assertEqual(self.cli("agents","messages",record["id"],"--to","advisor")[0],0)
        self.assertNotEqual(self.cli("agents","message",record["id"],"--to","unknown","--text","x")[0],0)

    def test_start_context_supports_claude_sdk_without_launching_it(self):
        initialize_agents(self.root)
        rc,output,err=self.cli("start","Inspect the project","--backend","claude-sdk","--prepare-only")
        self.assertEqual(rc,0,err)
        record=Store(self.root).get()
        self.assertEqual(record["agent_workflow"],"default")
        self.assertTrue(Path(record["folder"],"agents-config.json").exists())

    def test_cli_demo_runs_real_isolated_worker_and_records_feedback_outcome(self):
        out=self.base/"kept-demo"
        rc,output,err=self.cli("agents","demo","--output",str(out))
        self.assertEqual(rc,0,err+"\n"+output)
        record=Store(out).get()
        self.assertEqual(record["loop_outcome"],"accepted")
        self.assertEqual(record["loop_iteration"],2)
        self.assertEqual(len(record["agent_activity"]),6)
        self.assertEqual([c["exit_code"] for c in record["checks"]],[1,0])
        self.assertEqual((out/"note.txt").read_text(),"verified\n")

    def test_native_codex_peer_is_managed_through_existing_runner(self):
        config=starter_config()
        config["agents"]["advisor"]["backend"]="codex"
        rt=self.make_runtime(driver=None,config=config)
        result=self.run_async(rt.call_agent("advisor","Inspect export interface"))
        self.assertIn("Inspected",result["summary"])
        records=Store(self.root).list()
        self.assertTrue(any(r["backend"]=="codex" for r in records))
        self.assertIn("partial",rt.state["cost_coverage"])

    def test_dashboard_exposes_managed_definitions_team_and_artifacts(self):
        initialize_agents(self.root)
        config,_=load_agents(self.root)
        manager=Dashboard(self.root,self.cfg)
        try:
            self.assertIn("default",manager.state()["managed"]["workflows"])
            with manager.guard:
                job=manager.enqueue({"goal":"Test pending managed workflow","backend":"claude-sdk","permission":"workspace-write"})
                manager.cancel(job["id"])
            self.assertEqual(manager.find(job["id"])["agent_workflow"],"default")
            with self.assertRaises(UserError):
                manager.enqueue({"goal":"Bad reader","backend":"claude-sdk","permission":"read-only"})
        finally: manager.close()

    def test_dashboard_observes_cli_runs_and_queues_operator_messages(self):
        initialize_agents(self.root)
        record=prepare_feedback(self.root,self.cfg,"Work launched from a terminal")
        record.update(status="running",agent_activity={"call-1":{"name":"worker","backend":"claude-sdk","status":"running"}})
        Store(self.root).save(record)
        manager=Dashboard(self.root,self.cfg)
        try:
            state=manager.state()
            observed=next(j for j in state["jobs"] if j["id"]==record["id"])
            self.assertTrue(observed["external"])
            self.assertEqual(observed["run"]["agent_activity"]["call-1"]["name"],"worker")
            manager.agent_message(record["id"],"worker","Focus on the acceptance checks.")
            self.assertEqual(read_mail(record["folder"],"worker")[0]["body"],"Focus on the acceptance checks.")
            self.assertIn("worker",manager.artifact(record["id"],"loop/messages.jsonl"))
        finally: manager.close()

    def test_concurrent_first_store_initialization_preserves_all_records(self):
        from agentctl.common import now
        root=self.base/"fresh-store";root.mkdir()
        def create(n):
            store=Store(root);store.save({"id":str(n),"created":now(),"value":n})
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(create,range(30)))
        self.assertEqual(len(Store(root).list(100)),30)

    def test_installed_zipapp_runs_managed_demo_outside_source_directory(self):
        destination=self.base/"installed-agentctl"
        source=Path(__file__).resolve().parents[1]
        subprocess.run([sys.executable,str(source/"scripts/install.py"),"--output",str(destination)],check=True,capture_output=True)
        result=subprocess.run([sys.executable,str(destination),"agents","demo"],cwd=self.base,capture_output=True,text=True,timeout=12)
        self.assertEqual(result.returncode,0,result.stderr+"\n"+result.stdout)
        self.assertIn("Outcome: accepted",result.stdout)


def fake_sdk(behavior=None):
    module=types.ModuleType("claude_agent_sdk")
    class Options:
        def __init__(self,**kwargs): self.__dict__.update(kwargs)
    class Result:
        def __init__(self,**kwargs):
            self.__dict__.update({"subtype":"success","is_error":False,"structured_output":{"summary":"SDK result","artifacts":[]},
                                  "usage":{"input_tokens":17,"output_tokens":4},"total_cost_usd":.03,"session_id":"fixture-sdk-session",**kwargs})
    class Client:
        last_options=None
        def __init__(self,options): self.options=options;Client.last_options=options
        async def __aenter__(self): return self
        async def __aexit__(self,*args): return None
        async def query(self,prompt): self.prompt=prompt
        async def interrupt(self): pass
        async def receive_response(self):
            if behavior: await behavior(self)
            yield Result()
    def decorator(name,description,schema):
        def bind(handler): return types.SimpleNamespace(name=name,description=description,input_schema=schema,handler=handler)
        return bind
    module.ClaudeAgentOptions=Options;module.ClaudeSDKClient=Client;module.ResultMessage=Result
    module.HookMatcher=Options;module.PermissionResultAllow=Options;module.PermissionResultDeny=Options
    module.tool=decorator
    module.create_sdk_mcp_server=lambda **kwargs:{"type":"sdk",**kwargs}
    return module


class SDKBridge(RuntimeWorkspace):
    # Inherit workspace setup only; keep these bridge checks separate from provider benchmarks.
    def test_bridge_registers_typed_tools_and_allows_internal_structured_output(self):
        seen={}
        async def behavior(client):
            opts=client.options;seen["options"]=opts
            before=opts.hooks["PreToolUse"][0].hooks[0]
            self.assertEqual(await before({"tool_name":"StructuredOutput"},None,None),{})
            denied=await before({"tool_name":"Bash"},None,None)
            self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"],"deny")
            handlers={t.name:t for t in opts.mcp_servers["agentctl"]["tools"]}
            result=await handlers["read_file"].handler({"path":"README.md"})
            self.assertIn("Example",result["content"][0]["text"])
            failed=await handlers["read_file"].handler({"path":"../escape"})
            self.assertTrue(failed["is_error"])
        module=fake_sdk(behavior)
        rt=self.make_runtime(driver=None)
        with patch.dict(sys.modules,{"claude_agent_sdk":module}):
            result=self.run_async(sdk_call(rt,"worker","Inspect",("worker",),{"type":"object"}))
        self.assertEqual(result["summary"],"SDK result")
        self.assertEqual(seen["options"].tools,[])
        self.assertEqual(seen["options"].setting_sources,[])
        self.assertTrue(seen["options"].strict_mcp_config)
        self.assertEqual(rt.state["usage"]["input_tokens"],17)
        self.assertEqual(rt.state["reported_cost_usd"],.03)

    def test_bridge_rejects_reported_errors_even_with_success_subtype(self):
        module=fake_sdk()
        original=module.ResultMessage
        class Failed(original):
            def __init__(self,**kwargs): super().__init__(is_error=True,result="provider failure",**kwargs)
        # Replace the constructor used by the fake stream as well.
        async def response(self): yield Failed()
        module.ClaudeSDKClient.receive_response=response
        module.ResultMessage=Failed
        rt=self.make_runtime(driver=None)
        with patch.dict(sys.modules,{"claude_agent_sdk":module}),self.assertRaisesRegex(UserError,"did not complete"):
            self.run_async(sdk_call(rt,"worker","Inspect",("worker",),{"type":"object"}))

    @unittest.skipUnless(importlib.util.find_spec("claude_agent_sdk"),"Optional real SDK is not installed")
    def test_real_sdk_options_mcp_tools_and_stream_normalization_without_model_calls(self):
        import claude_agent_sdk as sdk
        captured={}
        original=sdk.create_sdk_mcp_server
        def create(**kwargs):
            captured["tools"]={t.name:t for t in kwargs["tools"]}
            server=original(**kwargs)
            self.assertEqual(server["type"],"sdk")
            return server
        class Client:
            def __init__(self,options): captured["options"]=options
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            async def query(self,prompt): pass
            async def interrupt(self): pass
            async def receive_response(self):
                value=await captured["tools"]["read_file"].handler({"path":"README.md"})
                if "content" not in value: raise AssertionError("Tool handler returned no content")
                yield sdk.AssistantMessage(content=[sdk.TextBlock(text="Public agent update."),
                    sdk.ThinkingBlock(thinking="SHOULD_NOT_LOG",signature="private")],model="fixture")
                yield sdk.ResultMessage(subtype="success",duration_ms=1,duration_api_ms=1,is_error=False,
                    num_turns=1,session_id="fixture",usage={"input_tokens":1},total_cost_usd=0,
                    result="done",structured_output={"summary":"Actual SDK API smoke check.","artifacts":[]})
        rt=self.make_runtime(driver=None)
        with patch.object(sdk,"create_sdk_mcp_server",side_effect=create),patch.object(sdk,"ClaudeSDKClient",Client):
            result=self.run_async(sdk_call(rt,"worker","Inspect",("worker",),{"type":"object"}))
        self.assertIn("Actual SDK",result["summary"])
        self.assertTrue(captured["options"].strict_mcp_config)
        self.assertNotIn("SHOULD_NOT_LOG",(rt.loop_folder/"events.jsonl").read_text())


if __name__=="__main__":
    unittest.main()
