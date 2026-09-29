"""Owner-side sandbox adapter: run Spark tasks as the isolated worker.

Issue #24 (T5.1 S5): the conductor's Spark runner uses this module (via
``bin/opl-sandbox-run``) as its ``[runner] command``, so every Spark task
runs as the isolated worker account through the root-owned launcher::

    /usr/local/libexec/opl-spark-launch

Launcher modes (fixed, installed by the lead)::

    clone --run ID --repo OWNER/NAME --ref SHA40 --branch NAME
    run   --run ID --model meta/MODEL [--variant V]   (packet on stdin,
        OpenCode JSON lines on stdout, exit code = OpenCode's)
    export --run ID --ref SHA40 --branch NAME         (git bundle on
        stdout; exit 3 = no new commits)

Model/variant validation and packet reading are reused from
``opl.conductor.spark.opencode`` (#12), not duplicated here. The runner
(``opl.conductor.spark.runner``) is unchanged: it keeps creating its own
worktree, checking ``OPL-RESULT``, and pushing.

Secrets: the worker API key travels only as an environment *name*
(``--preserve-env=OPL_WORKER_API_KEY`` to sudo). This module reads its
value only for a fail-closed substring check and never prints, logs, or
stores it. Standard library only.
"""

from __future__ import annotations

import argparse
import os
import random
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

from opl.conductor.spark.opencode import (
    TOOL_TYPES,
    convert_line,
    extract_usage,
    format_usage,
    read_packet,
    render_event,
    validate_model,
    validate_variant,
)

# The JSON-lines converter (TOOL_TYPES, extract_usage, render_event,
# convert_line, format_usage) is shared from opl.conductor.spark.opencode
# (issue #27): one parser, reused here, not duplicated.
from opl.conductor.spark.worktree import git_env

DEFAULT_LAUNCHER = "/usr/local/libexec/opl-spark-launch"
DEFAULT_WORKER = "opl-worker"
DEFAULT_WORKTREES_ROOT = "/srv/opl-worktrees"

#: Env name of the worker's own provider key. Only the name travels on
#: command lines; the value is only fail-closed-checked, never printed.
KEY_ENV = "OPL_WORKER_API_KEY"

#: Run IDs must satisfy the launcher's pattern.
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

#: Signal exit code used when the adapter itself is terminated.
SIGNAL_EXIT = 143

#: Heartbeat file the runner's stall detector watches (by mtime under
#: {workdir} only). Touched only when the worker repo really advances,
#: so an idle worker still stalls.
HEARTBEAT_NAME = ".opl-sandbox-heartbeat"

#: Seconds between worker-progress polls (tests override via env).
HEARTBEAT_INTERVAL_S = 5.0
HEARTBEAT_ENV = "OPL_SANDBOX_HEARTBEAT_S"

#: Override for the owner's private runner state holding bundle temp
#: files (never a shared /tmp path). Defaults to ~/.local/state/opl.
STATE_ENV = "OPL_SANDBOX_STATE_DIR"

#: The sudo child currently running, if any, for signal forwarding.
_child = None

#: Set when a terminal signal arrived: the import step is skipped.
_terminated = False


# -- workdir inspection ----------------------------------------------------

def parse_origin_slug(url):
    """Owner/name of a github.com remote URL, else raise ValueError.

    Accepts https, ssh:// and scp-like (git@host:path) forms. Local
    paths and foreign hosts fail closed: the launcher only clones public
    GitHub repos.
    """
    text = (url or "").strip()
    host = ""
    path = ""
    if "://" in text:
        _scheme, _, rest = text.partition("://")
        if "/" not in rest:
            raise ValueError("refusing non-GitHub origin %r" % (url,))
        host, _, path = rest.partition("/")
        host = host.split("@")[-1]
    elif "@" in text.split("/")[0] and ":" in text:
        head, _, path = text.partition(":")
        host = head.split("@")[-1]
    else:
        raise ValueError("refusing non-GitHub origin %r" % (url,))
    if host.lower() != "github.com":
        raise ValueError("refusing non-GitHub origin %r" % (url,))
    slug = path.strip().lower().rstrip("/")
    if slug.endswith(".git"):
        slug = slug[:-4]
    slug = slug.strip("/")
    if not slug or "/" not in slug:
        raise ValueError("refusing non-GitHub origin %r" % (url,))
    return slug


def _git(workdir, *args, env=None):
    proc = subprocess.run(
        ["git", "-C", workdir, *args],
        capture_output=True, text=True, timeout=120,
        env=git_env(env),
    )
    if proc.returncode != 0:
        raise ValueError("git %s failed: %s"
                         % (" ".join(args), proc.stderr.strip()[:200]))
    return proc.stdout.strip()


