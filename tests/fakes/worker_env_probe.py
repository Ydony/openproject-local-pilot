#!/usr/bin/env python3
"""Fake worker: report what the environment exposes, then finish."""

import os

print("ENVKEYS:" + ",".join(sorted(os.environ)))
home = os.path.expanduser("~")
print("HOME:" + home)
canary = os.path.join(home, "opl-canary.txt")
print("CANARY:" + ("visible" if os.path.exists(canary) else "absent"))
print("APIKEY:" + ("set" if os.environ.get("OPL_WORKER_KEY") else "unset"))
print("OPL-RESULT: DONE probe")
