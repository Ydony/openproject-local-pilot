"""Fake worker: the model provider refuses the account (#66)."""

import sys

print("[error] APIError 402 (not retryable): Billing verification failed. "
      "Please check your payment method.")
print("usage unknown")
sys.exit(1)
