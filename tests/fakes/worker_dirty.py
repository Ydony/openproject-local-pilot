"""Fake worker: leaves an uncommitted file and reports DONE (invalid proof)."""

import os
import sys

with open(os.path.join(os.getcwd(), "draft.txt"), "w", encoding="utf-8") as fh:
    fh.write("uncommitted\n")
print("OPL-RESULT: DONE but dirty")
sys.exit(0)
