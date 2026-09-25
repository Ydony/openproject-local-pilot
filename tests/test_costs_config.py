#!/usr/bin/env python3
"""Tests for cost config: price/estimate loaders, validation, math."""

import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRICES = os.path.join(REPO, "config", "prices.toml")
ESTIMATES = os.path.join(REPO, "config", "estimates.toml")

from opl.costs_config import (
    estimate_cost,
    load_estimates,
    load_prices,
    match_model,
    token_cost,
)


def write_temp(body):
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".toml",
                                     delete=False) as fh:
        fh.write(body)
        return fh.name


MODELS = """[[model]]
match = "claude-sonnet-*"
input = 3.0
output = 15.0
cache_read = 0.3
cache_write = 3.75
billing = "subscription"
as_of = "2026-09-25"
[[model]]
match = "claude-opus-*"
input = 5.0
output = 25.0
cache_read = 0.5
cache_write = 6.25
billing = "subscription"
as_of = "2026-09-25"
[default]
claude = "claude-sonnet-*"
codex = "claude-sonnet-*"
spark = "claude-sonnet-*"
"""


class PriceTests(unittest.TestCase):
    def test_shipped_defaults_cover_model_users(self):
        prices = load_prices(PRICES)
        for login in ("claude", "codex", "spark"):
            self.assertIn(login, prices["defaults"])
        self.assertTrue(prices["models"])

    def test_shipped_entries_have_required_keys(self):
        prices = load_prices(PRICES)
        for entry in prices["models"]:
            for key in ("match", "input", "output", "cache_read",
                        "cache_write", "billing", "as_of"):
                self.assertIn(key, entry, key)
            for key in ("input", "output", "cache_read", "cache_write"):
                self.assertGreaterEqual(entry[key], 0.0)
            self.assertIn(entry["billing"], ("api", "subscription"))

    def test_first_glob_match_wins(self):
        prices = load_prices(PRICES)
        sonnet = match_model(prices["models"], "claude-sonnet-4-6")
        opus = match_model(prices["models"], "claude-opus-4-6")
        self.assertEqual((sonnet["input"], sonnet["output"]), (3.0, 15.0))
        self.assertEqual((opus["input"], opus["output"]), (5.0, 25.0))
        self.assertIsNone(match_model(prices["models"], "mystery-9"))

    def test_missing_default_rejected(self):
        path = write_temp(MODELS.replace('spark = "claude-sonnet-*"',
                                         'spark = "nope-*"'))
        try:
            with self.assertRaises(ValueError):
                load_prices(path)
        finally:
            os.unlink(path)

    def test_missing_user_rejected(self):
        path = write_temp(MODELS)
        try:
            with self.assertRaises(ValueError):
                load_prices(path, users=("claude", "watson"))
        finally:
            os.unlink(path)

    def test_negative_price_rejected(self):
        path = write_temp(MODELS.replace("input = 3.0", "input = -3.0"))
        try:
            with self.assertRaises(ValueError):
                load_prices(path)
        finally:
            os.unlink(path)


class EstimateTests(unittest.TestCase):
    def test_shipped_estimates_cover_model_sizes(self):
        estimates = load_estimates(ESTIMATES)
        for login in ("claude", "codex", "spark"):
            for size in ("S", "M", "L"):
                row = estimates[(login, size)]
                self.assertGreater(row["input_tokens"], 0)
                self.assertGreater(row["output_tokens"], 0)

    def test_unknown_size_rejected(self):
        path = write_temp('[claude.S]\ninput_tokens = 1\noutput_tokens = 1\n')
        try:
            with self.assertRaises(ValueError):
                load_estimates(path)
        finally:
            os.unlink(path)


class MathTests(unittest.TestCase):
    def test_token_cost_uses_all_bands(self):
        entry = {"match": "x", "input": 3.0, "output": 15.0,
                 "cache_read": 0.3, "cache_write": 3.75,
                 "billing": "subscription", "as_of": "2026-09-25"}
        cost = token_cost(entry, input_tokens=1_000_000,
                          output_tokens=1_000_000,
                          cache_read_tokens=1_000_000,
                          cache_write_tokens=1_000_000)
        self.assertAlmostEqual(cost, 3.0 + 15.0 + 0.3 + 3.75)

    def test_subscription_priced_at_api_rates(self):
        # billing is display-only: it must not change the math.
        base = {"match": "x", "input": 3.0, "output": 15.0,
                "cache_read": 0.3, "cache_write": 3.75,
                "as_of": "2026-09-25"}
        sub = dict(base, billing="subscription")
        api = dict(base, billing="api")
        kwargs = {"input_tokens": 100, "output_tokens": 200,
                  "cache_read_tokens": 300, "cache_write_tokens": 400}
        self.assertEqual(token_cost(sub, **kwargs), token_cost(api, **kwargs))

    def test_estimate_uses_default_entry(self):
        prices = load_prices(PRICES)
        estimates = load_estimates(ESTIMATES)
        cost = estimate_cost("spark", "S", prices, estimates)
        row = estimates[("spark", "S")]
        table = next(m for m in prices["models"] if m["match"] == "*spark*")
        expected = (row["input_tokens"] / 1_000_000 * table["input"]
                    + row["output_tokens"] / 1_000_000 * table["output"])
        self.assertAlmostEqual(cost, expected)
        self.assertEqual(table["match"], "*spark*")
    def test_estimate_unknown_model_or_size(self):
        prices = load_prices(PRICES)
        estimates = load_estimates(ESTIMATES)
        with self.assertRaises(ValueError):
            estimate_cost("nobody", "S", prices, estimates)
        with self.assertRaises(ValueError):
            estimate_cost("spark", "XL", prices, estimates)


if __name__ == "__main__":
    unittest.main(verbosity=2)
