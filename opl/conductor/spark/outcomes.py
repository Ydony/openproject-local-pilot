"""Run-outcome helpers shared by build, review and test runs.

Standard library only. parse_final_line gates success on the worker's last
result line; keep_partial_work commits leftovers so a killed run's branch
still holds them.
"""

from __future__ import annotations

import re
import subprocess

_FINAL_RE = re.compile(r"^OPL-RESULT:\s*(DONE|FAILED)\b(.*)$",
                       re.MULTILINE | re.IGNORECASE)


def parse_final_line(last_lines):
    """Decide a finished run from its trailing output lines.

    Returns ("done", "") when the last result line says DONE,
    ("failed", message) for FAILED or a missing line.
    """
    text = "\n".join(last_lines)
    found = None
    for match in _FINAL_RE.finditer(text):
        found = match
    if found is None:
        return "failed", "missing OPL-RESULT line"
    if found.group(1).upper() == "DONE":
        return "done", ""
    return "failed", found.group(2).strip() or "worker reported FAILED"


def keep_partial_work(worktree_path, task_id, attempt):
    """Commit leftover files as a WIP commit; return True when committed.

    Only commits when the tree is actually dirty. Identity is the
    conductor's own marker, never the worker's config.
    """
    from opl.conductor.spark.worktree import git_env

    env = git_env()
    status = subprocess.run(
        ["git", "-C", worktree_path, "status", "--porcelain"],
        capture_output=True, text=True, timeout=120, env=env)
    if status.returncode != 0 or not status.stdout.strip():
        return False
    message = "WIP: partial work, run %s-%s" % (task_id, attempt)
    add = subprocess.run(
        ["git", "-C", worktree_path, "add", "-A"],
        capture_output=True, text=True, timeout=120, env=env)
    if add.returncode != 0:
        return False
    done = subprocess.run(
        ["git", "-C", worktree_path, "-c", "user.name=opl-conductor",
         "-c", "user.email=conductor@localhost", "commit", "-qm", message],
        capture_output=True, text=True, timeout=120, env=env)
    return done.returncode == 0
