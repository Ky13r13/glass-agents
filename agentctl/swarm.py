"""Parallel candidate agents in isolated Git worktrees, ranked by required checks and read-only critics.

Each round, every active candidate works in its own worktree. Required checks run in that
worktree, read-only critics judge the best-checked finalists, and the leader's result plus
critic feedback is sent back to every candidate. The run ends when one leader passes its
checks, has enough critic accepts, and holds the lead for the configured number of rounds,
or when a limit is reached. The user's checkout is only changed by an explicit integration
step that refuses to run if the project changed since the swarm started.
"""
from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
from pathlib import Path
import shutil
import threading
import time

from . import worktree
from .agent_runtime import AgentRuntime, BudgetReached, clean, read_mail
from .common import UserError, new_id, now, state_dir, write_json, write_text
from .project import redact, snapshot

CRITIC_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"decision": {"type": "string", "enum": ["accept", "revise", "reject"]},
                   "score": {"type": "number", "minimum": 0, "maximum": 10},
                   "feedback": {"type": "string"}},
    "required": ["decision", "score", "feedback"],
}
LOCAL = {"local", "codex-local"}
NOTE = ("Acceptance means the leading candidate passed the configured checks, received enough critic accepts "
        "and held the lead for the configured rounds. It is not a general correctness guarantee.")


def in_scope(path, scopes):
    return any(fnmatch.fnmatch(path, s) or path.startswith(s.rstrip("/") + "/") for s in scopes)


def check_summary(checks):
    result = []
    for c in checks:
        output = c.get("result")
        if output is not None and not isinstance(output, str):
            output = json.dumps(output)
        result.append({"tool": c["tool"], "status": c["status"], "exit_code": c["exit_code"],
                       "error": c.get("error"), "output": output[-1500:] if output else output})
    return result


def checks_current(checks, fingerprint):
    return all(c["status"] == "passed" and c["fingerprint"] == fingerprint and not c["source_changed_during_check"] for c in checks)


def rank_key(entry):
    score = entry["score"] if entry["score"] is not None else -1.0
    return (entry["checks_pass"], entry["accepts"], score, -entry["failed_checks"])


async def integrate(root, cfg, folder, config, workflow_name, baseline_fingerprint, patch, stop=None, emit=None, deadline=None):
    """Apply a candidate patch to the user's checkout and rerun required checks there."""
    root = Path(root).resolve()
    current = await asyncio.to_thread(snapshot, root, cfg)
    if current["fingerprint"] != baseline_fingerprint:
        raise UserError("Project files changed since the swarm started; refusing to apply. Review the patch or kept branch manually.")
    applied = await asyncio.to_thread(worktree.apply_patch, root, patch)
    spec = config["workflows"][workflow_name]
    evidence = []
    if spec["required_checks"]:
        runtime = AgentRuntime(root, cfg, Path(folder) / ("integration-" + new_id()), config, workflow_name,
                               "Integration checks for an applied swarm candidate.", stop=stop, emit=emit, spec=spec)
        if deadline:
            runtime.deadline = deadline
        for tool in spec["required_checks"]:
            evidence.append(await runtime.project_tool("controller", tool, {}))
    fingerprint = (await asyncio.to_thread(snapshot, root, cfg))["fingerprint"]
    return {"applied": applied, "patch": str(patch), "time": now(), "evidence": evidence,
            "checks": check_summary(evidence),
            "checks_pass": checks_current(evidence, fingerprint) if evidence else None}


