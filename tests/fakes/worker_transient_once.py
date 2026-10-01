"""Fake worker: the first run dies of a transient database error (#77), the
second one commits and reports DONE. The marker lives beside the worktrees."""

import os
import runpy
import sys

marker = os.path.join(os.path.dirname(os.getcwd()), "transient.marker")
if not os.path.exists(marker):
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write("seen")
    print("[error] UnknownError: Failed to execute statement")
    print("usage unknown")
    sys.exit(1)
runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "worker_commit.py"), run_name="__main__")
