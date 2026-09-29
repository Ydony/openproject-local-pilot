"""opl-opencode-run worker adapter entry point (T5.1).

Invoked as ``python -m opl.conductor.spark.opencode_cli`` by the
``bin/opl-opencode-run`` launcher (which is the `[runner] command` the
conductor spawns through ``supervisor.run_worker``).

Translates ``--packet/--workdir/--model/--variant/--opencode-bin`` into
``opencode run --model M [--variant V] --dir <workdir> <prompt>`` where
<prompt> is the packet file's content. Output passes through so the
supervisor log keeps the final OPL-RESULT/REVIEW/TEST line as evidence.

Secrets: the packet is already scrubbed by the runner; this module reads
no secret values, prints none, and refuses to exec when a known
worker-secret value appears in the prompt. Standard library only.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from opl.conductor.spark.opencode import (
    BIN_ENV,
    build_command,
    read_packet,
    resolve_bin,
)


def _worker_secret_values():
    """Values of allowlisted worker env names (length >= 4 only)."""
    # Names come from argv/env configuration, never committed files.
    # Values are only used for a fail-closed substring check and are
    # never printed, stored, or placed in argv.
    names = (os.environ.get("OPL_WORKER_ENV_NAMES", "") or "").split(",")
    values = []
    for name in (n.strip() for n in names):
        if not name:
            continue
        value = os.environ.get(name, "")
        if value and len(value) >= 4:
            values.append(value)
    return values


def main(argv=None):
    parser = argparse.ArgumentParser(prog="opl-opencode-run")
    parser.add_argument("--packet", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--variant", default="")
    parser.add_argument("--opencode-bin", default="")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.workdir):
        print("opl-opencode-run: error: workdir is not a directory: %s" % args.workdir,
              file=sys.stderr)
        return 2
    try:
        prompt = read_packet(args.packet)
    except ValueError as exc:
        print("opl-opencode-run: error: %s" % exc, file=sys.stderr)
        return 2
    try:
        cmd = build_command(
            resolve_bin(args.opencode_bin or None),
            args.model, args.variant, args.workdir, prompt,
            forbidden=_worker_secret_values(),
        )
    except ValueError as exc:
        print("opl-opencode-run: error: %s" % exc, file=sys.stderr)
        return 2

    # The child inherits this process's (already isolated) environment;
    # nothing credential-like is added here. The binary name is logged,
    # never any value.
    try:
        proc = subprocess.run(cmd, cwd=args.workdir)
    except FileNotFoundError:
        print("opl-opencode-run: error: opencode binary not found: %s"
              % (os.environ.get(BIN_ENV, "") and BIN_ENV or cmd[0]),
              file=sys.stderr)
        return 127
    except OSError as exc:
        print("opl-opencode-run: error: cannot start opencode (%s)" % type(exc).__name__,
              file=sys.stderr)
        return 126
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
