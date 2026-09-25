"""Isolated git worktrees for Spark runs. Standard library only."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class Worktree:
    path: str
    branch: str


# Non-interactive git: fail fast on missing credentials instead of opening
# a sign-in window. The credential helper is deliberately left alone so
# live fetches still use the owner's configured gh helper.
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}


def git_env(extra=None):
    """Process environment for git calls: non-interactive, plus `extra`."""
    env = dict(os.environ)
    env.update(GIT_ENV)
    if extra:
        env.update(extra)
    return env


def _git(repo_path, *args):
    proc = subprocess.run(
        ["git", "-C", repo_path, *args],
        capture_output=True, text=True, timeout=120, env=git_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), proc.stderr.strip()))
    return proc.stdout.strip()


def create_worktree(repo_path, base_ref, task_id, state_dir):
    """Create an isolated worktree on branch `opl/task-<id>-<stamp>`.

    The worktree lives under `<state_dir>/worktrees/`, so all conductor
    state stays in one place. Returns a Worktree. On a name collision
    (two runs in the same second) a -2/-3 suffix is added.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = "opl/task-%s-%s" % (task_id, stamp)
    for attempt in range(100):
        branch = base if attempt == 0 else "%s-%d" % (base, attempt + 1)
        path = os.path.join(state_dir, "worktrees", branch)
        if _branch_exists(repo_path, branch) or os.path.exists(path):
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _git(repo_path, "worktree", "add", "-b", branch, path, base_ref)
        return Worktree(path=path, branch=branch)
    raise RuntimeError("could not find a free worktree name for %s" % base)


def _branch_exists(repo_path, branch):
    proc = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "--verify", "--quiet",
         "refs/heads/" + branch],
        capture_output=True, text=True, timeout=120,
    )
    return proc.returncode == 0


