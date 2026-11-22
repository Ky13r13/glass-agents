"""Isolated SDK/controller process consumed by the existing bounded CLI runner."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import signal
import sys
import threading

from .agent_config import validate_config
from .agent_runtime import AgentRuntime
from .common import read_text


async def run(request, stop):
    def emit(event):
        print(json.dumps(event, ensure_ascii=False), flush=True)
    driver = None
    if request.get("demo"):
        from .feedback_demo import demo_driver
        driver = demo_driver
    runtime = AgentRuntime(Path(request["root"]), request["config"], Path(request["folder"]),
                           validate_config(request["agents"]), request["workflow"], request["goal"],
                           request["context"], stop, emit, driver)
    state = await runtime.run()
    if state["status"] in {"failed", "cancelled"}:
        emit({"type": "turn.failed", "error": state.get("error") or state["status"]})
        return 1
    emit({"type": "turn.completed", "usage": state["usage"]})
    return 0


def main():
    stop = threading.Event()
    def halt(signum, frame):
        stop.set()
    signal.signal(signal.SIGTERM, halt)
    signal.signal(signal.SIGINT, halt)
    try:
        request = json.load(sys.stdin)
        return asyncio.run(run(request, stop))
    except Exception as exc:
        print(json.dumps({"type": "turn.failed", "error": str(exc)}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
