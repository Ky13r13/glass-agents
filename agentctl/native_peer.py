"""Run native providers one turn at a time and route their peer requests through Glass."""
from __future__ import annotations

import asyncio
from collections import deque
import json
import math
from pathlib import Path
import threading

from .adapters import event_summary
from .agent_config import validate_value
from .agent_runtime import BudgetReached, clean, object_schema, read_mail
from .common import Store, UserError, digest, new_id, read_text, write_json, write_text
from .runner import execute_run, prepare_run


def turn_schema(runtime, actor, final_schema):
    tools = runtime.tool_definitions(actor)
    choices = [object_schema({"final": final_schema}, ["final"])]
    for name in ("send_message", "call_agent"):
        choices.append(object_schema({name: tools[name][1]}, [name]))
    return object_schema({"turn": {"anyOf": choices}}, ["turn"])


def account(runtime, record):
    for key, value in record.get("usage", {}).items():
        if type(value) in {int, float}:
            runtime.state["usage"][key] = runtime.state["usage"].get(key, 0) + value
    runtime.state["cost_coverage"] = "partial: CLI peer cost may be unavailable"
    cost = record.get("reported_cost_usd")
    if type(cost) in {int, float} and math.isfinite(cost) and cost >= 0:
        runtime.state["reported_cost_usd"] += cost
        runtime.state["cost_reports"] += 1


def preview(value, limit):
    text = json.dumps(clean(value), ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + " [truncated]"


class TurnStop(threading.Event):
    def __init__(self, workflow_stop):
        super().__init__()
        self.workflow_stop = workflow_stop

    def is_set(self):
        return super().is_set() or self.workflow_stop.is_set()


async def run_turn(runtime, actor, contract, schema, turn):
    agent = runtime.agent_spec(actor)
    backend = agent["backend"]
    if backend != "local":
        path = runtime.loop_folder / ("peer-task-" + new_id() + ".md")
        write_text(path, contract)
        contract = f"Read the full role and response contract in {path}. Follow it and return the required JSON."
    permission = "workspace-write" if agent["write_scope"] else "read-only"
    record = prepare_run(runtime.root, runtime.cfg, runtime.goal, backend, permission,
                         model=agent.get("model"), write_scope=agent["write_scope"],
                         parent=runtime.folder.name, managed_contract=contract)
    schema_path = Path(record["folder"]) / "output-schema.json"
    write_json(schema_path, schema)
    record.update(output_schema_file=str(schema_path), output_schema_sha256=digest(read_text(schema_path)))
    Store(runtime.root).save(record)
    runtime.emit("peer.link", actor, run_id=record["id"], backend=backend, folder=record["folder"], turn=turn)

    def observe(child, event):
        if event:
            runtime.emit("peer.event", actor, event=event_summary(event), run_id=child["id"])

    stop = TurnStop(runtime.stop)
    job = asyncio.create_task(asyncio.to_thread(execute_run, runtime.root, runtime.cfg, record, stop, False, True, observe))
    try:
        result = await asyncio.shield(job)
    except asyncio.CancelledError:
        stop.set()
        await job  # Keep the workspace lock until the child process group has exited.
        raise
    account(runtime, result)
    if result["status"] == "scope_violation":
        runtime.state["integrity_failure"] = True
    if result["status"] != "completed":
        raise UserError(f"Managed CLI peer {actor} did not complete: {result.get('error', result['status'])}")
    runtime.budget()
    return Path(result["folder"]) / "answer.md"


async def native_call(runtime, actor, prompt, stack, final_schema, reviewer):
    agent = runtime.agent_spec(actor)
    peers = agent["peers"]
    schema = turn_schema(runtime, actor, final_schema) if peers else final_schema
    role = (f"{agent['instructions']}\nWrite scope: {json.dumps(agent['write_scope'])}\n{prompt}\n"
            f"Return only JSON matching this schema:\n{json.dumps(schema)}")
    if peers:
        roster = {name: runtime.agent_spec(name)["backend"] for name in peers}
        role += (f"\nAssigned peers: {json.dumps(roster)}. Choose one operation per turn. "
                 "send_message queues a note. call_agent runs a peer now and returns its result. "
                 "Use final to finish. Glass supplies recent exchanges on your next turn.")
    history = deque(maxlen=4)
    limit = runtime.workflow["max_turns_per_agent"]
    for turn in range(1, limit + 1):
        async with runtime.action_guard:
            runtime.budget()
            mail = read_mail(runtime.folder, actor)[-15:]
            contract = f"{role}\nRecent messages addressed to you: {json.dumps(clean(mail), ensure_ascii=False)}"
            if peers:
                contract += f"\nTurn {turn} of {limit}. Recent peer exchanges:\n" + "\n".join(history)
            answer = await run_turn(runtime, actor, contract, schema, turn)
        raw = read_text(answer)
        try:
            response = json.loads(raw)
        except ValueError as exc:
            if peers or reviewer:
                raise UserError(f"Managed agent {actor} returned invalid JSON.") from exc
            # Existing single-turn native workers may report a plain-text summary.
            return {"summary": raw[:18000], "artifacts": [str(answer)]}
        if not peers:
            return response
        validate_value(response, schema)
        operation, args = next(iter(response["turn"].items()))
        if operation == "final":
            return args
        if turn == limit:
            raise BudgetReached(f"Managed agent {actor} reached its {limit}-turn limit before a final response.")
        result = await runtime.tool(actor, operation, args, stack)
        if operation == "send_message":
            result = {"queued": result["id"]}
        history.append(f"{operation} {preview(args, 800)}\nResult: {preview(result, 3200)}")
