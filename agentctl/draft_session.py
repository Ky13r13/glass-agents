"""One live team draft per dashboard, with a pollable result and cancellation."""
from copy import deepcopy
import threading
import time

from .common import UserError, new_id


class DraftSession:
    def __init__(self, draft):
        self.draft = draft
        self.guard = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.state = None
        self.closed = False

    def start(self, body):
        with self.guard:
            if self.closed:
                raise UserError("Dashboard is shutting down.")
            if self.thread and self.thread.is_alive():
                raise UserError("A team draft is already running. Follow its progress or cancel it.")
            self.stop.clear()
            self.state = {"id": new_id(), "status": "running", "stage": "starting",
                          "started": time.monotonic(), "last_activity": None, "events": 0,
                          "goal": body.get("goal"), "provider": body.get("provider", "codex")}
            self.thread = threading.Thread(target=self.run, args=(body,), name="glass-team-draft", daemon=True)
            self.thread.start()
            return self.snapshot()

    def progress(self, stage, event=False):
        with self.guard:
            self.state["stage"] = stage
            if event:
                self.state["last_activity"] = time.monotonic()
                self.state["events"] += 1

    def run(self, body):
        try:
            result = self.draft(body, stop=self.stop, progress=self.progress)
        except UserError as exc:
            self.finish(error=str(exc))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.finish(error=f"Team draft could not be completed: {exc}")
        except Exception:
            self.finish(error="Team draft failed unexpectedly. Inspect the local dashboard server logs.")
            raise
        else:
            self.finish(result=result)

    def finish(self, *, result=None, error=None):
        with self.guard:
            status = "cancelled" if self.stop.is_set() else "failed" if error else "completed"
            self.state.update(status=status, finished=time.monotonic())
            if status == "completed":
                self.state["result"] = result
            elif status == "failed":
                self.state["error"] = error

    def snapshot(self):
        if self.state is None:
            return None
        state = deepcopy(self.state)
        end = state.pop("finished", time.monotonic())
        state["elapsed_seconds"] = int(end - state.pop("started"))
        last = state.pop("last_activity")
        state["seconds_since_activity"] = None if last is None else int(end - last)
        return state

    def status(self):
        with self.guard:
            return self.snapshot()

    def cancel(self, ident):
        with self.guard:
            if not self.state or self.state["id"] != ident or self.state["status"] != "running":
                raise UserError("This team draft is no longer running.")
            self.stop.set()
            self.state["status"] = "cancelling"
            return self.snapshot()

    def close(self):
        with self.guard:
            self.closed = True
            self.stop.set()
            thread = self.thread
        if thread:
            thread.join(timeout=12)
