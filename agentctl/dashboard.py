"""Local dashboard over agentctl's existing context, runner, history and workflows."""
from __future__ import annotations

import copy
import importlib.util
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

from . import __version__
from .adapters import connection_info, event_summary, process_connections
from .common import Store, UserError, load_config, lock, new_id, now, read_text, run_command, state_dir, write_json
from .project import initialize, inventory, redact, report
from .runner import execute_run, prepare_run, verify
from .workflows import catalog, compile_workflow

ACTIVE = {"preparing", "running", "stopping"}
TERMINAL = {"completed", "failed", "cancelled", "blocked", "scope_violation", "interrupted"}


def bounded_text(value, field, limit=12000, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise UserError(f"{field} must be {'a string' if empty else 'nonempty text'} of at most {limit} characters.")
    if "\x00" in value:
        raise UserError(f"{field} cannot contain a NUL character.")
    return value.strip()


class Dashboard:
    def __init__(self, root, cfg=None, demo=False):
        self.root = Path(root).resolve()
        self.cfg = cfg or load_config(self.root)
        self.demo = demo
        self.guard = threading.RLock()
        self.closed = threading.Event()
        self.wake = threading.Event()
        self.active = {}
        self.verifying = False
        self.verify_stop = threading.Event()
        self.verify_thread = None
        self.settings_path = state_dir(self.root) / "dashboard-settings.json"
        self.jobs_path = state_dir(self.root) / "dashboard-jobs.json"
        self.settings = {"codex_bin": self.cfg["codex_bin"], "opencode_bin": self.cfg["opencode_bin"]}
        self.jobs = []
        self.lease = None
        if self.settings_path.exists() and not demo:
            saved = json.loads(read_text(self.settings_path))
            if not isinstance(saved, dict):
                raise UserError("Invalid dashboard connection settings.")
            for key in self.settings:
                if key in saved:
                    self.settings[key] = bounded_text(saved[key], key, 1000)
        self.instance_lease = lock(state_dir(self.root) / "dashboard.lock")
        self.instance_lease.__enter__()
        try:
            self.restore_jobs()
            self.persist()
            self.scheduler = threading.Thread(target=self.schedule, name="agentctl-scheduler", daemon=True)
            self.scheduler.start()
        except Exception:
            self.instance_lease.__exit__(*sys.exc_info())
            raise

    def restore_jobs(self):
        if self.jobs_path.exists():
            self.jobs = json.loads(read_text(self.jobs_path))
            if not isinstance(self.jobs, list) or not all(isinstance(j, dict) for j in self.jobs):
                raise UserError("Invalid dashboard task storage.")
            for job in self.jobs:
                if job["status"] in ACTIVE | {"queued"}:
                    job.update(status="interrupted", finished=now(),
                               error="Dashboard stopped before this job finished. Inspect logs, then explicitly retry.")
                    if job.get("run_id"):
                        try:
                            rec = Store(self.root).get(job["run_id"])
                            if rec["status"] == "running":
                                rec.update(status="interrupted", finished=now(), error=job["error"])
                                Store(self.root).save(rec)
                        except UserError:
                            pass

    def persist(self):
        write_json(self.jobs_path, self.jobs)

    def config(self):
        return dict(self.cfg, **self.settings)

    def enqueue(self, spec):
        if not isinstance(spec, dict):
            raise UserError("Expected a task object.")
        goal = bounded_text(spec.get("goal"), "Task")
        backend = spec.get("backend", "codex")
        permission = spec.get("permission", "read-only")
        if backend not in {"codex", "opencode", "local", "codex-local", "claude-sdk"}:
            raise UserError("Unknown backend.")
        if permission not in {"read-only", "workspace-write"}:
            raise UserError("Unknown permission.")
        if backend == "opencode" and permission != "workspace-write":
            raise UserError("OpenCode is CLI-managed and runs exclusively. Choose CLI-managed in the task form.")
        agent_workflow = spec.get("agent_workflow") or "default"
        if backend == "claude-sdk":
            if permission != "workspace-write":
                raise UserError("Managed feedback workflows use exclusive scheduling; configure individual agent scopes in agents.json.")
            from .agent_config import load_agents
            definitions, _ = load_agents(self.root)
            if agent_workflow not in definitions["workflows"]:
                raise UserError("Unknown managed feedback workflow.")
        if backend in {"local", "codex-local"} and permission != "read-only":
            raise UserError("Local backends are read-only.")
        model = bounded_text(spec.get("model") or "", "Model", 200, empty=True) or None
        name = bounded_text(spec.get("name") or goal[:60], "Name", 100)
        includes = spec.get("includes", [])
        if not isinstance(includes, list) or len(includes) > 30 or not all(isinstance(x, str) for x in includes):
            raise UserError("Includes must be an array of at most 30 relative file paths.")
        for path in includes:
            if not path or Path(path).is_absolute() or ".." in Path(path).parts:
                raise UserError("Includes must stay inside the project.")
        depends = spec.get("depends_on", [])
        if not isinstance(depends, list) or len(depends) > 30:
            raise UserError("Dependencies must be an array of job IDs.")
        scope = spec.get("write_scope")
        if scope is not None and (not isinstance(scope, list) or not all(isinstance(p, str) and p and not Path(p).is_absolute() and ".." not in Path(p).parts for p in scope)):
            raise UserError("Write scopes must be relative paths or globs inside the project.")
        with self.guard:
            if self.closed.is_set():
                raise UserError("Dashboard is shutting down.")
            known = {j["id"] for j in self.jobs}
            if any(not isinstance(x, str) or x not in known for x in depends):
                raise UserError("A dependency does not exist.")
            if sum(j["status"] in ACTIVE | {"queued"} for j in self.jobs) >= 30:
                raise UserError("The queue is full (30 outstanding tasks).")
            job = {"id": new_id(), "created": now(), "name": name, "goal": goal, "backend": backend,
                   "permission": permission, "model": model, "includes": includes,
                   "depends_on": depends, "parent": spec.get("parent"), "run_id": None, "write_scope": scope,
                   "agent_workflow": agent_workflow if backend == "claude-sdk" else None,
                   "status": "queued", "connection": connection_info(self.root, backend, model, self.config())}
            self.jobs.append(job)
            self.persist()
            self.wake.set()
            return copy.deepcopy(job)

    def launch_workflow(self, spec):
        if spec.get("backend") == "claude-sdk":
            raise UserError("Select a Managed feedback workflow for the Claude SDK runtime.")
        name = spec.get("workflow")
        if name not in {x["name"] for x in catalog()}:
            raise UserError("Unknown workflow.")
        goal = bounded_text(spec.get("goal"), "Task")
        # Built-in, validated templates retain their original scopes and dependencies.
        plan = compile_workflow(name, goal, includes=spec.get("includes", []))
        mapping, created = {}, []
        with self.guard:
            try:
                for job in plan["jobs"]:
                    backend = spec.get("backend", "codex")
                    if backend in {"local", "codex-local"} and job["permission"] == "workspace-write":
                        raise UserError("This workflow needs a writing backend. Choose Codex or OpenCode.")
                    rec = self.enqueue(dict(job, backend=backend, model=spec.get("model"),
                                            name=job["id"], permission="workspace-write" if backend == "opencode" else job["permission"],
                                            depends_on=[mapping[d] for d in job.get("depends_on", [])]))
                    mapping[job["id"]] = rec["id"]
                    created.append(rec)
                    self.find(rec["id"])["write_scope"] = job.get("write_scope")
                self.persist()
            except Exception:
                # The scheduler cannot observe a partial template while this lock is held.
                self.jobs = [j for j in self.jobs if j["id"] not in {r["id"] for r in created}]
                self.persist()
                raise
        return created

    def find(self, ident):
        for job in self.jobs:
            if job["id"] == ident:
                return job
        raise UserError("No matching dashboard task.")

    def cancel(self, ident):
        with self.guard:
            job = self.find(ident)
            if job["status"] == "queued":
                job.update(status="cancelled", finished=now())
            elif ident in self.active:
                job["status"] = "stopping"
                self.active[ident]["stop"].set()
            else:
                raise UserError("This task is no longer running or queued.")
            self.persist()
            self.wake.set()
            return {"ok": True}

    def followup(self, ident, spec):
        with self.guard:
            old = self.find(ident)
            if old["status"] not in TERMINAL:
                raise UserError("Wait for the current task to finish, or cancel it before sending a follow-up.")
            return self.enqueue({"name": spec.get("name") or old["name"],
                                 "goal": bounded_text(spec.get("goal"), "Follow-up"),
                                 "backend": spec.get("backend", old["backend"]),
                                 "permission": old["permission"], "model": spec.get("model", old["model"]),
                                 "includes": old["includes"], "write_scope": old.get("write_scope"), "parent": ident,
                                 "agent_workflow": old.get("agent_workflow")})

    def schedule(self):
        while not self.closed.is_set():
            self.wake.wait(0.2)
            self.wake.clear()
            with self.guard:
                if self.verifying:
                    continue
                statuses = {j["id"]: j["status"] for j in self.jobs}
                for job in self.jobs:
                    if job["status"] != "queued":
                        continue
                    deps = [statuses[x] for x in job["depends_on"]]
                    if any(s in TERMINAL - {"completed"} for s in deps):
                        job.update(status="blocked", finished=now(), error="A dependency did not complete.")
                        self.persist()
                        continue
                    if any(s != "completed" for s in deps):
                        continue
                    active_jobs = [self.find(i) for i in self.active]
                    writer = job["permission"] == "workspace-write"
                    if len(active_jobs) >= self.cfg["max_workers"] or (writer and active_jobs):
                        continue
                    if any(j["permission"] == "workspace-write" for j in active_jobs):
                        continue
                    local = job["backend"] in {"local", "codex-local"}
                    if local and any(j["backend"] in {"local", "codex-local"} for j in active_jobs):
                        continue
                    if self.lease is None:
                        lease = lock(state_dir(self.root) / "operation.lock")
                        try:
                            lease.__enter__()
                        except UserError:
                            job["waiting_reason"] = "Another agentctl operation owns this workspace."
                            continue
                        self.lease = lease
                    job.pop("waiting_reason", None)
                    job.update(status="preparing", started=now())
                    control = {"stop": threading.Event()}
                    worker = threading.Thread(target=self.work, args=(job, control["stop"]), daemon=True)
                    control["thread"] = worker
                    self.active[job["id"]] = control
                    self.persist()
                    worker.start()
                if not self.active and self.lease:
                    self.lease.__exit__(None, None, None)
                    self.lease = None
    def work(self, job, stop):
        rec = None
        try:
            cfg = self.config()
            carry, dependencies, parent_run = "", [], None
            with self.guard:
                if job.get("parent"):
                    old = self.find(job["parent"])
                    if old.get("run_id"):
                        prior = Store(self.root).get(old["run_id"])
                        parent_run = prior["id"]
                        carry = f"Previous task: {old['goal']}\nReported result:\n{prior.get('answer_excerpt', '')}"
                for ident in job["depends_on"]:
                    dep = self.find(ident)
                    if dep.get("run_id"):
                        prior = Store(self.root).get(dep["run_id"])
                        dependencies.append(f"{dep['name']}: {prior.get('answer_excerpt', '')[:1800]}\nArtifacts: {prior['folder']}")
            if stop.is_set():
                raise UserError("Run cancelled before context preparation.")
            if job["backend"] == "claude-sdk":
                from .agent_config import prepare_feedback
                rec = prepare_feedback(self.root, cfg, job["goal"], job.get("agent_workflow") or "default",
                                       includes=job["includes"], carry=carry, extra="\n\n".join(dependencies),
                                       parent=parent_run, model=job["model"])
            else:
                rec = prepare_run(self.root, cfg, job["goal"], job["backend"], job["permission"],
                                  includes=job["includes"], model=job["model"], carry=carry,
                                  extra="\n\n".join(dependencies), parent=parent_run, write_scope=job.get("write_scope"))
            with self.guard:
                job.update(run_id=rec["id"], status="stopping" if stop.is_set() else "running")
                job["connection"] = connection_info(self.root, job["backend"], job["model"], cfg)
                self.persist()
            execute_run(self.root, cfg, rec, stop=stop, quiet=True, observer=self.observed)
            with self.guard:
                job.update(status=rec["status"], finished=now())
                if rec.get("error"):
                    job["error"] = rec["error"]
                elif rec.get("provider_error"):
                    job["error"] = redact(json.dumps(rec["provider_error"]))
        except Exception as exc:
            with self.guard:
                job.update(status="cancelled" if stop.is_set() else "failed", finished=now(), error=str(exc))
                if rec:
                    rec.update(status=job["status"], finished=now(), error=str(exc))
                    Store(self.root).save(rec)
        finally:
            with self.guard:
                self.active.pop(job["id"], None)
                self.persist()
                self.wake.set()

    def observed(self, record, event):
        # The runner stores events and metadata; callbacks never mutate source or run tools.
        self.wake.set()

    def update_settings(self, supplied):
        if self.demo:
            raise UserError("Demo connections are synthetic fixtures. Start without --demo to configure real CLIs.")
        with self.guard:
            if self.active:
                raise UserError("Wait for active tasks to finish before changing connections.")
            updated = dict(self.settings)
            for key in ("codex_bin", "opencode_bin"):
                if key in supplied:
                    updated[key] = bounded_text(supplied[key], key, 1000)
            self.settings = updated
            write_json(self.settings_path, self.settings)
        return self.settings

    def probe(self, backend):
        if backend not in {"codex", "opencode"}:
            raise UserError("Choose Codex or OpenCode.")
        binary = self.config()[backend + "_bin"]
        resolved = shutil.which(binary)
        if not resolved:
            return {"available": False, "binary": binary, "message": "Executable not found. Set its absolute path."}
        result = run_command([resolved, "--version"], self.root, timeout=8)
        return {"available": bool(result and result.returncode == 0), "binary": resolved,
                "message": redact((result.stdout + result.stderr).decode(errors="replace")[:2000]) if result else "Version check timed out.",
                "note": "Version check only; provider authentication was not tested."}

    def state(self):
        with self.guard:
            jobs = copy.deepcopy(self.jobs)
            settings = dict(self.settings)
            verifying = self.verifying
        records = {r["id"]: r for r in Store(self.root).list(200)}
        linked = {j.get("run_id") for j in jobs}
        for rec in reversed(list(records.values())):
            if rec["id"] not in linked and rec.get("agent_workflow") and rec["status"] != "prepared":
                jobs.append({"id": rec["id"], "run_id": rec["id"], "name": "Managed · " + rec["goal"][:55],
                             "goal": rec["goal"], "created": rec["created"], "started": rec.get("started"),
                             "finished": rec.get("finished"), "status": rec["status"], "backend": "claude-sdk",
                             "permission": rec["permission"], "model": rec.get("model"), "includes": rec.get("includes", []),
                             "agent_workflow": rec["agent_workflow"], "external": True,
                             "connection": connection_info(self.root, "claude-sdk", cfg=self.config()),
                             "error": rec.get("error")})
        visible_runs = set()
        for job in jobs:
            rec = records.get(job.get("run_id"), {})
            if rec:
                visible_runs.add(rec["id"])
                job["run"] = {k: rec.get(k) for k in ("id", "status", "pid", "usage", "reported_cost_usd", "elapsed_seconds",
                                                     "answer_excerpt", "commands_observed", "changes", "checks", "manifest",
                                                     "error", "provider_error", "argv", "thread_id", "folder",
                                                     "agent_activity", "agent_sessions", "loop_outcome", "loop_iteration")}
                job["run"]["agent_names"] = rec.get("agent_names", [])
                job["sockets"] = process_connections(rec.get("pid")) if job["status"] in ACTIVE else []
        history = [{k: r.get(k) for k in ("id", "goal", "created", "backend", "status", "usage", "folder")}
                   for r in records.values() if r["id"] not in visible_runs]
        managed = {"configured": False, "workflows": [], "sdk_installed": importlib.util.find_spec("claude_agent_sdk") is not None}
        try:
            from .agent_config import load_agents
            definitions, _ = load_agents(self.root)
            managed.update(configured=True, workflows=list(definitions["workflows"]))
        except (UserError, ValueError, OSError) as exc:
            managed["message"] = str(exc)
        return {"version": __version__, "root": str(self.root), "project": self.root.name, "demo": self.demo, "managed": managed,
                "jobs": list(reversed(jobs)), "history": history, "settings": settings, "verifying": verifying,
                "backends": [{"id": b, "binary": settings[b + "_bin"], "resolved": shutil.which(settings[b + "_bin"]),
                              "connection": connection_info(self.root, b, cfg=self.config())} for b in ("codex", "opencode")],
                "workflows": catalog(), "max_workers": self.cfg["max_workers"],
                "socket_observation": "Linux process-tree TCP sample; proxies may hide the upstream destination."}

    def agent_message(self, ident, recipient, message):
        from .agent_runtime import send_mail
        record = self.run_record(ident)
        if record["backend"] != "claude-sdk":
            raise UserError("Messages require a managed agent workflow.")
        result = send_mail(record["folder"], record["agent_names"], "operator", recipient, message)
        return {"queued": result, "delivery": "Next message-tool read or agent invocation; no interruption."}

    def run_record(self, ident):
        # IDs come from our SQLite store; artifact paths are additionally confined below.
        try:
            with self.guard:
                ident = self.find(ident).get("run_id") or ""
        except UserError:
            pass
        if not ident:
            raise UserError("The task has not started yet.")
        return Store(self.root).get(ident)

    def events(self, ident, offset=0):
        rec = self.run_record(ident)
        folder = Path(rec["folder"])
        path = folder / "events.jsonl"
        if not folder.resolve().is_relative_to(state_dir(self.root).resolve()) or path.is_symlink():
            raise UserError("Invalid event path.")
        diagnostics = folder / "stderr.log"
        stderr = ""
        if diagnostics.is_file() and not diagnostics.is_symlink():
            with diagnostics.open("rb") as handle:
                handle.seek(max(0, diagnostics.stat().st_size - 12000))
                stderr = redact(handle.read(12000).decode(errors="replace"))
        if not path.exists():
            return {"events": [], "offset": 0, "diagnostic": stderr}
        length = path.stat().st_size
        if offset < 0 or offset > length:
            raise UserError("Invalid event offset.")
        events = []
        with path.open("rb") as handle:
            handle.seek(offset)
            for _ in range(150):
                start = handle.tell()
                line = handle.readline(1024 * 1024 + 1)
                if not line:
                    break
                if not line.endswith(b"\n") and rec["status"] == "running":
                    handle.seek(start)
                    break
                if len(line) > 1024 * 1024:
                    events.append({"kind": "status", "label": "Large event", "text": "Inspect the raw log locally."})
                    while line and not line.endswith(b"\n"):
                        line = handle.readline(1024 * 1024)
                    continue
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError()
                    value = event_summary(event)
                    value["raw_type"] = event.get("type")
                    value["timestamp"] = event.get("timestamp")
                    events.append(json.loads(redact(json.dumps(value))))
                except ValueError:
                    events.append({"kind": "diagnostic", "label": "CLI output", "text": redact(line.decode(errors="replace")[:4000])})
            offset = handle.tell()
        return {"events": events, "offset": offset, "diagnostic": stderr}

    def artifact(self, ident, name):
        if name not in {"PACKET.md", "answer.md", "stderr.log", "manifest.json", "launch.json", "events.jsonl",
                        "loop/state.json", "loop/messages.jsonl"}:
            raise UserError("Unknown artifact.")
        rec = self.run_record(ident)
        path = Path(rec["folder"]) / name
        if path.is_symlink() or not path.resolve().is_relative_to(state_dir(self.root).resolve()):
            raise UserError("Invalid artifact path.")
        if not path.exists():
            return "This artifact has not been written yet."
        with path.open("rb") as handle:
            raw = handle.read(512 * 1024 + 1)
        text = raw[:512 * 1024].decode(errors="replace")
        if len(raw) > 512 * 1024:
            text += "\n[Preview limited to 512 KiB; inspect the full artifact locally.]"
        return redact(text)

    def check(self, ident, command):
        command = bounded_text(command, "Verification command", 3000)
        argv = shlex.split(command)
        if not argv:
            raise UserError("Supply a verification command.")
        rec = self.run_record(ident)
        if rec["status"] in {"prepared", "running"}:
            raise UserError("Wait for the agent to finish.")
        with self.guard:
            if self.active or self.verifying or any(j["status"] == "queued" for j in self.jobs):
                raise UserError("Wait until the task queue is idle before verifying.")
            lease = self.lease
            if lease:
                self.lease = None
            else:
                lease = lock(state_dir(self.root) / "operation.lock")
                lease.__enter__()
            self.verifying = True
            self.verify_stop.clear()

        def run():
            try:
                verify(self.root, self.config(), rec["id"], argv, stop=self.verify_stop)
            finally:
                with self.guard:
                    self.verifying = False
                    lease.__exit__(None, None, None)
                    self.wake.set()
        self.verify_thread = threading.Thread(target=run, daemon=True)
        self.verify_thread.start()
        return {"ok": True}

    def close(self):
        with self.guard:
            self.closed.set()
            self.verify_stop.set()
            controls = list(self.active.values())
            for control in controls:
                control["stop"].set()
            for job in self.jobs:
                if job["status"] == "queued":
                    job.update(status="cancelled", finished=now())
            self.persist()
        for control in controls:
            control["thread"].join(timeout=12)
        if self.verify_thread:
            self.verify_thread.join(timeout=12)
        self.wake.set()
        self.scheduler.join(timeout=3)
        with self.guard:
            if self.lease and not self.active:
                self.lease.__exit__(None, None, None)
                self.lease = None
            if self.instance_lease and not self.active and not self.verifying:
                self.instance_lease.__exit__(None, None, None)
                self.instance_lease = None


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, dashboard):
        self.dashboard = dashboard
        self.token = secrets.token_urlsafe(32)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "agentctl"

    def log_message(self, *args):
        pass

    def send(self, body, status=200, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = (json.dumps(body) if content_type.startswith("application/json") else body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handle_request(self, post=False):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if host not in hosts or (origin and origin not in {"http://" + h for h in hosts}):
            self.send({"error": "Local origin required."}, 403)
            return
        url = urlsplit(self.path)
        if not post and url.path in {"/", "/app.js", "/style.css"}:
            names = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
            name, mime = names[url.path]
            content = files("agentctl").joinpath("web", name).read_text()
            if name == "index.html":
                content = content.replace("__AGENTCTL_TOKEN__", self.server.token)
            self.send(content, content_type=mime + "; charset=utf-8")
            return
        if not secrets.compare_digest(self.headers.get("X-Agentctl-Token", ""), self.server.token):
            self.send({"error": "Dashboard session expired. Reload the page."}, 403)
            return
        dashboard = self.server.dashboard
        query = parse_qs(url.query)
        ident = query.get("id", [""])[0]
        try:
            if post:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 65536:
                    raise UserError("Request body must be between 1 and 65536 bytes.")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise UserError("Expected a JSON object.")
                routes = {
                    "/api/jobs": lambda: dashboard.enqueue(body),
                    "/api/workflow": lambda: dashboard.launch_workflow(body),
                    "/api/cancel": lambda: dashboard.cancel(body.get("id")),
                    "/api/followup": lambda: dashboard.followup(body.get("id"), body),
                    "/api/settings": lambda: dashboard.update_settings(body),
                    "/api/probe": lambda: dashboard.probe(body.get("backend")),
                    "/api/verify": lambda: dashboard.check(body.get("id"), body.get("command")),
                    "/api/agent-message": lambda: dashboard.agent_message(body.get("id"), body.get("to"), body.get("message")),
                }
            else:
                routes = {
                    "/api/state": dashboard.state,
                    "/api/events": lambda: dashboard.events(ident, int(query.get("offset", ["0"])[0])),
                    "/api/artifact": lambda: {"text": dashboard.artifact(ident, query.get("name", [""])[0])},
                    "/api/inspect": lambda: report(inventory(dashboard.root, dashboard.config())),
                }
            if url.path not in routes:
                self.send({"error": "Not found."}, 404)
                return
            self.send(routes[url.path]())
        except (UserError, ValueError, TypeError, KeyError) as exc:
            self.send({"error": str(exc)}, 400)
        except Exception:
            self.send({"error": "Dashboard operation failed. Inspect the local project and runtime logs."}, 500)

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request(True)


def serve_dashboard(root, port=8765, open_browser=True, demo=False):
    if type(port) is not int or not 0 <= port <= 65535:
        raise UserError("Port must be between 0 and 65535.")
    temporary = None
    if demo:
        temporary = tempfile.TemporaryDirectory(prefix="agentctl-demo-")
        root = Path(temporary.name) / "qte-research-demo"
        root.mkdir()
        (root / "README.md").write_text("# Synthetic demo workspace\nNo trading engine or market data is attached.\n")
        initialize(root)
        from .demo import make_runtime
        runtime = make_runtime(root)
        cfg = dict(load_config(root), codex_bin=str(runtime), opencode_bin=str(runtime))
    else:
        if not (state_dir(root) / "config.toml").is_file():
            raise UserError("Initialize this project first: agentctl init. Or preview with agentctl dashboard --demo.")
        cfg = load_config(root)
    dashboard = Dashboard(root, cfg, demo=demo)
    server = None
    try:
        server = DashboardServer(("127.0.0.1", port), dashboard)
        url = f"http://127.0.0.1:{server.server_port}"
        print(f"agentctl {__version__} dashboard: {url}\nProject: {root}\nPress Ctrl+C to stop the dashboard and its managed jobs.", flush=True)
        if demo:
            dashboard.enqueue({"name": "Strategy researcher", "goal": "DEMO: inspect the strategy interface and propose an experiment contract.", "permission": "read-only", "model": "synthetic-fixture"})
            dashboard.enqueue({"name": "Validation reviewer", "goal": "DEMO: inspect the evaluation protocol and report leakage risks.", "permission": "read-only", "model": "synthetic-fixture"})
        if open_browser:
            webbrowser.open(url)
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.server_close()
        dashboard.close()
        if temporary:
            temporary.cleanup()
    return 0
