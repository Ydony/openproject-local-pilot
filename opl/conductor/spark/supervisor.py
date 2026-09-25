"""Supervise Spark worker processes: time limits, stall stop, slots.

Standard library only. run_worker starts the command with output to a log
file, polls for completion, and kills the whole process tree on timeout or
stall. OPL_TIME_SCALE (env float, default 1) scales every limit so tests run
in seconds. Stopping uses process groups on POSIX and taskkill on Windows.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

# Every worker process still running, so a shutting-down conductor can
# stop them all (TH.9). Workers start in their own process group or
# session and would otherwise outlive the conductor.
_LIVE = set()
_LIVE_LOCK = threading.Lock()
# Stop gate (TH.17): once terminate_all() runs, no new worker may spawn —
# not even a run thread still stuck in preparation (fetch, worktree).
_STOP = {"stopping": False}
# Each runner's own stop event, bound to its run threads (TH.23): reopening
# the process-wide gate for a new runner never releases an older runner's
# threads that are still preparing.
_THREAD = threading.local()


def bind_stop(event):
    """Tie the calling run thread to its runner's stop event."""
    _THREAD.stop = event


def reset_stop_gate():
    """Reopen the stop gate: for a new runner in the same process (the
    conductor's _make_runner) and for tests. A runner that was shut down
    never resumes on its own."""
    with _LIVE_LOCK:
        _STOP["stopping"] = False


@dataclass(frozen=True)
class RunResult:
    outcome: str  # "success" | "failed" | "timeout" | "stalled"
    duration_s: float
    last_lines: tuple
    cost_usd: float = 0.0


_COST_RE = re.compile(r"^OPL-COST:\s*([0-9]+(?:\.[0-9]+)?)\s*$", re.MULTILINE)

# How often the supervisor polls (real seconds). Fine-grained enough for
# scaled test windows; the mtime walk below is additionally throttled.
_POLL_S = 0.1
# Minimum gap between workdir mtime walks: full walks of big checkouts are
# expensive, and per-second resolution is plenty for stall windows.
_MTIME_EVERY_S = 1.0


def _scale():
    try:
        return float(os.environ.get("OPL_TIME_SCALE", "1") or "1")
    except ValueError:
        return 1.0


def _parse_cost(text):
    match = _COST_RE.search(text or "")
    if not match:
        return 0.0
    try:
        return float(match.group(1))
    except ValueError:
        return 0.0


def _tail_lines(text, count=20):
    return tuple(text.splitlines()[-count:])


class _DirWatcher:
    """Newest mtime under a directory, walked at most once per interval."""

    def __init__(self, root, exclude=()):
        self.root = root
        self.exclude = {os.path.abspath(p) for p in exclude}
        self.last_walk = 0.0
        self.newest = 0.0

    def poll(self, now):
        if now - self.last_walk < _MTIME_EVERY_S:
            return self.newest
        self.last_walk = now
        newest = self.newest
        for dirpath, _dirnames, filenames in os.walk(self.root):
            for name in filenames:
                path = os.path.join(dirpath, name)
                if os.path.abspath(path) in self.exclude:
                    continue
                try:
                    stamp = os.path.getmtime(path)
                except OSError:
                    continue
                if stamp > newest:
                    newest = stamp
        self.newest = newest
        return newest


# OS basics a worker process needs to start at all. Nothing credential-like:
# every other variable of the conductor (tokens included) is dropped.
_BASE_ENV_NAMES = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "LANG", "LC_ALL", "TZ", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
)


def worker_environment(allow, home_dir, base=None):
    """Build a worker's whole environment explicitly.

    OS basics, plus only the variable names in `allow` (the worker's own API
    key), plus every home/temp/config variable pointed into `home_dir`, a
    fresh directory, so no owner tool configs, logins or histories are
    reachable through them.
    """
    source = os.environ if base is None else base
    env = {}
    for name in _BASE_ENV_NAMES + tuple(allow):
        value = source.get(name)
        if value is not None:
            env[name] = value
    home = os.path.abspath(home_dir)
    dirs = {
        "HOME": home,
        "USERPROFILE": home,
        "APPDATA": os.path.join(home, "AppData", "Roaming"),
        "LOCALAPPDATA": os.path.join(home, "AppData", "Local"),
        "XDG_CONFIG_HOME": os.path.join(home, ".config"),
        "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
        "XDG_CACHE_HOME": os.path.join(home, ".cache"),
        "XDG_STATE_HOME": os.path.join(home, ".local", "state"),
        "TEMP": os.path.join(home, "tmp"),
        "TMP": os.path.join(home, "tmp"),
        "TMPDIR": os.path.join(home, "tmp"),
    }
    for name, path in dirs.items():
        os.makedirs(path, exist_ok=True)
        env[name] = path
    return env


