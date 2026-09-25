"""Fake worker: applies review feedback, commits, reports DONE."""

import os
import subprocess
import sys

path = os.path.join(os.getcwd(), "fix.txt")
with open(path, "w", encoding="utf-8") as fh:
    fh.write("addressed review feedback\n")
subprocess.run(["git", "add", "fix.txt"], check=True)
subprocess.run(
    ["git", "-c", "user.email=t@t", "-c", "user.name=t",
     "commit", "-qm", "fix work"],
    check=True,
)
print("OPL-RESULT: DONE fixed")
sys.exit(0)
