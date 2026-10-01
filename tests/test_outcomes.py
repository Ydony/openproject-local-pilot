#!/usr/bin/env python3
"""Tests for outcome helpers: final-line gating and WIP commits."""

import os
import shutil
import subprocess
import tempfile
import unittest

from opl.conductor.spark.outcomes import (
    final_summary,
    keep_partial_work,
    parse_final_line,
)


class ParseTests(unittest.TestCase):
    def test_done(self):
        self.assertEqual(parse_final_line(["a", "OPL-RESULT: DONE all green"]),
                         ("done", ""))

    def test_done_case_insensitive_and_last_wins(self):
        self.assertEqual(
            parse_final_line(["OPL-RESULT: FAILED x", "OPL-RESULT: done"]),
            ("done", ""))

    def test_failed_with_message(self):
        self.assertEqual(parse_final_line(["OPL-RESULT: FAILED kaboom"]),
                         ("failed", "kaboom"))

    def test_failed_bare(self):
        status, message = parse_final_line(["OPL-RESULT: FAILED"])
        self.assertEqual(status, "failed")
        self.assertTrue(message)

    def test_missing(self):
        status, message = parse_final_line(["nothing here"])
        self.assertEqual(status, "failed")
        self.assertIn("missing OPL-RESULT", message)

    def test_empty(self):
        self.assertEqual(parse_final_line([])[0], "failed")


class MarkdownWrappedTests(unittest.TestCase):
    """#74: models wrap the result line in markdown; it still counts."""

    def test_backticked_done_line_counts(self):
        line = "`OPL-RESULT: DONE boards fixed; no node here`"
        self.assertEqual(parse_final_line(["text", line]), ("done", ""))
        self.assertEqual(final_summary([line]), "boards fixed; no node here")

    def test_bold_and_quoted_lines_count(self):
        self.assertEqual(parse_final_line(["**OPL-RESULT: FAILED broke**"]),
                         ("failed", "broke"))
        self.assertEqual(parse_final_line(["> OPL-RESULT: DONE x"]),
                         ("done", ""))

    def test_result_text_in_the_middle_of_a_sentence_does_not_count(self):
        self.assertEqual(parse_final_line(["I will print OPL-RESULT: DONE soon"]),
                         ("failed", "missing OPL-RESULT line"))


class SummaryTests(unittest.TestCase):
    def test_done_text_is_the_summary(self):
        self.assertEqual(final_summary(["a", "OPL-RESULT: DONE all green"]),
                         "all green")

    def test_bare_done_has_no_summary(self):
        self.assertEqual(final_summary(["OPL-RESULT: DONE"]), "")

    def test_failed_has_no_summary(self):
        self.assertEqual(final_summary(["OPL-RESULT: FAILED kaboom"]), "")

    def test_missing_has_no_summary(self):
        self.assertEqual(final_summary(["nothing here"]), "")

    def test_last_done_wins_and_trims(self):
        long_text = "x" * 600
        self.assertEqual(final_summary(["OPL-RESULT: DONE %s" % long_text]),
                         "x" * 500)


class WipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opl-wip-")
        subprocess.run(["git", "init", "-q", "-b", "main", self.tmp],
                       check=True, timeout=60)
        subprocess.run(["git", "-C", self.tmp, "config", "user.email", "t@t"],
                       check=True, timeout=60)
        subprocess.run(["git", "-C", self.tmp, "config", "user.name", "t"],
                       check=True, timeout=60)
        with open(os.path.join(self.tmp, "base.txt"), "w") as fh:
            fh.write("base\n")
        subprocess.run(["git", "-C", self.tmp, "add", "-A"], check=True, timeout=60)
        subprocess.run(["git", "-C", self.tmp, "commit", "-qm", "base"],
                       check=True, timeout=60)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _log(self):
        out = subprocess.run(["git", "-C", self.tmp, "log", "--oneline"],
                             capture_output=True, text=True, timeout=60,
                             check=True)
        return out.stdout

    def test_clean_tree_commits_nothing(self):
        self.assertFalse(keep_partial_work(self.tmp, 5, 1))
        self.assertNotIn("WIP", self._log())

    def test_dirty_tree_gets_wip_commit(self):
        with open(os.path.join(self.tmp, "half.txt"), "w") as fh:
            fh.write("half\n")
        self.assertTrue(keep_partial_work(self.tmp, 5, 1))
        self.assertIn("WIP: partial work, run 5-1", self._log())


if __name__ == "__main__":
    unittest.main(verbosity=2)