def remove_worktree(worktree):
    """Remove a worktree created by create_worktree.

    `worktree` is a Worktree; the main repo is resolved from the worktree
    itself, so callers never track it separately.
    """
    path = worktree.path if isinstance(worktree, Worktree) else worktree
    proc = subprocess.run(
        ["git", "-C", path, "rev-parse", "--git-common-dir"],
        capture_output=True, text=True, timeout=120, env=git_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError("not a worktree: %s" % path)
    common = proc.stdout.strip()
    if not os.path.isabs(common):
        common = os.path.normpath(os.path.join(path, common))
    main_repo = os.path.dirname(common)
    # Never --force: git refuses to drop a tree with changes, which is the
    # last line of defence for evidence a caller forgot to check (TH.4).
    proc = subprocess.run(
        ["git", "-C", main_repo, "worktree", "remove", path],
        capture_output=True, text=True, timeout=120, env=git_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError("git worktree remove failed: %s" % proc.stderr.strip())


def _inside(path, root):
    """True when `path` resolves to a location under `root`."""
    try:
        p = os.path.normcase(os.path.realpath(path))
        r = os.path.normcase(os.path.realpath(root))
        return p != r and os.path.commonpath([p, r]) == r
    except ValueError:
        return False


def dispose_worktree(worktree, state_dir, succeeded):
    """Remove a scratch worktree only after a clean success; keep it otherwise.

    Returns None when removed, else the kept path to report to the lead. A
    failed run, or any tracked or untracked change, keeps the tree as
    evidence. Refuses (RuntimeError) any path outside <state_dir>/worktrees,
    so a mix-up can never remove a real checkout.
    """
    path = worktree.path if isinstance(worktree, Worktree) else worktree
    root = os.path.join(state_dir, "worktrees")
    if not _inside(path, root):
        raise RuntimeError("refusing to remove %s: not under %s" % (path, root))
    if not succeeded:
        return path
    status = subprocess.run(
        ["git", "-C", path, "status", "--porcelain"],
        capture_output=True, text=True, timeout=120, env=git_env(),
    )
    if status.returncode != 0 or status.stdout.strip():
        return path
    remove_worktree(worktree)
    return None


def checkout_worktree(repo_path, ref, label, state_dir):
    """Check out an existing ref in a fresh detached worktree.

    Used for review runs (the PR branch) and test runs (the base). Tries
    a best-effort `fetch origin`, then attaches detached at the ref or at
    `origin/<ref>`. The worktree is scratch: callers remove it afterwards.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = "opl/%s-%s" % (label, stamp)
    path = None
    for attempt in range(100):
        name = base if attempt == 0 else "%s-%d" % (base, attempt + 1)
        candidate_path = os.path.join(state_dir, "worktrees", name)
        if not os.path.exists(candidate_path):
            path = candidate_path
            break
    if path is None:
        raise RuntimeError("could not find a free worktree path")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(
        ["git", "-C", repo_path, "fetch", "origin", ref],
        capture_output=True, text=True, timeout=300, env=git_env(),
    )
    for candidate in (ref, "origin/" + ref):
        proc = subprocess.run(
            ["git", "-C", repo_path, "worktree", "add", "--detach",
             path, candidate],
            capture_output=True, text=True, timeout=300, env=git_env(),
        )
        if proc.returncode == 0:
            return Worktree(path=path, branch=candidate)
    raise RuntimeError("could not check out %r" % (ref,))


_SHA_RE = re.compile(r"^[0-9a-f]{4,64}$")


def checkout_pr_head(repo_path, head_url, sha, label, state_dir,
                     extra_env=None):
    """Check out a PR's exact head SHA in a fresh detached worktree.

    Fetches the SHA from the PR's head repository into an isolated ref
    (`refs/opl/<label>-<stamp>`, never a branch that could go stale) and
    aborts on fetch failure — no fallback to a stale local branch. The
    checkout is verified to equal the SHA before returning.
    """
    if not isinstance(sha, str) or not _SHA_RE.match(sha):
        raise RuntimeError("refusing to check out non-SHA %r" % (sha,))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = "opl/%s-%s" % (label, stamp)
    path = None
    for attempt in range(100):
        name = base if attempt == 0 else "%s-%d" % (base, attempt + 1)
        candidate_path = os.path.join(state_dir, "worktrees", name)
        if not os.path.exists(candidate_path):
            path = candidate_path
            break
    if path is None:
        raise RuntimeError("could not find a free worktree path")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ref = "refs/opl/%s-%s" % (label, stamp)
    env = git_env(extra_env)
    fetch = subprocess.run(
        ["git", "-C", repo_path, "fetch", head_url, sha + ":" + ref],
        capture_output=True, text=True, timeout=300, env=env,
    )
    if fetch.returncode != 0:
        raise RuntimeError(
            "could not fetch PR head %s: %s"
            % (sha, fetch.stderr.strip()[:200]))
    added = subprocess.run(
        ["git", "-C", repo_path, "worktree", "add", "--detach", path, ref],
        capture_output=True, text=True, timeout=300, env=env,
    )
    if added.returncode != 0:
        raise RuntimeError(
            "could not check out PR head %s: %s"
            % (sha, added.stderr.strip()[:200]))
    got = subprocess.run(
        ["git", "-C", path, "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=120, env=env,
    )
    if got.returncode != 0 or got.stdout.strip().lower() != sha.lower():
        raise RuntimeError(
            "checked-out %r does not equal PR head %s"
            % (got.stdout.strip(), sha))
    return Worktree(path=path, branch=ref)


def delete_ref(repo_path, ref):
    """Delete an isolated `refs/opl/…` fetch ref; False when already absent.

    Only `refs/opl/` refs are ever deleted: anything else is a caller bug
    and raises. A missing ref is not an error.
    """
    if not ref.startswith("refs/opl/"):
        raise RuntimeError("refusing to delete non-isolated ref %r" % (ref,))
    verify = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "--verify", "--quiet", ref],
        capture_output=True, text=True, timeout=120, env=git_env(),
    )
    if verify.returncode != 0:
        return False
    done = subprocess.run(
        ["git", "-C", repo_path, "update-ref", "-d", ref],
        capture_output=True, text=True, timeout=120, env=git_env(),
    )
    if done.returncode != 0:
        raise RuntimeError("could not delete ref %r: %s"
                           % (ref, done.stderr.strip()[:200]))
    return True