def workdir_info(workdir, *, git=_git):
    """(slug, base_sha, branch) for the runner's worktree.

    Slug comes from the ``origin`` URL (github.com only), base is the
    full HEAD SHA, branch is the current branch, or None on a detached HEAD.
    The runner detaches review and test worktrees at the PR head on
    purpose (they must not change the PR); such runs import nothing.
    """
    url = git(workdir, "config", "--get", "remote.origin.url")
    slug = parse_origin_slug(url)
    base = git(workdir, "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", base or ""):
        raise ValueError("HEAD is not a full SHA: %r" % (base,))
    try:
        branch = git(workdir, "symbolic-ref", "--short", "-q", "HEAD")
    except ValueError:
        branch = ""
    return slug, base, (branch.strip() or None)


def make_run_id(basename, *, now=None, randhex=None):
    """Unique launcher run ID: sanitized basename + UTC stamp + randomness."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    suffix = randhex or "%04x" % random.getrandbits(16)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", (basename or "").strip())
    safe = safe.strip(".-") or "run"
    if not safe[0].isalnum():
        safe = "run-" + safe
    run_id = "%s-%s-%s" % (safe, stamp, suffix)
    if len(run_id) > 100:
        keep = 100 - len(stamp) - len(suffix) - 2
        safe = safe[:keep].rstrip(".-") or "run"
        run_id = "%s-%s-%s" % (safe, stamp, suffix)
    if not RUN_ID_RE.match(run_id):
        raise ValueError("cannot make a valid run ID from %r" % (basename,))
    return run_id


# -- sudo / launcher argv ---------------------------------------------------

def sudo_argv(sudo_bin, worker, launcher, mode_args, *, preserve_key=False):
    """``sudo -n -u <worker> [--preserve-env=KEY] -- <launcher> ...``.

    Only the key's *name* appears; its value never does.
    """
    argv = [sudo_bin or "sudo", "-n", "-u", worker]
    if preserve_key:
        argv.append("--preserve-env=%s" % KEY_ENV)
    argv += ["--", launcher] + list(mode_args)
    return argv


# -- signal forwarding -------------------------------------------------------

def _forward(signum, _frame):
    """Forward a terminal signal to the sudo child, then exit 143."""
    global _child, _terminated
    _terminated = True
    child, _child = _child, None
    if child is not None:
        try:
            if signum == signal.SIGINT:
                child.send_signal(signal.SIGINT)
            else:
                child.terminate()
        except (OSError, ValueError):
            pass
        try:
            child.wait(timeout=30)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    raise SystemExit(SIGNAL_EXIT)


def _install_forwarding(child):
    """Route SIGTERM/SIGINT to the running sudo child. Returns old handlers."""
    global _child
    _child = child
    old = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            old[signum] = signal.signal(signum, _forward)
        except (OSError, ValueError):
            pass
    return old


def _restore_forwarding(old):
    global _child
    _child = None
    for signum, handler in old.items():
        try:
            signal.signal(signum, handler)
        except (OSError, ValueError):
            pass


# -- heartbeat for the runner's stall detector -------------------------------

def heartbeat_interval():
    """Seconds between progress polls (env override is test-only)."""
    try:
        value = float(os.environ.get(HEARTBEAT_ENV, "") or HEARTBEAT_INTERVAL_S)
    except ValueError:
        return HEARTBEAT_INTERVAL_S
    return value if value > 0 else HEARTBEAT_INTERVAL_S


def newest_mtime(root):
    """Newest mtime under root, excluding the .git subtree (0.0 if none)."""
    newest = 0.0
    git_dir = os.path.join(os.path.abspath(root), ".git")
    for dirpath, dirnames, filenames in os.walk(root):
        if os.path.abspath(dirpath) == git_dir or \
                os.path.abspath(dirpath).startswith(git_dir + os.sep):
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames
                       if os.path.join(os.path.abspath(dirpath), d) != git_dir]
        for name in filenames:
            try:
                stamp = os.path.getmtime(os.path.join(dirpath, name))
            except OSError:
                continue
            if stamp > newest:
                newest = stamp
    return newest


class Heartbeat(threading.Thread):
    """Touch the heartbeat file only while the worker repo really advances.

    The runner watches mtimes under {workdir} only, so each touch counts
    as progress there. A snapshot taken at start is the baseline: later
    touches happen only when the newest non-.git mtime under the worker
    repo moved past it, and an idle worker still stalls.
    """

    def __init__(self, worker_repo, heartbeat_path, interval):
        super().__init__(daemon=True)
        self.worker_repo = worker_repo
        self.heartbeat_path = heartbeat_path
        self.interval = interval
        self._halt = threading.Event()  # not _stop: that shadows Thread._stop (crash on 3.12)

    def stop(self):
        self._halt.set()
        self.join(timeout=30)

    def run(self):
        baseline = newest_mtime(self.worker_repo)
        while not self._halt.wait(self.interval):
            try:
                current = newest_mtime(self.worker_repo)
            except OSError:
                continue
            if current > baseline:
                baseline = current
                try:
                    with open(self.heartbeat_path, "a",
                              encoding="utf-8"):
                        pass
                    os.utime(self.heartbeat_path, None)
                except OSError:
                    pass


def ensure_excluded(workdir):
    """List the heartbeat file in the worktree's info/exclude, once.

    Keeps the runner's tree clean while the heartbeat exists; warn and
    continue when the exclude file cannot be updated.
    """
    try:
        rel = _git(workdir, "rev-parse", "--git-path", "info/exclude")
    except ValueError as exc:
        print("opl-sandbox-run: warning: %s" % exc, file=sys.stderr)
        return
    path = rel if os.path.isabs(rel) else os.path.join(workdir, rel)
    entry = "/%s" % HEARTBEAT_NAME
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        lines = []
    if entry in lines:
        return
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(entry + "\n")
    except OSError as exc:
        print("opl-sandbox-run: warning: cannot update %s: %s" % (path, exc),
              file=sys.stderr)


# -- bundle export / import ----------------------------------------------------

def state_dir():
    """Owner-private runner state dir for bundle temp files."""
    configured = (os.environ.get(STATE_ENV, "") or "").strip()
    base = configured or os.path.join(os.path.expanduser("~"),
                                      ".local", "state", "opl")
    bundles = os.path.join(base, "sandbox-bundles")
    os.makedirs(bundles, exist_ok=True)
    try:
        os.chmod(bundles, 0o700)
    except OSError:
        pass
    return bundles


def export_and_import(*, sudo_bin, worker, launcher, run_id, base, branch,
                      workdir, spawn=subprocess.Popen):
    """Fetch the worker's new commits into the owner's worktree.

    The bundle lands in a temp file under the owner's private runner
    state (never a shared /tmp path). Exit 3 means no new commits and
    imports nothing; exit 0 fetches the bundle as refs/heads/<branch>
    and fast-forwards it. No git command ever runs inside the worker's
    repository from the owner side. The bundle file is always deleted.
    """
    export_cmd = sudo_argv(sudo_bin, worker, launcher, [
        "export", "--run", run_id, "--ref", base, "--branch", branch])
    fd, bundle = tempfile.mkstemp(prefix="opl-sandbox-",
                                   suffix=".bundle", dir=state_dir())
    try:
        with os.fdopen(fd, "wb") as handle:
            # Binary stdout: the bundle streams straight into the file.
            child = spawn(export_cmd, stdout=handle,
                          stderr=subprocess.PIPE)
            old_handlers = _install_forwarding(child)
            try:
                child.communicate()
            finally:
                _restore_forwarding(old_handlers)
        if _terminated:
            raise SystemExit(SIGNAL_EXIT)
        if child.returncode == 3:
            return "empty"
        if child.returncode != 0:
            raise ValueError("launcher export failed (exit %d)"
                             % child.returncode)
        fetch = subprocess.run(
            ["git", "-C", workdir, "fetch", "--no-tags", bundle,
             "refs/heads/%s" % branch],
            capture_output=True, text=True, timeout=300, env=git_env())
        if fetch.returncode != 0:
            raise ValueError("bundle fetch failed: %s"
                             % fetch.stderr.strip()[:300])
        merge = subprocess.run(
            ["git", "-C", workdir, "merge", "--ff-only", "FETCH_HEAD"],
            capture_output=True, text=True, timeout=300, env=git_env())
        if merge.returncode != 0:
            raise ValueError("bundle merge is not a fast-forward: %s"
                             % merge.stderr.strip()[:300])
        return "imported"
    finally:
        try:
            os.unlink(bundle)
        except OSError:
            pass


# -- clone + run --------------------------------------------------------------

def _key_value():
    """The worker key value, or "" when missing (fail-closed by callers)."""
    return os.environ.get(KEY_ENV, "") or ""


def _packet_ok(packet_text):
    """Refuse when the packet contains the worker key value (never logged)."""
    secret = _key_value()
    if secret and len(secret) >= 4 and secret in packet_text:
        raise ValueError("refusing: packet contains the worker key value")


def _stream_run(run_cmd, prompt, spawn):
    """Run the worker, printing converted lines. Returns (exit, totals)."""
    child = spawn(run_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                  stderr=subprocess.PIPE, text=True, bufsize=1)
    old_handlers = _install_forwarding(child)
    totals = None
    usage_seen = False
    try:
        try:
            child.stdin.write(prompt)
        except (OSError, ValueError):
            pass
        try:
            child.stdin.close()
        except (OSError, ValueError):
            pass
        for raw in iter(child.stdout.readline, ""):
            rendered, usage = convert_line(raw)
            if usage is not None:
                if not usage_seen:
                    totals = [0, 0, 0.0]
                    usage_seen = True
                totals[0] += usage[0]
                totals[1] += usage[1]
                totals[2] += usage[2]
            if rendered:
                for out_line in rendered:
                    print(out_line, flush=True)
        child.wait()
        try:
            tail = child.stderr.read() or ""
        except (OSError, ValueError):
            tail = ""
        if tail.strip():
            print(tail.strip(), file=sys.stderr)
    finally:
        _restore_forwarding(old_handlers)
    return child.returncode, (tuple(totals) if usage_seen else None)


def run_sandbox(*, packet_path, workdir, model, variant, launcher,
                worker, worktrees_root, sudo_bin="sudo",
                spawn=subprocess.Popen):
    """Clone, run, stream JSON lines as text, import the bundle.

    Returns the run's exit code. On SIGTERM/SIGINT the signal is
    forwarded to the sudo child, the import is skipped, cleanup runs,
    and the process exits 143.
    """
    global _terminated
    _terminated = False
    if not os.path.isdir(workdir):
        raise ValueError("workdir is not a directory: %s" % workdir)
    prompt = read_packet(packet_path)
    clean_model = validate_model(model)
    clean_variant = validate_variant(variant)
    _packet_ok(prompt)
    if not _key_value():
        raise ValueError("missing %s in the environment" % KEY_ENV)

    slug, base, branch = workdir_info(workdir)
    run_id = make_run_id(os.path.basename(os.path.abspath(workdir)))
    # Detached (review/test/fix at the PR head): the worker still needs a
    # branch in its own clone. New commits come back onto the detached
    # HEAD (fix runs push HEAD); reviews/tests commit nothing (export exit 3).
    detached = branch is None
    clone_branch = "opl/detached-%s" % run_id if detached else branch

    clone_cmd = sudo_argv(sudo_bin, worker, launcher, [
        "clone", "--run", run_id, "--repo", slug,
        "--ref", base, "--branch", clone_branch])
    proc = spawn(clone_cmd, stdout=subprocess.PIPE,
                 stderr=subprocess.PIPE, text=True)
    _out, err = proc.communicate()
    if proc.returncode != 0:
        raise ValueError("launcher clone failed: %s" % (err or "").strip()[:300])

    worker_repo = os.path.join(worktrees_root, run_id, "repo")
    heartbeat_path = os.path.join(workdir, HEARTBEAT_NAME)
    ensure_excluded(workdir)
    monitor = Heartbeat(worker_repo, heartbeat_path, heartbeat_interval())
    monitor.start()
    try:
        run_cmd = sudo_argv(sudo_bin, worker, launcher, [
            "run", "--run", run_id, "--model", clean_model]
            + (["--variant", clean_variant] if clean_variant else []),
            preserve_key=True)
        run_code, totals = _stream_run(run_cmd, prompt, spawn)
        for usage_line in format_usage(totals):
            print(usage_line, flush=True)
        sys.stdout.flush()
        if _terminated:
            raise SystemExit(SIGNAL_EXIT)
        export_and_import(sudo_bin=sudo_bin, worker=worker,
                          launcher=launcher, run_id=run_id, base=base,
                          branch=clone_branch, workdir=workdir, spawn=spawn)
        return run_code
    finally:
        monitor.stop()
        try:
            os.unlink(heartbeat_path)
        except OSError:
            pass


def main(argv=None, *, spawn=subprocess.Popen):
    parser = argparse.ArgumentParser(prog="opl-sandbox-run")
    parser.add_argument("--packet", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--variant", default="")
    parser.add_argument("--launcher", default=DEFAULT_LAUNCHER)
    parser.add_argument("--worker", default=DEFAULT_WORKER)
    parser.add_argument("--worktrees-root", default=DEFAULT_WORKTREES_ROOT)
    parser.add_argument("--sudo-bin", default="sudo")
    args = parser.parse_args(argv)
    try:
        return run_sandbox(
            packet_path=args.packet, workdir=args.workdir,
            model=args.model, variant=args.variant,
            launcher=args.launcher, worker=args.worker,
            worktrees_root=args.worktrees_root,
            sudo_bin=args.sudo_bin, spawn=spawn)
    except ValueError as exc:
        # Never echo secret values: messages here name variables only.
        print("opl-sandbox-run: error: %s" % exc, file=sys.stderr)
        return 2
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else SIGNAL_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
