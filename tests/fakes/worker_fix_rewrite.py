"""Fake worker: rewrites the PR head (amend, like a rebase), reports DONE."""

import os
import subprocess
import sys

with open(os.path.join(os.getcwd(), "rebased.txt"), "w", encoding="utf-8") as fh:
    fh.write("rebased\n")
subprocess.run(["git", "add", "rebased.txt"], check=True)
subprocess.run(
    ["git", "-c", "user.email=t@t", "-c", "user.name=t",
     "commit", "-q", "--amend", "-m", "rewritten head"],
    check=True,
)
print("OPL-RESULT: DONE rebased")
sys.exit(0)
