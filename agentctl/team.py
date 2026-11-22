from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
from pathlib import Path
import re
import threading

from .common import UserError, new_id, now, read_text, state_dir, write_json, write_text
from .project import inventory, rank_files, snapshot
from .runner import checkpoint, execute_run, prepare_run


def make_plan(root, cfg, goal, local_readers=False):
    """A transparent heuristic: parallelize focused reading; serialize changes."""
    scan = inventory(root, cfg)
    buckets = {}
    for score, path, reasons, f in rank_files(scan, goal):
        if score < 12 or Path(path).suffix == ".md":
            continue
        parts = Path(path).parts
        component = "/".join(parts[:2]) if len(parts) > 2 and parts[0] in {"src", "lib", "packages", "apps"} else parts[0] if len(parts) > 1 else "root"
        buckets.setdefault(component, []).append(path)
    broad = any(word in goal.lower() for word in ("architecture", "refactor", "integrat", "migrat", "across", "pipeline"))
    selected = list(buckets.items())[:2] if broad and len(buckets) >= 2 else []
    jobs = []
    for i, (component, paths) in enumerate(selected, 1):
        jobs.append({"id": f"read-{i}", "goal": f"For the task '{goal}', inspect {component}. Identify relevant interfaces, constraints, and existing tests. Return concise findings with paths and uncertainties. Do not edit files.",
                     "backend": "local" if local_readers else "codex", "permission": "read-only", "includes": paths[:1], "depends_on": [], "write_scope": []})
    jobs.append({"id": "implement", "goal": goal, "backend": "codex", "permission": "workspace-write", "includes": [],
                 "depends_on": [j["id"] for j in jobs], "write_scope": ["**"]})
    return {"version": 1, "goal": goal, "created": now(), "recommendation": "Two focused readers, then one writer." if selected else "One worker: no clear independent reading tasks found.",
            "method": "Keyword/path heuristic, not semantic certainty. Review goals and scopes before running. No model calls were used to plan.",
            "jobs": jobs}


def validate_plan(plan):
    if not isinstance(plan, dict) or plan.get("version") != 1 or not isinstance(plan.get("jobs"), list) or not plan["jobs"]:
        raise UserError("Plan must have version=1 and a nonempty jobs array.")
    if len(plan["jobs"]) > 30:
        raise UserError("v0.1 plans are limited to 30 jobs.")
    ids = set()
    for job in plan["jobs"]:
        if not isinstance(job, dict) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}", str(job.get("id", ""))):
            raise UserError("Each job needs a short alphanumeric id.")
        if job["id"] in ids:
            raise UserError("Duplicate job id: " + job["id"])
        ids.add(job["id"])
        if not isinstance(job.get("goal"), str) or not job["goal"].strip():
            raise UserError(f"Job {job['id']} needs a goal.")
        if job.get("backend", "codex") not in {"codex", "opencode", "local", "codex-local"}:
            raise UserError("Unknown backend in plan.")
        if job.get("permission", "read-only") not in {"read-only", "workspace-write"}:
            raise UserError("Job permission must be read-only or workspace-write.")
        if job.get("backend") == "opencode" and job.get("permission", "read-only") != "workspace-write":
            raise UserError("OpenCode jobs use CLI-managed permissions and must be scheduled exclusively as workspace-write.")
        if job.get("backend") in {"local", "codex-local"} and job.get("permission", "read-only") != "read-only":
            raise UserError("Local jobs are read-only in v0.1.")
        for field in ["includes", "depends_on", "write_scope"]:
            if not isinstance(job.get(field, []), list) or not all(isinstance(p, str) for p in job.get(field, [])):
                raise UserError(f"{field} must be an array of strings.")
        for p in job.get("includes", []) + job.get("write_scope", []):
            if Path(p).is_absolute() or ".." in Path(p).parts:
                raise UserError("Plan scopes and includes must be relative to the repository.")
    done = set()
    while len(done) < len(ids):
        ready = [j["id"] for j in plan["jobs"] if j["id"] not in done and set(j.get("depends_on", [])) <= done]
        if not ready:
            raise UserError("Plan has a dependency cycle or a missing dependency.")
        done.update(ready)
    return plan


