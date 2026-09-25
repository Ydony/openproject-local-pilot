"""Fake worker: prints forever (timeout, never stall)."""

import time

while True:
    print("still working...", flush=True)
    time.sleep(0.2)
