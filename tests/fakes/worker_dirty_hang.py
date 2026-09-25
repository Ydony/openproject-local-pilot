"""Fake worker: leaves partial work, then hangs (timeout with dirt)."""

import time

with open("partial.txt", "w", encoding="utf-8") as fh:
    fh.write("half done\n")
while True:
    print("still working...", flush=True)
    time.sleep(0.2)
