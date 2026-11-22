"""Swarm (parallel candidates in Git worktrees) and single-agent ask workflows.

Scripted drivers stand in for models; git, worktrees, checks and integration are real.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys
import time

from agentctl.agent_config import ask_config, prepare_feedback, run_succeeded, starter_config, validate_config
from agentctl.agent_runtime import AgentRuntime, send_mail
from agentctl.common import Store, UserError, new_id, write_json
from agentctl.runner import execute_run
from agentctl.swarm import SwarmRuntime
from agentctl import worktree
from test_workflows import BaseWorkspace

ORIGINAL = "def export_csv(rows):\n    return ','.join(rows)\n"
CHECK = "import pathlib,sys; sys.exit(0 if 'GOOD' in pathlib.Path('src/export.py').read_text() else 1)"


def swarm_config(**workflow):
    config = starter_config()
    writer = {"backend": "claude-sdk", "write_scope": ["src/**"], "peers": [], "project_tools": [], "instructions": "Candidate."}
    config["agents"] = {"a": dict(writer), "b": dict(writer),
                        "critic": {"backend": "claude-sdk", "write_scope": [], "peers": [], "project_tools": [], "instructions": "Critic."}}
    config["tools"]["check"] = {"argv": [sys.executable, "-c", CHECK]}
    config["workflows"] = {"default": {"type": "swarm", "candidates": ["a", "b"], "critics": ["critic"],
                                       "required_checks": ["check"], "timeout_seconds": 120, "max_agent_calls": 20, **workflow}}
    return config


def scripted(contents, critic=None, seen=None):
    """contents: {agent: str or callable(round_number) -> str or None}."""
    async def driver(rt, actor, prompt, stack, reviewer):
        if seen is not None:
            seen.setdefault(actor, []).append(prompt)
        if reviewer:
            text = (await rt.tool(actor, "read_file", {"path": "src/export.py"}, stack))["text"]
            if critic:
                return critic(text)
            good = "GOOD" in text
            return {"decision": "accept" if good else "revise", "score": 8 if good else 2, "feedback": "Judged the file."}
        round_number = int(prompt.split("Your task: Round ", 1)[1].split(".", 1)[0])
        content = contents[actor](round_number) if callable(contents[actor]) else contents[actor]
        if content is not None:
            await rt.tool(actor, "write_file", {"path": "src/export.py", "content": content}, stack)
        return {"summary": f"{actor} round {round_number}", "artifacts": ["src/export.py"]}
    return driver


class SwarmWorkspace(BaseWorkspace):
    def swarm(self, driver, **workflow):
        folder = self.root / ".agentctl/runs" / new_id()
        folder.mkdir(parents=True)
        return SwarmRuntime(self.root, self.cfg, folder, validate_config(swarm_config(**workflow)), "default",
                            "Improve the CSV export", driver=driver)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, check=True).stdout


class Worktrees(SwarmWorkspace):
    def test_baseline_captures_uncommitted_and_untracked_files_without_touching_user_state(self):
        subprocess.run(["git", "add", "README.md"], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], cwd=self.root, check=True)
        (self.root / "README.md").write_text("# Edited but not committed\n")
        status_before, head_before = self.git("status", "--porcelain"), self.git("rev-parse", "HEAD")
        base = worktree.baseline(self.root)
        self.assertEqual(self.git("status", "--porcelain"), status_before)
        self.assertEqual(self.git("rev-parse", "HEAD"), head_before)
        project = worktree.create(self.root, base, self.root / ".agentctl/worktrees/t/c1")
        self.assertEqual((project / "README.md").read_text(), "# Edited but not committed\n")
        self.assertEqual((project / "src/export.py").read_text(), ORIGINAL)
        self.assertFalse((project / ".agentctl/config.toml").exists())
        (project / "src/export.py").write_text("changed\n")
        head = worktree.commit(project, "candidate")
        self.assertEqual(worktree.changed_files(project, base, head), ["src/export.py"])
        self.assertEqual((self.root / "src/export.py").read_text(), ORIGINAL)
        worktree.remove(self.root, self.root / ".agentctl/worktrees/t/c1")
        self.assertEqual(len(self.git("worktree", "list").splitlines()), 1)

    def test_non_git_projects_get_an_actionable_error(self):
        plain = self.base / "plain"
        plain.mkdir()
        with self.assertRaisesRegex(UserError, "git init"):
            worktree.baseline(plain)


class SwarmValidation(SwarmWorkspace):
    def test_invalid_swarm_definitions_are_rejected(self):
        mutations = [
            lambda c: c["agents"]["critic"].update(write_scope=["**"]),
            lambda c: c["agents"]["a"].update(backend="local", write_scope=[]),
            lambda c: c["agents"]["b"].update(write_scope=[]),
            lambda c: c["workflows"]["default"].update(critics=["a"]),
            lambda c: c["workflows"]["default"].update(replicas=9),
            lambda c: c["workflows"]["default"].update(min_accepts=2),
            lambda c: c["workflows"]["default"].update(mode="merge_everything"),
            lambda c: c["workflows"]["default"].update(candidates=["a", "a"]),
            lambda c: c["workflows"]["default"].update(type="mystery"),
            lambda c: c["workflows"]["default"].update(apply_winner="yes"),
        ]
        for change in mutations:
            config = swarm_config()
            change(config)
            with self.subTest(config=config["workflows"]), self.assertRaises(UserError):
                validate_config(config)

    def test_cli_replicas_and_rounds_override_only_swarm_workflows(self):
        write_json(self.root / ".agentctl/agents.json", swarm_config())
        record = prepare_feedback(self.root, self.cfg, "goal", replicas=3, rounds=5)
        frozen = json.loads(Path(record["folder"], "agents-config.json").read_text())
        self.assertEqual((frozen["workflows"]["default"]["replicas"], frozen["workflows"]["default"]["max_rounds"]), (3, 5))
        self.assertEqual(record["workflow_type"], "swarm")
        write_json(self.root / ".agentctl/agents.json", starter_config())
        with self.assertRaisesRegex(UserError, "swarm"):
            prepare_feedback(self.root, self.cfg, "goal", replicas=3)


class SwarmRuns(SwarmWorkspace):
    def test_candidates_work_in_isolation_and_the_checked_accepted_leader_wins(self):
        rt = self.swarm(scripted({"a": "GOOD = 'a'\n", "b": "bad = 'b'\n"}))
        state = asyncio.run(rt.run())
        self.assertEqual(state["status"], "accepted", state.get("error"))
        self.assertEqual(state["winner"]["slot"], "a")
        board = {row["slot"]: row for row in state["rounds"][0]["leaderboard"]}
        self.assertTrue(board["a"]["checks_pass"])
        self.assertFalse(board["b"]["checks_pass"])
        self.assertFalse(board["b"]["finalist"], "Candidates failing required checks are not sent to critics.")
        self.assertEqual((self.root / "src/export.py").read_text(), ORIGINAL, "apply_winner is off by default")
        self.assertIn("GOOD = 'a'", Path(state["winner"]["patch"]).read_text())
        self.assertIn(state["winner"]["branch"], self.git("branch", "--list", "agentctl/swarm/*"))
        self.assertEqual(len(self.git("worktree", "list").splitlines()), 1, "Candidate worktrees are removed.")
        self.assertIn("agents apply", (rt.folder / "answer.md").read_text())

    def test_candidates_run_concurrently_up_to_max_parallel(self):
        for limit, expected in ((1, 1), (4, 4)):
            active, peak = [0], [0]
            async def driver(rt, actor, prompt, stack, reviewer):
                if reviewer:
                    return {"decision": "accept", "score": 7, "feedback": "ok"}
                active[0] += 1
                peak[0] = max(peak[0], active[0])
                await asyncio.sleep(0.3)
                active[0] -= 1
                await rt.tool(actor, "write_file", {"path": "src/export.py", "content": "GOOD\n"}, stack)
                return {"summary": "done", "artifacts": []}
            with self.subTest(max_parallel=limit):
                state = asyncio.run(self.swarm(driver, replicas=2, max_parallel=limit).run())
                self.assertEqual(state["status"], "accepted", state.get("error"))
                self.assertEqual(len(state["candidates"]), 4)
                self.assertEqual(peak[0], expected)

    def test_failed_checks_block_acceptance_and_report_best_unaccepted_candidate(self):
        always_accept = lambda text: {"decision": "accept", "score": 10, "feedback": "Looks great."}
        state = asyncio.run(self.swarm(scripted({"a": "nope\n", "b": "still no\n"}, critic=always_accept), max_rounds=2).run())
        self.assertEqual(state["status"], "limit_reached")
        self.assertEqual(state["round"], 2)
        self.assertFalse(state["winner"]["accepted"])
        self.assertEqual(state["agent_calls"], 4, "No critic calls without passing checks.")

    def test_feedback_rounds_carry_leader_and_critic_feedback_and_adopt_leader_resets(self):
        seen = {}
        revise = lambda text: {"decision": "revise", "score": 6 if "GOOD" in text else 1, "feedback": "CRITIC_NOTE_17"}
        driver = scripted({"a": lambda r: "GOOD = 'a'\n" if r == 1 else None, "b": lambda r: "bad\n" if r == 1 else None},
                          critic=revise, seen=seen)
        rt = self.swarm(driver, max_rounds=2, mode="adopt_leader")
        state = asyncio.run(rt.run())
        self.assertEqual(state["status"], "limit_reached")
        round_two_b = [p for p in seen["b"] if "Your task: Round 2." in p][0]
        self.assertIn("Current leading candidate: a", round_two_b)
        self.assertIn("CRITIC_NOTE_17", round_two_b)
        self.assertIn("reset to the leader's version", round_two_b)
        self.assertTrue(state["rounds"][1]["leaderboard"][1]["checks_pass"], "b now holds the leader's passing code")
        self.assertEqual(state["rounds"][1]["leader"], "a", "Ties keep the incumbent leader")

    def test_scope_violations_outside_scanned_text_drop_the_candidate(self):
        async def driver(rt, actor, prompt, stack, reviewer):
            if reviewer:
                return {"decision": "accept", "score": 9, "feedback": "ok"}
            await rt.tool(actor, "write_file", {"path": "src/export.py", "content": "GOOD\n"}, stack)
            if actor == "b":
                (rt.root / "blob.bin").write_bytes(b"\x00binary outside scope")
            return {"summary": "done", "artifacts": []}
        state = asyncio.run(self.swarm(driver).run())
        self.assertEqual(state["candidates"]["b"]["status"], "failed")
        self.assertIn("blob.bin", state["candidates"]["b"]["reason"])
        self.assertEqual(state["winner"]["slot"], "a")

    def test_shared_cost_limit_stops_the_whole_swarm(self):
        async def driver(rt, actor, prompt, stack, reviewer):
            rt.state["reported_cost_usd"] += 3.0
            rt.state["cost_reports"] += 1
            return {"decision": "revise", "score": 1, "feedback": "x"} if reviewer else {"summary": "spent", "artifacts": []}
        state = asyncio.run(self.swarm(driver, max_reported_cost_usd=5.0, max_rounds=3).run())
        self.assertEqual(state["status"], "limit_reached")
        self.assertIn("cost", state["error"])
        self.assertEqual(state["round"], 1)

    def test_operator_messages_reach_candidates(self):
        seen = {}
        rt = self.swarm(scripted({"a": "GOOD\n", "b": "GOOD\n"}, seen=seen))
        send_mail(rt.folder, rt.config["agents"], "operator", "b", "OPERATOR_HINT_42")
        asyncio.run(rt.run())
        self.assertIn("OPERATOR_HINT_42", seen["b"][0])
        self.assertNotIn("OPERATOR_HINT_42", seen["a"][0])

    def test_apply_winner_integrates_and_reruns_checks_on_the_real_checkout(self):
        state = asyncio.run(self.swarm(scripted({"a": "GOOD = 'a'\n", "b": "bad\n"}), apply_winner=True).run())
        self.assertEqual(state["status"], "accepted", state.get("error"))
        self.assertTrue(state["applied"]["applied"])
        self.assertTrue(state["applied"]["checks_pass"])
        self.assertEqual((self.root / "src/export.py").read_text(), "GOOD = 'a'\n")


class SwarmCommands(SwarmWorkspace):
    def finished_swarm(self, contents, **workflow):
        write_json(self.root / ".agentctl/agents.json", swarm_config(**workflow))
        record = prepare_feedback(self.root, self.cfg, "Improve the CSV export")
        config = json.loads(Path(record["folder"], "agents-config.json").read_text())
        rt = SwarmRuntime(self.root, self.cfg, record["folder"], validate_config(config), "default", record["goal"],
                          driver=scripted(contents))
        state = asyncio.run(rt.run())
        record.update(status="completed", loop_outcome=state["status"])
        Store(self.root).save(record)
        return record, state

    def test_apply_refuses_changed_checkouts_and_unaccepted_candidates_then_applies_once(self):
        record, state = self.finished_swarm({"a": "GOOD = 'a'\n", "b": "bad\n"})
        self.assertEqual(state["status"], "accepted")
        (self.root / "README.md").write_text("# Concurrent manual edit\n")
        rc, out, err = self.cli("agents", "apply", record["id"])
        self.assertEqual(rc, 2)
        self.assertIn("changed since the swarm started", err)
        (self.root / "README.md").write_text("# Example\nCSV export lives in src/export.py.\n")
        rc, out, err = self.cli("agents", "apply", record["id"], "--candidate", "b")
        self.assertEqual(rc, 2)
        self.assertIn("--allow-unaccepted", err)
        rc, out, err = self.cli("agents", "apply", record["id"])
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.root / "src/export.py").read_text(), "GOOD = 'a'\n")
        saved = Store(self.root).get(record["id"])
        self.assertEqual([c["origin"] for c in saved["checks"]], ["swarm_apply"])
        self.assertEqual(saved["checks"][0]["exit_code"], 0)
        rc, out, err = self.cli("agents", "apply", record["id"])
        self.assertIn("already applied", err)

    def test_cli_swarm_demo_runs_offline_and_integrates_the_winner(self):
        out = self.base / "swarm-demo"
        rc, output, err = self.cli("agents", "demo", "--swarm", "--output", str(out))
        self.assertEqual(rc, 0, err + "\n" + output)
        record = Store(out).get()
        self.assertEqual((record["loop_outcome"], record["swarm_winner"]), ("accepted", "beta"))
        self.assertEqual((out / "note.txt").read_text(), "verified concise\n")
        self.assertEqual([c["exit_code"] for c in record["checks"]], [0], "Only checks on the real checkout are evidence.")


class AskAgent(SwarmWorkspace):
    def base_config(self):
        config = starter_config()
        config["agents"]["worker"]["write_scope"] = ["src/**", "docs/**"]
        config["agents"]["writer_peer"] = {"backend": "claude-sdk", "write_scope": ["**"], "instructions": "Peer."}
        config["agents"]["worker"]["peers"] = ["advisor", "writer_peer"]
        config["tools"]["check"] = {"argv": [sys.executable, "-c", CHECK]}
        return validate_config(config)

    def test_ask_can_only_narrow_permissions(self):
        config = self.base_config()
        narrowed = ask_config(config, "worker", folder="src")
        self.assertEqual(narrowed["agents"]["worker"]["write_scope"], ["src/**"])
        self.assertEqual(narrowed["agents"]["writer_peer"]["write_scope"], ["src/**"])
        self.assertIn("work inside the project folder src/", narrowed["agents"]["worker"]["instructions"])
        self.assertEqual(narrowed["workflows"]["agentctl-ask"]["type"], "single")
        read_only = ask_config(config, "worker", read_only=True)
        self.assertEqual(read_only["agents"]["worker"]["write_scope"], [])
        self.assertNotIn("writer_peer", read_only["agents"]["worker"]["peers"])
        for kwargs in ({"scopes": ["tests/**"]}, {"folder": "../elsewhere"}, {"scopes": ["/etc/passwd"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(UserError):
                ask_config(config, "worker", **kwargs)
        with self.assertRaisesRegex(UserError, "read-only"):
            ask_config(config, "advisor", scopes=["src/**"])
        with self.assertRaisesRegex(UserError, "Unknown managed agent"):
            ask_config(config, "nobody")

    def test_single_agent_runs_once_with_checks_and_no_reviewer(self):
        for content, expected in (("GOOD\n", "completed"), ("bad\n", "checks_failed")):
            config = ask_config(self.base_config(), "worker", folder="src", checks=["check"])
            calls = []
            async def driver(rt, actor, prompt, stack, reviewer, content=content):
                calls.append((actor, reviewer))
                await rt.tool(actor, "write_file", {"path": "src/export.py", "content": content}, stack)
                return {"summary": "Wrote export.", "artifacts": []}
            folder = self.root / ".agentctl/runs" / new_id()
            folder.mkdir(parents=True)
            rt = AgentRuntime(self.root, self.cfg, folder, config, "agentctl-ask", "Fix export", driver=driver)
            with self.subTest(content=content):
                state = asyncio.run(rt.run_single())
                self.assertEqual(state["status"], expected)
                self.assertEqual(calls, [("worker", False)])
                self.assertIn("not a correctness guarantee", (folder / "answer.md").read_text())

    def test_single_agent_cannot_write_outside_the_requested_folder(self):
        config = ask_config(self.base_config(), "worker", folder="docs")
        async def driver(rt, actor, prompt, stack, reviewer):
            await rt.tool(actor, "write_file", {"path": "src/export.py", "content": "x\n"}, stack)
            return {"summary": "nope", "artifacts": []}
        folder = self.root / ".agentctl/runs" / new_id()
        folder.mkdir(parents=True)
        state = asyncio.run(AgentRuntime(self.root, self.cfg, folder, config, "agentctl-ask", "x", driver=driver).run_single())
        self.assertEqual(state["status"], "failed")
        self.assertIn("write scope", state["error"])
        self.assertEqual((self.root / "src/export.py").read_text(), ORIGINAL)

    def test_ask_with_a_native_codex_agent_runs_through_the_managed_runner(self):
        config = self.base_config()
        config["agents"]["coder"] = {"backend": "codex", "write_scope": ["src/**"], "instructions": "Code."}
        write_json(self.root / ".agentctl/agents.json", config)
        record = prepare_feedback(self.root, self.cfg, "Inspect the export", ask={"agent": "coder", "read_only": True})
        record = execute_run(self.root, self.cfg, record, quiet=True)
        self.assertEqual((record["status"], record["loop_outcome"]), ("completed", "completed"), record.get("error"))
        self.assertTrue(run_succeeded(record))
        peers = [r for r in Store(self.root).list() if r["backend"] == "codex"]
        self.assertEqual(peers[0]["permission"], "read-only")

    def test_cli_ask_prepares_without_launching_and_rejects_missing_folders(self):
        write_json(self.root / ".agentctl/agents.json", self.base_config())
        rc, out, err = self.cli("agents", "ask", "worker", "Tidy the export", "--in", "src", "--prepare-only")
        self.assertEqual(rc, 0, err)
        record = Store(self.root).get()
        frozen = json.loads(Path(record["folder"], "agents-config.json").read_text())
        self.assertEqual(frozen["agents"]["worker"]["write_scope"], ["src/**"])
        self.assertEqual(record["agent_workflow"], "agentctl-ask")
        rc, out, err = self.cli("agents", "ask", "worker", "x", "--in", "missing")
        self.assertEqual(rc, 2)
        self.assertIn("does not exist", err)
