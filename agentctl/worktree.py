"""Isolated Git worktrees for concurrent candidate writers.

The baseline is a commit built from the current working tree, including uncommitted
and untracked non-ignored files, using a temporary index. The user's index, branch
and working tree are not modified. Git hooks are disabled for every command here so
creating or committing a candidate never executes project scripts.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile

from .common import UserError

IDENTITY = {"GIT_AUTHOR_NAME": "agentctl", "GIT_AUTHOR_EMAIL": "agentctl@localhost",
            "GIT_COMMITTER_NAME": "agentctl", "GIT_COMMITTER_EMAIL": "agentctl@localhost"}
SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "gc.auto=0"]
# Agent state inside the project or a candidate workspace never becomes part of a candidate.
EXCLUDE = ["--", ":/", ":(exclude).agentctl"]


def git(cwd, *args, env=None, timeout=300, check=True):
    full = {**os.environ, **IDENTITY, **(env or {})}
    try:
        proc = subprocess.run(["git", *SAFE, *args], cwd=cwd, capture_output=True, env=full, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UserError(f"git {args[0]} failed: {exc}") from exc
    if check and proc.returncode:
        message = proc.stderr.decode(errors="replace").strip() or proc.stdout.decode(errors="replace").strip()
        raise UserError(f"git {args[0]} failed: {message[:2000]}")
    return proc


def repository(root):
    """Return (top-level path, project prefix inside the repository)."""
    root = Path(root).resolve()
    proc = git(root, "rev-parse", "--show-toplevel", check=False)
    if proc.returncode:
        raise UserError("Parallel candidate workspaces need a Git repository. Run `git init` in the project first.")
    top = Path(proc.stdout.decode().strip()).resolve()
    if not root.is_relative_to(top):
        raise UserError("Project directory is outside its Git work tree.")
    return top, root.relative_to(top).as_posix().strip(".")


def baseline(root):
    """Commit the current working-tree content without touching the user's index or refs."""
    top, _ = repository(root)
    head = git(top, "rev-parse", "--verify", "-q", "HEAD", check=False)
    head = head.stdout.decode().strip() if head.returncode == 0 else None
    with tempfile.TemporaryDirectory(prefix="agentctl-index-") as temp:
        env = {"GIT_INDEX_FILE": str(Path(temp) / "index")}
        if head:
            git(top, "read-tree", head, env=env)
        git(Path(root).resolve(), "add", "-A", *EXCLUDE, env=env)
        tree = git(top, "write-tree", env=env).stdout.decode().strip()
    parent = ["-p", head] if head else []
    return git(top, "commit-tree", tree, *parent, "-m", "agentctl swarm baseline").stdout.decode().strip()


def create(root, commit, path):
    """Add a detached worktree at path; returns the project directory inside it."""
    top, prefix = repository(root)
    path = Path(path)
    if path.exists():
        raise UserError(f"Candidate workspace already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    git(top, "worktree", "add", "-q", "--detach", str(path), commit)
    return path / prefix if prefix else path


def commit(project, message):
    """Record the candidate's current files; returns the commit ID."""
    git(project, "add", "-A", *EXCLUDE)
    git(project, "commit", "-q", "--allow-empty", "--no-verify", "-m", message)
    return git(project, "rev-parse", "HEAD").stdout.decode().strip()


def diff(project, base, head, limit=None):
    text = git(project, "diff", "--binary", base, head).stdout.decode(errors="replace")
    return text if limit is None or len(text) <= limit else text[:limit] + "\n[diff truncated]\n"


def diff_stat(project, base, head):
    return git(project, "diff", "--stat", base, head).stdout.decode(errors="replace")[:4000]


def changed_files(project, base, head):
    """Changed paths relative to the project; paths outside it are returned with a leading '../'."""
    top, prefix = repository(project)
    raw = git(project, "diff", "--name-only", "-z", base, head).stdout.decode(errors="replace")
    result = []
    for path in filter(None, raw.split("\0")):
        if not prefix:
            result.append(path)
        elif path.startswith(prefix + "/"):
            result.append(path[len(prefix) + 1:])
        else:
            result.append("../" + path)
    return result


def reset(project, target):
    """Make a candidate workspace match another candidate's recorded commit."""
    git(project, "reset", "-q", "--hard", target)
    git(project, "clean", "-fdq", "-e", ".agentctl", "--", ".")


def remove(root, path):
    top, _ = repository(root)
    git(top, "worktree", "remove", "--force", str(path), check=False)
    git(top, "worktree", "prune", check=False)


def keep_branch(root, name, target):
    top, _ = repository(root)
    git(top, "branch", name, target)
    return name


def apply_patch(root, patch_path):
    """Apply a candidate patch to the user's working tree (not the index); refuses on conflict."""
    top, _ = repository(root)
    patch_path = Path(patch_path)
    if not patch_path.read_text(encoding="utf-8", errors="replace").strip():
        return False
    check = git(top, "apply", "--check", "--binary", str(patch_path), check=False)
    if check.returncode:
        raise UserError("Candidate patch does not apply cleanly to the current working tree: " +
                        check.stderr.decode(errors="replace").strip()[:2000])
    git(top, "apply", "--binary", str(patch_path))
    return True
