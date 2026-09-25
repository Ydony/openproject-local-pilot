#!/usr/bin/env python3
"""Tests for outcome helpers: final-line gating and WIP commits."""

import os
import shutil
import subprocess
import tempfile
import unittest

from opl.conductor.spark.outcomes import keep_partial_work, parse_final_line


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
