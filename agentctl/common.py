from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import threading
import tomllib
import uuid


class UserError(Exception):
    """An actionable error that does not need a traceback."""


_store_init_guard = threading.Lock()


DEFAULTS = {
    "context_budget": 6000,
    "local_context_budget": 2500,
    "max_file_bytes": 98304,
    "max_files": 5000,
    "max_scan_bytes": 25000000,
    "max_workers": 2,
    "timeout_seconds": 1800,
    "codex_bin": "codex",
    "opencode_bin": "opencode",
    "ollama_url": "http://127.0.0.1:11434",
    "local_model": "agentctl-helper:latest",
    "local_base_model": "qwen2.5-coder:7b-instruct-q4_K_M",
    "local_num_ctx": 8192,
    "local_num_predict": 1024,
}

CONFIG_TEXT = '''# agentctl 0.1 -- Linux / 16 GB RAM / GTX 1080 Ti starting profile.
# Budgets are approximate prompt estimates, not model or billing limits.
context_budget = 6000
local_context_budget = 2500
max_file_bytes = 98304
max_files = 5000
max_scan_bytes = 25000000
max_workers = 2
timeout_seconds = 1800
codex_bin = "codex"
opencode_bin = "opencode"
ollama_url = "http://127.0.0.1:11434"
local_model = "agentctl-helper:latest"
local_base_model = "qwen2.5-coder:7b-instruct-q4_K_M"
local_num_ctx = 8192
local_num_predict = 1024
'''


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def new_id():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]


def tokens(text):
    # Deliberately dependency-free. This is NOT a model tokenizer.
    return math.ceil(len(text.encode("utf-8")) / 3)


def digest(data):
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise UserError(f"Refusing to overwrite a symlink: {path}")
    fd, name = tempfile.mkstemp(prefix=".agentctl-tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path, obj):
    write_text(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def read_text(path):
    return Path(path).read_text(encoding="utf-8", errors="replace")


def run_command(argv, cwd=None, timeout=15, check=False):
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if check:
            raise UserError(f"Could not run {argv[0]}: {exc}") from exc
        return None
    if check and p.returncode:
        raise UserError(p.stderr.decode(errors="replace").strip() or f"{argv[0]} exited {p.returncode}")
    return p


def project_root(path):
    path = Path(path).expanduser().resolve()
    if not path.is_dir():
        raise UserError(f"Not a directory: {path}")
    for parent in [path, *path.parents]:
        if (parent / ".agentctl" / "config.toml").is_file():
            return parent
    result = run_command(["git", "rev-parse", "--show-toplevel"], path)
    if result and result.returncode == 0:
        return Path(result.stdout.decode().strip()).resolve()
    return path


def state_dir(root):
    result = root / ".agentctl"
    if result.is_symlink():
        raise UserError(".agentctl must be a real directory, not a symlink.")
    for name in ("runs", "plans", "teams", "benchmarks", "state.sqlite3", "HANDOFF.md", "handoff.json", "config.toml", "operation.lock",
                 "dashboard.lock", "dashboard-jobs.json", "dashboard-settings.json"):
        if (result / name).is_symlink():
            raise UserError(f"agentctl state must not contain symlinked storage: {name}")
    return result


def load_config(root):
    cfg = DEFAULTS.copy()
    path = state_dir(root) / "config.toml"
    if path.exists():
        try:
            supplied = tomllib.loads(read_text(path))
        except tomllib.TOMLDecodeError as exc:
            raise UserError(f"Invalid {path}: {exc}") from exc
        unknown = set(supplied) - set(DEFAULTS)
        if unknown:
            raise UserError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
        cfg.update(supplied)
    for key, value in DEFAULTS.items():
        if isinstance(value, int):
            if type(cfg[key]) is not int or cfg[key] < 1:
                raise UserError(f"{key} must be a positive integer.")
        elif not isinstance(cfg[key], str) or not cfg[key]:
            raise UserError(f"{key} must be a non-empty string.")
    if cfg["max_workers"] > 8:
        raise UserError("max_workers must be <= 8. Start with 2 on this machine.")
    if cfg["local_context_budget"] + cfg["local_num_predict"] + 512 > cfg["local_num_ctx"]:
        raise UserError("Local prompt budget + output allowance + 512 must fit local_num_ctx.")
    return cfg


def require_init(root):
    if not (state_dir(root) / "config.toml").exists():
        raise UserError("Run `agentctl init` in this project first.")


@contextlib.contextmanager
def lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UserError(f"Another operation holds {path}. Wait for it to finish.") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def local_lock():
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return lock(base / "agentctl" / "local-model.lock")


class Store:
    def __init__(self, root):
        self.root = root
        self.path = state_dir(root) / "state.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Serialize first-time WAL/table setup across simultaneous reader starts.
        with _store_init_guard:
            with self.connect() as db:
                db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created TEXT NOT NULL, data TEXT NOT NULL)")

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

    def save(self, record):
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                       (record["id"], record["created"], json.dumps(record)))

    def get(self, run_id=None):
        with self.connect() as db:
            if run_id:
                row = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
            else:
                row = db.execute("SELECT data FROM runs ORDER BY created DESC, rowid DESC LIMIT 1").fetchone()
        if not row:
            raise UserError("No matching run. Use `agentctl start` first.")
        return json.loads(row[0])

    def list(self, limit=20):
        with self.connect() as db:
            rows = db.execute("SELECT data FROM runs ORDER BY created DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row[0]) for row in rows]


def safe_terminal(text):
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(text))
