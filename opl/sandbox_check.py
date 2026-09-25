"""Owner-run DAC canary; not proof of a complete hostile-code sandbox.

Creates synthetic probes only; never reads or prints configuration contents.
Only an explicit PermissionError from a working low-privilege process counts
as denial. sudo/auth/interpreter failures are failures, never false passes.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile

LABELS = ("private sibling read", "outside-worktree write", "owner config read")

# Exit 13 is a proven permission denial, 0 means the forbidden access worked,
# and 70 means a broken probe. Nothing read from a target is printed.
PROBE = r'''
import os, sys, tempfile
kind, target = sys.argv[1:]
try:
    if kind in ("inside-write", "outside-write"):
        with tempfile.TemporaryFile(dir=target):
            pass
    elif kind == "private-read":
        fd = os.open(target, os.O_RDONLY)
        os.close(fd)
    elif kind == "owner-config":
        allowed = False
        try:
            with os.scandir(target) as entries:
                next(entries, None)
            allowed = True
        except PermissionError:
            pass
        try:
            fd = os.open(os.path.join(target, "opl.toml"), os.O_RDONLY)
            os.close(fd)
            allowed = True
        except PermissionError:
            pass
        if not allowed:
            raise PermissionError()
    else:
        sys.exit(70)
except PermissionError:
    sys.exit(13)
except OSError:
    sys.exit(70)
sys.exit(0)
'''


def _under(path, root):
    return path == root or root in path.parents


def check(worker, worktree, outside, config, *, run=subprocess.run, owner_uid=None):
    """Check permissions with injected process runner for offline fake tests."""
    if owner_uid is None:
        owner_uid = os.getuid()
    failed = False

    def report(label, ok):
        nonlocal failed
        print(("PASS" if ok else "FAIL") + ": " + label)
        failed |= not ok

    def fail_all():
        for label in LABELS:
            report(label, False)
        return 1

    try:
        if not re.fullmatch(r"[a-z_][a-z0-9_-]*[$]?", worker):
            raise ValueError("invalid worker")
        worktree, outside, config = (Path(p).resolve(strict=True)
                                     for p in (worktree, outside, config))
        if (not all(p.is_dir() for p in (worktree, outside, config))
                or _under(outside, worktree) or _under(config, worktree)
                or outside == Path(outside.anchor)
                or not (config / "opl.toml").is_file()):
            raise ValueError("invalid probe targets")
    except (OSError, ValueError, RuntimeError):
        print("FAIL: preflight (existing separate targets required)")
        return fail_all()

    prefix = ["sudo", "-n", "-H", "-u", worker, "--"]

    def invoke(args):
        try:
            return run(prefix + args, capture_output=True, text=True, timeout=20,
                       stdin=subprocess.DEVNULL,
                       env={"PATH": os.defpath, "LANG": "C"})
        except (OSError, subprocess.SubprocessError):
            return None

    identity = invoke(["/usr/bin/id", "-u"])
    try:
        uid = int(identity.stdout.strip()) if identity and identity.returncode == 0 else -1
    except ValueError:
        uid = -1
    distinct = uid > 0 and uid != owner_uid
    report("different non-root worker identity", distinct)
    if not distinct:
        return fail_all()

    def probe(kind, target):
        result = invoke(["/usr/bin/python3", "-I", "-c", PROBE, kind, str(target)])
        return result.returncode if result is not None else None

    writable = probe("inside-write", worktree) == 0
    report("worktree positive control", writable)
    if not writable:
        return fail_all()

    # The directory itself is traversable, so the read probe tests the 0600
    # file rather than succeeding solely because a parent cannot be reached.
    probe_dir = None
    canary = None
    try:
        probe_dir = Path(tempfile.mkdtemp(prefix=".opl-canary-", dir=outside))
        probe_dir.chmod(0o755)
        canary = probe_dir / "private-canary"
        fd = os.open(canary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(b"synthetic permission canary\n")
        canary.chmod(0o600)
        for label, kind, target in (
                (LABELS[0], "private-read", canary),
                (LABELS[1], "outside-write", outside),
                (LABELS[2], "owner-config", config)):
            report(label, probe(kind, target) == 13)
    except OSError:
        print("FAIL: probe setup")
        return fail_all()
    finally:
        # Exact generated targets only; no recursive deletion or user files.
        try:
            if canary is not None:
                canary.unlink(missing_ok=True)
            if probe_dir is not None:
                probe_dir.rmdir()
        except OSError:
            report("synthetic probe cleanup", False)
    return int(failed)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--outside-dir", required=True)
    parser.add_argument("--owner-config", default=str(Path.home() / ".config" / "opl"))
    args = parser.parse_args(argv)
    if os.name != "posix":
        print("FAIL: run this canary inside Linux/WSL, not Windows Python")
        return 1
    return check(args.worker, args.worktree, args.outside_dir, args.owner_config)


if __name__ == "__main__":
    raise SystemExit(main())