class SwarmRuntime:
    def __init__(self, root, cfg, folder, config, workflow, goal, context="", stop=None, emit=None, driver=None):
        self.root = Path(root).resolve()
        self.cfg, self.folder, self.config = cfg, Path(folder), config
        self.workflow_name, self.workflow, self.goal = workflow, config["workflows"][workflow], goal
        self.context, self.callback, self.driver = context, emit, driver
        self.stop = stop or threading.Event()
        timeout = self.workflow["timeout_seconds"]
        self.final_deadline = time.monotonic() + timeout
        # Keep time to collect patches, clean up and (optionally) integrate after the last round.
        self.deadline = self.final_deadline - (min(120, timeout * 0.2) if self.workflow["apply_winner"] else min(30, timeout * 0.1))
        self.event_guard = threading.RLock()
        self.loop_folder = self.folder / "loop"
        self.loop_folder.mkdir(parents=True, exist_ok=True)
        self.slots, self.extra_runtimes, self.seen_messages = {}, [], {}
        self.base = None
        self.state = {"type": "swarm", "workflow": workflow, "goal": goal, "status": "running", "started": now(),
                      "mode": self.workflow["mode"], "round": 0, "iteration": 0, "baseline": None,
                      "baseline_fingerprint": None, "workspace": None, "candidates": {}, "rounds": [],
                      "leader": None, "winner": None, "applied": None, "agent_calls": 0, "tool_calls": 0,
                      "reported_cost_usd": 0.0, "cost_reports": 0, "usage": {}, "cost_coverage": "reported SDK calls only"}
        self.save()

    # Shared accounting -----------------------------------------------------------------
    def runtimes(self):
        return [info["runtime"] for info in self.slots.values()] + self.extra_runtimes

    def total_cost(self):
        return sum(rt.state["reported_cost_usd"] for rt in self.runtimes())

    def remaining_cost(self):
        return self.workflow["max_reported_cost_usd"] - self.total_cost()

    def budget(self):
        if self.stop.is_set():
            raise UserError("Swarm run cancelled.")
        if time.monotonic() >= self.deadline:
            raise BudgetReached("Swarm runtime limit reached.")
        if self.total_cost() >= self.workflow["max_reported_cost_usd"]:
            raise BudgetReached("Reported model-cost estimate limit reached.")

    def save(self):
        with self.event_guard:
            usage, coverage = {}, "reported SDK calls only"
            for rt in self.runtimes():
                for key, value in rt.state["usage"].items():
                    if type(value) in {int, float}:
                        usage[key] = usage.get(key, 0) + value
                if rt.state["cost_coverage"] != "reported SDK calls only":
                    coverage = rt.state["cost_coverage"]
            self.state.update(agent_calls=sum(rt.state["agent_calls"] for rt in self.runtimes()),
                              tool_calls=sum(rt.state["tool_calls"] for rt in self.runtimes()),
                              reported_cost_usd=self.total_cost(), usage=usage, cost_coverage=coverage,
                              cost_reports=sum(rt.state["cost_reports"] for rt in self.runtimes()))
            write_json(self.loop_folder / "state.json", clean(self.state))

    def emit(self, kind, actor="swarm", **data):
        event = clean({"type": "agentctl." + kind, "actor": actor, "time": now(), **data})
        with self.event_guard:
            with (self.loop_folder / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            if self.callback:
                self.callback(event)

    def forward(self, slot):
        def relay(event):
            if not self.callback:
                return
            data = dict(event)
            # Prefix actors so candidate checks are never mistaken for checks on the user's checkout.
            data.update(candidate=slot, agent=data.get("actor"), actor=f"{slot}.{data.get('actor', 'controller')}")
            with self.event_guard:
                self.callback(data)
        return relay

    # Setup and candidate rounds ----------------------------------------------------------
    async def setup(self):
        self.base = await asyncio.to_thread(worktree.baseline, self.root)
        fingerprint = (await asyncio.to_thread(snapshot, self.root, self.cfg))["fingerprint"]
        workspace = state_dir(self.root) / "worktrees" / self.folder.name
        self.state.update(baseline=self.base, baseline_fingerprint=fingerprint, workspace=str(workspace))
        names = self.workflow["candidates"]
        total = len(names) * self.workflow["replicas"]
        index = 0
        for agent in names:
            for replica in range(1, self.workflow["replicas"] + 1):
                index += 1
                slot = agent if self.workflow["replicas"] == 1 else f"{agent}-{replica}"
                path = workspace / slot
                project = await asyncio.to_thread(worktree.create, self.root, self.base, path)
                folder = self.folder / "candidates" / slot
                folder.mkdir(parents=True, exist_ok=True)
                runtime = AgentRuntime(project, self.cfg, folder, self.config, self.workflow_name, self.goal, self.context,
                                       self.stop, self.forward(slot), self.driver, spec=self.workflow, parent=self)
                runtime.deadline = self.deadline
                self.slots[slot] = {"agent": agent, "index": index, "total": total, "root": project, "path": path,
                                    "folder": folder, "runtime": runtime, "status": "active", "commit": self.base}
                self.state["candidates"][slot] = {"agent": agent, "backend": self.config["agents"][agent]["backend"],
                                                  "model": self.config["agents"][agent].get("model"),
                                                  "workspace": str(project), "status": "active"}
                self.emit("candidate.created", slot=slot, agent=agent, workspace=str(project))
        self.save()

    def drop(self, slot, round_number, status, reason):
        self.slots[slot]["status"] = status
        self.state["candidates"][slot].update(status=status, reason=reason, round=round_number)
        self.emit("candidate.dropped", slot=slot, status=status, reason=reason, round=round_number)
        self.save()
        return None

    async def turn(self, slot, round_number, task):
        info = self.slots[slot]
        runtime, agent = info["runtime"], info["agent"]
        async with self.sem:
            self.budget()
            try:
                result = await runtime.call_agent(agent, task)
                checks = [await runtime.project_tool("controller", tool, {}) for tool in self.workflow["required_checks"]]
                head = await asyncio.to_thread(worktree.commit, info["root"], f"agentctl swarm {slot} round {round_number}")
                files = await asyncio.to_thread(worktree.changed_files, info["root"], self.base, head)
                bad = [p for p in files if not in_scope(p, runtime.agent_spec(agent)["write_scope"])]
                if bad:
                    raise UserError("Candidate changed files outside its write scope: " + ", ".join(bad[:10]))
                patch = info["folder"] / f"round-{round_number}.patch"
                write_text(patch, await asyncio.to_thread(worktree.diff, info["root"], self.base, head))
                stat = await asyncio.to_thread(worktree.diff_stat, info["root"], self.base, head)
                fingerprint = (await asyncio.to_thread(snapshot, info["root"], self.cfg))["fingerprint"]
            except (BudgetReached, asyncio.TimeoutError) as exc:
                self.budget()  # Re-raises when the whole swarm, not just this candidate, is out of budget.
                return self.drop(slot, round_number, "exhausted", str(exc) or "Candidate limit reached.")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.stop.is_set():
                    raise
                return self.drop(slot, round_number, "failed", str(exc))
        info["commit"] = head
        entry = {"slot": slot, "agent": agent, "round": round_number, "summary": redact(str(result.get("summary", "")))[:4000],
                 "artifacts": result.get("artifacts", [])[:50], "checks": check_summary(checks),
                 "checks_pass": checks_current(checks, fingerprint), "failed_checks": sum(c["status"] != "passed" for c in checks),
                 "commit": head, "files": files[:200], "diff_stat": stat, "patch": str(patch),
                 "reviews": [], "accepts": 0, "score": None, "finalist": False}
        self.state["candidates"][slot].update(commit=head, round=round_number)
        self.emit("candidate.completed", slot=slot, round=round_number, checks_pass=entry["checks_pass"],
                  files=entry["files"][:30], summary=entry["summary"][:600])
        return entry

    def critic_task(self, entry):
        diff = worktree.diff(self.slots[entry["slot"]]["root"], self.base, entry["commit"], limit=12000)
        return (f"Judge candidate {entry['slot']} (round {entry['round']}) against the operator goal. The project files "
                "visible to you are this candidate's isolated workspace; inspect them as needed.\n"
                f"Candidate's reported summary (not verified): {entry['summary'][:2500]}\n"
                f"Required check evidence: {json.dumps(entry['checks'])[:4000]}\n"
                f"Changed files: {entry['diff_stat'][:1500]}\n"
                f"Changes relative to the shared starting point:\n{diff}\n\n"
                "Return decision accept (meets the goal and is ready to integrate), revise, or reject; a score from 0 to 10 "
                "for quality relative to the goal; and concrete, actionable feedback. Do not claim checks ran without evidence.")

    async def critique(self, entry, critic):
        runtime = self.slots[entry["slot"]]["runtime"]
        guard = self.local_guard if self.config["agents"][critic]["backend"] in LOCAL else contextlib.nullcontext()
        async with self.sem, guard:
            self.budget()
            try:
                task = await asyncio.to_thread(self.critic_task, entry)
                review = await runtime.call_agent(critic, task, reviewer=True, schema=CRITIC_SCHEMA)
            except (BudgetReached, asyncio.TimeoutError) as exc:
                self.budget()
                review = {"decision": "error", "score": None, "feedback": "Critic did not complete: " + (str(exc) or "limit reached")}
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.stop.is_set():
                    raise
                review = {"decision": "error", "score": None, "feedback": "Critic did not complete: " + str(exc)}
        entry["reviews"].append({"critic": critic, **review})

    async def review(self, entries, round_number):
        if not entries:
            return
        if self.workflow["required_checks"]:
            pool = [e for e in entries if e["checks_pass"]]
        else:
            shift = (round_number - 1) % len(entries)
            pool = entries[shift:] + entries[:shift]
        finalists = pool[:self.workflow["max_finalists"]]
        results = await asyncio.gather(*(self.critique(e, c) for e in finalists for c in self.workflow["critics"]),
                                       return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        for entry in finalists:
            entry["finalist"] = True
            if self.slots[entry["slot"]]["runtime"].state["integrity_failure"]:
                entry.update(checks_pass=False, accepts=0, score=None)
                self.drop(entry["slot"], round_number, "failed", "A managed agent changed this candidate's files outside its scope.")
                continue
            scores = [r["score"] for r in entry["reviews"] if isinstance(r.get("score"), (int, float))]
            entry["accepts"] = sum(r["decision"] == "accept" for r in entry["reviews"])
            entry["score"] = round(sum(scores) / len(scores), 3) if scores else None

    @staticmethod
    def review_text(entry):
        return "\n".join(f"- {r['critic']}: {r['decision']} (score {r['score']}): {r['feedback'][:2000]}" for r in entry["reviews"])

    def operator_messages(self, slot):
        agent = self.slots[slot]["agent"]
        seen = self.seen_messages.setdefault(slot, set())
        fresh = [m for m in read_mail(self.folder, agent) if m["id"] not in seen]
        seen.update(m["id"] for m in fresh)
        if not fresh:
            return ""
        return "\nOperator messages for you:\n" + "\n".join(f"- {m['from']}: {m['body'][:2000]}" for m in fresh[-10:])

    def next_feedback(self, entries, leader, round_number):
        leader_text = ""
        if leader:
            diff = worktree.diff(self.slots[leader["slot"]]["root"], self.base, leader["commit"], limit=6000)
            leader_text = (f"\nCurrent leading candidate: {leader['slot']} ({leader['agent']}).\n"
                           f"Its reported summary: {leader['summary'][:2000]}\n"
                           f"Its checks: {json.dumps(leader['checks'])[:2000]}\n"
                           f"Critic feedback on it:\n{self.review_text(leader)[:3000] or '- not reviewed'}\n"
                           f"Its changes relative to the shared starting point (truncated):\n{diff}\n")
        feedback = {}
        for entry in entries:
            slot = entry["slot"]
            if self.slots[slot]["status"] != "active":
                continue
            own = (f"Round {round_number} result for your candidate {slot}: required checks "
                   f"{'passed' if entry['checks_pass'] else 'did not pass'}.\n"
                   f"Check evidence: {json.dumps(entry['checks'])[:4000]}\n")
            own += (f"Critic feedback on your candidate:\n{self.review_text(entry)[:4000]}\n" if entry["reviews"] else
                    "Your candidate was not a reviewed finalist this round; finalists must pass the required checks.\n")
            if leader and slot == leader["slot"]:
                text = own + "You are the current leader. Improve your solution by addressing the critic feedback without regressing passing checks."
            elif leader and self.workflow["mode"] == "adopt_leader":
                worktree.reset(self.slots[slot]["root"], leader["commit"])
                self.slots[slot]["commit"] = leader["commit"]
                text = (own + leader_text + "\nYour workspace has been reset to the leader's version. Improve on it: address the "
                        "critic feedback above and any weaknesses you find.")
            else:
                text = (own + leader_text + "\nRevise your own candidate. You may adopt ideas from the leader, fix your failures, "
                        "or pursue a better approach; explain your choice in your summary.")
            feedback[slot] = (f"Round {round_number + 1}. " + text + self.operator_messages(slot))[:20000]
        return feedback

    # Main loop -------------------------------------------------------------------------
    async def run(self):
        self.sem = asyncio.Semaphore(self.workflow["max_parallel"])
        self.local_guard = asyncio.Lock()
        outcome, error, winner, best = "limit_reached", None, None, None
        self.emit("loop.started", workflow=self.workflow_name, goal=self.goal, agents=list(self.config["agents"]),
                  candidates=self.workflow["candidates"], critics=self.workflow["critics"], replicas=self.workflow["replicas"])
        try:
            await self.setup()
            total = len(self.slots)
            feedback = {slot: (f"Round 1. You are candidate {slot} ({info['index']} of {total}). Other candidates are solving the "
                               "same goal independently in separate isolated workspaces; they cannot see your files and you cannot "
                               "see theirs until the round ends. Inspect the project and produce your best complete solution."
                               + self.operator_messages(slot))
                        for slot, info in self.slots.items()}
            leaders = []
            for round_number in range(1, self.workflow["max_rounds"] + 1):
                self.budget()
                active = [s for s, info in self.slots.items() if info["status"] == "active" and s in feedback]
                if not active:
                    raise UserError("No candidate remains active.")
                self.state.update(round=round_number, iteration=round_number)
                self.emit("round.started", round=round_number, candidates=active)
                results = await asyncio.gather(*(self.turn(s, round_number, feedback[s]) for s in active), return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        raise result
                entries = [r for r in results if r]
                await self.review(entries, round_number)
                entries = [e for e in entries if self.slots[e["slot"]]["status"] == "active"]
                # Ties keep the incumbent leader so equal candidates do not trade places every round.
                incumbent = self.state["leader"]
                ranked = sorted(entries, key=lambda e: (rank_key(e), e["slot"] == incumbent), reverse=True)
                leader = ranked[0] if ranked else None
                qualified = bool(leader and leader["checks_pass"] and leader["accepts"] >= self.workflow["min_accepts"])
                leaders.append(leader["slot"] if qualified else None)
                if leader and (best is None or rank_key(leader) >= rank_key(best)):
                    best = leader
                board = [{"slot": e["slot"], "agent": e["agent"], "checks_pass": e["checks_pass"], "accepts": e["accepts"],
                          "score": e["score"], "finalist": e["finalist"]} for e in ranked]
                self.state["rounds"].append({"round": round_number, "leader": leader["slot"] if leader else None,
                                             "qualified": qualified, "leaderboard": board, "entries": entries})
                self.state["leader"] = leader["slot"] if leader else None
                summary = (f"round {round_number}: leader {leader['slot']} checks={'pass' if leader['checks_pass'] else 'fail'} "
                           f"accepts={leader['accepts']}/{len(self.workflow['critics'])} score={leader['score']}" if leader
                           else f"round {round_number}: no candidate produced a result")
                self.emit("swarm.round", round=round_number, leader=self.state["leader"], qualified=qualified,
                          leaderboard=board, summary=summary)
                self.save()
                need = self.workflow["stable_rounds"]
                if qualified and len(leaders) >= need and all(x == leader["slot"] for x in leaders[-need:]):
                    outcome, winner = "accepted", leader
                    break
                if not entries:
                    raise UserError("Every candidate failed or exhausted its limits this round.")
                if round_number < self.workflow["max_rounds"]:
                    self.budget()
                    feedback = await asyncio.to_thread(self.next_feedback, entries, leader, round_number)
            else:
                error = "Maximum rounds reached without a stable, accepted leader."
        except (BudgetReached, asyncio.TimeoutError) as exc:
            outcome, error = "limit_reached", str(exc) or "Swarm runtime limit reached."
        except asyncio.CancelledError:
            outcome, error = "cancelled", "Swarm run cancelled."
            self.stop.set()
        except Exception as exc:
            outcome, error = ("cancelled" if self.stop.is_set() else "failed"), str(exc)
        try:
            await self.finalize(outcome, winner, best)
        except Exception as exc:
            error = (error + "; " if error else "") + "Finalization: " + str(exc)
        return self.finish(outcome, error)

    async def finalize(self, outcome, winner, best):
        chosen = winner or best
        try:
            for slot, info in self.slots.items():
                if info["commit"] != self.base:
                    write_text(info["folder"] / "final.patch", await asyncio.to_thread(worktree.diff, info["root"], self.base, info["commit"]))
            if chosen and self.base:
                patch = self.folder / "winner.patch"
                write_text(patch, await asyncio.to_thread(worktree.diff, self.slots[chosen["slot"]]["root"], self.base, chosen["commit"]))
                branch = f"agentctl/swarm/{self.folder.name}"
                try:
                    await asyncio.to_thread(worktree.keep_branch, self.root, branch, chosen["commit"])
                except UserError as exc:
                    branch = None
                    self.emit("swarm.branch_failed", error=str(exc))
                self.state["winner"] = {"slot": chosen["slot"], "agent": chosen["agent"], "round": chosen["round"],
                                        "accepted": outcome == "accepted", "commit": chosen["commit"], "branch": branch,
                                        "patch": str(patch), "checks_pass": chosen["checks_pass"], "accepts": chosen["accepts"],
                                        "score": chosen["score"], "summary": chosen["summary"], "files": chosen["files"]}
                if outcome == "accepted" and self.workflow["apply_winner"] and not self.stop.is_set():
                    self.deadline = self.final_deadline
                    try:
                        applied = await integrate(self.root, self.cfg, self.folder, self.config, self.workflow_name,
                                                  self.state["baseline_fingerprint"], patch, self.stop, self.callback, self.final_deadline)
                        self.state["applied"] = {k: v for k, v in applied.items() if k != "evidence"}
                    except Exception as exc:
                        self.state["applied"] = {"applied": False, "error": str(exc), "time": now()}
                    self.emit("swarm.integration", **self.state["applied"])
        finally:
            if not self.workflow["keep_worktrees"]:
                for info in self.slots.values():
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(worktree.remove, self.root, info["path"])
                if self.state["workspace"]:
                    shutil.rmtree(self.state["workspace"], ignore_errors=True)
            self.save()

    def finish(self, outcome, error):
        if outcome in {"limit_reached", "cancelled", "failed"}:
            self.stop.set()
        self.state.update(status=outcome, finished=now(), error=error)
        self.save()
        winner = self.state["winner"]
        self.emit("loop.finished", outcome=outcome, iteration=self.state["round"], error=error,
                  agent_calls=self.state["agent_calls"], tool_calls=self.state["tool_calls"],
                  reported_cost_usd=self.state["reported_cost_usd"] if self.state["cost_reports"] else None,
                  usage=self.state["usage"], winner=winner["slot"] if winner else None)
        lines = ["# Swarm workflow", "", f"Goal: {self.goal}", f"Outcome: {outcome}", f"Rounds: {self.state['round']}",
                 f"Candidates: {len(self.slots)} ({self.state['mode']})", f"Agent calls: {self.state['agent_calls']}",
                 f"Tool calls: {self.state['tool_calls']}", "", NOTE]
        if not self.workflow["required_checks"]:
            lines.append("No required command checks were configured; ranking relies on critic review only.")
        if self.state["rounds"]:
            lines += ["", "## Final leaderboard", "", "| Candidate | Agent | Checks | Critic accepts | Score |", "|---|---|---|---|---|"]
            for row in self.state["rounds"][-1]["leaderboard"]:
                lines.append(f"| {row['slot']} | {row['agent']} | {'pass' if row['checks_pass'] else 'fail'} | "
                             f"{row['accepts']}/{len(self.workflow['critics'])} | {row['score'] if row['score'] is not None else '-'} |")
        dropped = {s: c for s, c in self.state["candidates"].items() if c["status"] != "active"}
        if dropped:
            lines += ["", "## Dropped candidates"] + [f"- {s}: {c['status']} — {c.get('reason', '')[:300]}" for s, c in dropped.items()]
        if winner:
            label = "Winner" if winner["accepted"] else "Best candidate (not accepted)"
            lines += ["", f"## {label}: {winner['slot']} (round {winner['round']})", winner["summary"], "",
                      f"Patch: {winner['patch']}", f"Branch: {winner['branch'] or 'not created'}"]
            applied = self.state["applied"]
            if applied and applied.get("applied"):
                lines.append(f"Applied to the project working tree. Integration checks passed: {applied.get('checks_pass')}")
            elif applied:
                lines.append("Not applied: " + str(applied.get("error")))
            else:
                lines.append(f"Not applied. Review it, then run: agentctl agents apply {self.folder.name}"
                             + ("" if winner["accepted"] else " --allow-unaccepted"))
        if error:
            lines += ["", "## Stopping reason", error]
        lines += ["", f"Full swarm state: {self.loop_folder / 'state.json'}", f"Candidate artifacts: {self.folder / 'candidates'}"]
        write_text(self.folder / "answer.md", redact("\n".join(lines)))
        return self.state