def run_team(root, cfg, plan):
    validate_plan(plan)
    team_id = new_id()
    path = state_dir(root) / "teams" / f"{team_id}.json"
    jobs = {j["id"]: j for j in plan["jobs"]}
    statuses = {i: "pending" for i in jobs}
    records, running = {}, {}
    stop = threading.Event()
    before = snapshot(root, cfg)

    def persist():
        value = {"id": team_id, "goal": plan.get("goal"), "status": statuses, "runs": {k: v["id"] for k, v in records.items()},
                 "updated": now(), "plan": plan}
        write_json(path, value)
        return value

    def execute(job):
        dependencies = []
        for dep in job.get("depends_on", []):
            r = records[dep]
            dependencies.append(f"{dep}: status={r['status']}; artifacts={r['folder']}\n{r.get('answer_excerpt', '')[:1800]}")
        rec = prepare_run(root, cfg, job["goal"], backend=job.get("backend", "codex"), permission=job.get("permission", "read-only"),
                          includes=job.get("includes", []), extra="\n\n".join(dependencies), model=job.get("model"),
                          write_scope=job.get("write_scope") or ["**"])
        return execute_run(root, cfg, rec, stop=stop, handoff=False, quiet=True)

    persist()
    pool = ThreadPoolExecutor(max_workers=cfg["max_workers"])
    try:
        while any(s in {"pending", "running"} for s in statuses.values()):
            for ident, job in jobs.items():
                if statuses[ident] != "pending":
                    continue
                deps = job.get("depends_on", [])
                if any(statuses[d] in {"failed", "blocked", "cancelled", "scope_violation"} for d in deps):
                    statuses[ident] = "blocked"
                    continue
                if not all(statuses[d] == "completed" for d in deps) or len(running) >= cfg["max_workers"]:
                    continue
                active_jobs = [jobs[name] for name in running.values()]
                writer = job.get("permission", "read-only") == "workspace-write"
                if (writer and active_jobs) or any(j.get("permission", "read-only") == "workspace-write" for j in active_jobs):
                    continue
                local = job.get("backend") in {"local", "codex-local"}
                if local and any(j.get("backend") in {"local", "codex-local"} for j in active_jobs):
                    continue
                statuses[ident] = "running"
                running[pool.submit(execute, job)] = ident
                print(f"Started {ident} ({job.get('backend', 'codex')}, {job.get('permission', 'read-only')})", flush=True)
            persist()
            if not running:
                if any(s == "pending" for s in statuses.values()):
                    raise UserError("Scheduler cannot make progress; inspect the task graph.")
                break
            finished, _ = wait(running, timeout=0.2, return_when=FIRST_COMPLETED)
            for future in finished:
                ident = running.pop(future)
                try:
                    rec = future.result()
                    records[ident] = rec
                    statuses[ident] = rec["status"]
                except Exception as exc:
                    statuses[ident] = "failed"
                    print(f"{ident} failed before execution: {exc}", flush=True)
                print(f"Finished {ident}: {statuses[ident]}", flush=True)
    except KeyboardInterrupt:
        stop.set()
        for ident in statuses:
            if statuses[ident] == "pending":
                statuses[ident] = "cancelled"
        for future, ident in list(running.items()):
            try:
                records[ident] = future.result(timeout=10)
                statuses[ident] = records[ident]["status"]
            except Exception:
                statuses[ident] = "cancelled"
    finally:
        stop.set()
        pool.shutdown(wait=True, cancel_futures=True)
        result = persist()
        if records:
            writer_records = [records[i] for i in records if jobs[i].get("permission") == "workspace-write"]
            latest = writer_records[-1] if writer_records else list(records.values())[-1]
            checkpoint(root, cfg, latest, note=f"Team {team_id}: {json.dumps(statuses)}. Full task graph: {path}",
                       next_action="Review the team results, resolve any failed or blocked jobs, and run relevant integration checks before calling the task complete.")
    result["path"] = str(path)
    result["successful"] = all(s == "completed" for s in statuses.values())
    return result
