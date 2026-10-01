"""Fake worker: exits before reaching the model, no usage, no error (#66)."""

import sys

print("usage unknown")
sys.exit(1)
