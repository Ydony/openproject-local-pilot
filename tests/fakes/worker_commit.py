"""Fake worker: commits one file in the workdir (its cwd) and reports DONE."""

import os
import subprocess
import sys

path = os.path.join(os.getcwd(), "hello.txt")
with open(path, "w", encoding="utf-8") as fh:
    fh.write("hello from spark\n")
subprocess.run(["git", "add", "hello.txt"], check=True)
subprocess.run(
    ["git", "-c", "user.email=t@t", "-c", "user.name=t",
     "commit", "-qm", "spark work"],
    check=True,
)
print("OPL-RESULT: DONE committed")
sys.exit(0)
