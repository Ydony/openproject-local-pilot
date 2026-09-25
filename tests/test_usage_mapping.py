#!/usr/bin/env python3
"""Tests: the usage-mapping doc covers every required piece (TC.1, TH.12)."""

import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(REPO, "docs", "USAGE-MAPPING.md")


class UsageMappingTests(unittest.TestCase):
    def test_all_three_tools_covered(self):
        body = open(PATH, encoding="utf-8").read()
        for needle in ("Claude Code", "Codex", "OpenCode"):
            self.assertIn(needle, body)

    def test_each_tool_has_location_and_join(self):
        body = open(PATH, encoding="utf-8").read()
        for needle in (".claude/projects", ".codex/sessions",
                       "opencode.db", "sessionFile", "modelName"):
            self.assertIn(needle, body)

    def test_ccusage_pinned_with_command(self):
        body = open(PATH, encoding="utf-8").read()
        self.assertIn("20.0.24", body)
        self.assertIn("session --json", body)

    def test_synthetic_sample_and_cache_note(self):
        body = open(PATH, encoding="utf-8").read()
        for needle in ("sessionId", "modelBreakdowns", "cacheReadTokens",
                       "reasoningOutputTokens", "synthetic"):
            self.assertIn(needle, body)

    def test_unknowns_stay_unknown(self):
        body = open(PATH, encoding="utf-8").read().lower()
        self.assertIn("unknown", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
