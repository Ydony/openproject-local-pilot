#!/usr/bin/env python3
"""Fake worker that leaks a known secret into its output, then fails."""

import base64

SECRET = "leaky-s3cret-value"
print("debug dump: token=%s" % SECRET)
print("auth: %s" % base64.b64encode(("x-access-token:" + SECRET).encode()).decode())
print("OPL-RESULT: FAILED could not finish with %s" % SECRET)
raise SystemExit(1)
