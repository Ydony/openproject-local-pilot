"""Fake Spark worker for the end-to-end test: one command, every run kind.

Called as `worker_e2e.py <packet>` with the worktree as its cwd. The packet
file name tells the kind: build/fix packets commit a file and report DONE,
review packets pass (or ask for changes when OPL_E2E_REVIEW=changes).
"""

import os
import subprocess
import sys

packet = os.path.basename(sys.argv[1])
if packet.startswith("review-"):
    print("read the diff")
    if os.environ.get("OPL_E2E_REVIEW") == "changes":
        print("OPL-REVIEW: CHANGES please add a test")
    else:
        print("OPL-REVIEW: PASS looks good")
    sys.exit(0)
if packet.startswith("test-"):
    print("OPL-TEST: PASS all criteria met")
    sys.exit(0)
name = "work-%s.txt" % packet.split(".")[0]
with open(name, "w", encoding="utf-8") as fh:
    fh.write("change for %s\n" % packet)
subprocess.run(["git", "add", name], check=True)
subprocess.run(["git", "-c", "user.email=spark@example.invalid",
                "-c", "user.name=spark", "commit", "-qm", "spark: " + packet],
               check=True)
print("OPL-RESULT: DONE committed %s" % name)