def _spawn(cmd, workdir, log_file, env):
    kwargs = {"cwd": workdir, "stdout": log_file, "stderr": subprocess.STDOUT,
              "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def _kill_tree(process):
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, timeout=30,
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except Exception:
        pass
    try:
        process.wait(timeout=30)
    except Exception:
        pass


def _track(process, running):
    with _LIVE_LOCK:
        (_LIVE.add if running else _LIVE.discard)(process)


def terminate_all():
    """Kill the process tree of every running worker; return how many.

    Also sets the stop gate: run threads still preparing (fetching,
    creating a worktree) reach run_worker afterwards and return "failed"
    without spawning. Their run_worker loops then see the exit and report
    "failed".
    """
    with _LIVE_LOCK:
        _STOP["stopping"] = True
        live = list(_LIVE)
    for process in live:
        _kill_tree(process)
    return len(live)


def live_workers():
    with _LIVE_LOCK:
        return len(_LIVE)


def run_worker(cmd, workdir, limit_s, stall_s, log_path, clock=time.monotonic,
               env=None):
    """Run one worker to completion, timeout, or stall. Never raises for
    worker behavior (spawn failures, i.e. caller bugs, do raise).

    `env` is the worker's whole environment (see worker_environment). When
    omitted, an isolated one with no extra variables is built next to the
    log, so a forgetful caller can never leak the conductor's secrets.
    """
    scale = _scale()
    limit = max(limit_s * scale, 0.05)
    stall = max(stall_s * scale, 0.05)
    parent = os.path.dirname(os.path.abspath(log_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    if env is None:
        env = worker_environment(
            (), os.path.join(parent, "home-" + os.path.basename(log_path)))
    start = clock()
    deadline = start + limit
    # Two clock domains: monotonic for timeouts, wall mtimes for files.
    # They never mix; mtime changes only refresh the monotonic activity mark.
    last_activity = start
    seen_mtime = None
    watcher = _DirWatcher(workdir, exclude=(log_path,))
    with open(log_path, "wb") as log_file:
        # Raw worker output may hold anything; keep it owner-only (TH.2).
        # No-op where the OS ignores POSIX modes.
        try:
            os.chmod(log_path, 0o600)
        except OSError:
            pass
        # The gate check and the spawn plus registration happen inside
        # one lock hold: a terminate_all() in between can neither miss
        # this worker nor let it start after the stop (TH.17, Codex E4).
        with _LIVE_LOCK:
            own = getattr(_THREAD, "stop", None)
            stopping = _STOP["stopping"] or (own is not None and own.is_set())
            process = None
            if not stopping:
                process = _spawn(cmd, workdir, log_file, env)
                _LIVE.add(process)
        if process is None:
            log_file.write(b"conductor shutting down\n")
            log_file.flush()
            outcome = "failed"
        else:
            outcome = None
            while outcome is None:
                now = clock()
                rc = process.poll()
                if rc is not None:
                    outcome = "success" if rc == 0 else "failed"
                    break
                # Progress is worktree file changes and commits (seen through
                # the mtime walk below). Log growth alone is NOT progress: a
                # worker that only spams output must stall (TH.10, R13).
                newest = watcher.poll(now)
                if seen_mtime is None:
                    seen_mtime = newest
                elif newest > seen_mtime:
                    seen_mtime = newest
                    last_activity = now
                if now - last_activity >= stall:
                    outcome = "stalled"
                    break
                if now >= deadline:
                    outcome = "timeout"
                    break
                time.sleep(_POLL_S)
            if outcome in ("timeout", "stalled"):
                _kill_tree(process)
            # Only a finished loop untracks: after an unexpected error the
            # process stays registered, so shutdown still kills it.
            _track(process, False)
        end = clock()
    with open(log_path, "rb") as fh:
        text = fh.read().decode("utf-8", "replace")
    return RunResult(
        outcome=outcome,
        duration_s=end - start,
        last_lines=_tail_lines(text),
        cost_usd=_parse_cost(text),
    )


class Slots:
    """Track running tasks up to max_parallel (single-threaded use)."""

    def __init__(self, max_parallel):
        self.max_parallel = max_parallel
        self._running = set()

    def acquire(self, task_id):
        """Take a slot; False when full or already held."""
        if task_id in self._running or len(self._running) >= self.max_parallel:
            return False
        self._running.add(task_id)
        return True

    def release(self, task_id):
        self._running.discard(task_id)

    def __len__(self):
        return len(self._running)
