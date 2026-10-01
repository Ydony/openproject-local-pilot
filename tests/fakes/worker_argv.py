"""Fake worker: records its argv beside the worktrees, then commits like
worker_commit.py (issue #45: the runner passes --runtime/--setup-b64)."""

import os
import runpy
import sys

with open(os.path.join(os.path.dirname(os.getcwd()), "argv.txt"), "w",
          encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]))
runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "worker_commit.py"), run_name="__main__")
