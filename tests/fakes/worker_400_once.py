"""Fake worker: the first run dies of a provider 400 (#82); every run logs the
--variant it was given beside the worktrees, then the second one commits."""

import os
import runpy
import sys

variant = sys.argv[sys.argv.index("--variant") + 1]
root = os.path.dirname(os.getcwd())
with open(os.path.join(root, "variants.log"), "a", encoding="utf-8") as fh:
    fh.write(variant + "\n")
marker = os.path.join(root, "bad-request.marker")
if not os.path.exists(marker):
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write("seen")
    print("[error] APIError 400 (not retryable): The request contains invalid parameters")
    print("usage unknown")
    sys.exit(1)
runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "worker_commit.py"), run_name="__main__")
