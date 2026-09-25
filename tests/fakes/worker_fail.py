"""Fake worker: fails immediately."""

import sys

print("something broke", file=sys.stderr)
sys.exit(1)
