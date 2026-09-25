#!/usr/bin/env python3
"""Tests: no tracked file leaks a private machine path (TH.13).

Patterns are built dynamically so this file itself stays clean.
"""

import os
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BS = chr(92)


def machine_path_patterns():
    """Windows drive paths and Unix home directories."""
    return ["C:" + "/", "C:" + BS, "/" + "Users/", "/" + "home/"]


def tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True,
                         text=True, timeout=60, check=True)
    return out.stdout.splitlines()


class PublicationTests(unittest.TestCase):
    def test_no_tracked_file_has_a_machine_path(self):
        patterns = machine_path_patterns()
        offenders = []
        for rel in tracked_files():
            path = os.path.join(REPO, rel)
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            try:
                text = data.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                continue  # binary asset, nothing to scan
            found = sorted({p for p in patterns if p in text})
            if found:
                offenders.append("%s: %s" % (rel, ",".join(found)))
        self.assertEqual(offenders, [])

    def test_workflow_references_no_template_path(self):
        path = os.path.join(REPO, ".github", "workflows", "project-sync.yml")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("C:" + "/", text)
        self.assertNotIn("C:" + BS, text)
        self.assertIn("board", text.lower())

    def test_golden_has_single_trailing_newline(self):
        path = os.path.join(REPO, "tests", "golden", "admin_small.rb")
        with open(path, "rb") as fh:
            data = fh.read()
        self.assertTrue(data.endswith(b"\n"))
        self.assertFalse(data.endswith(b"\n\n"))

    def test_configure_warns_instance_wide(self):
        path = os.path.join(REPO, "docs", "CONFIGURE.md")
        with open(path, encoding="utf-8") as fh:
            text = fh.read().lower()
        self.assertIn("instance-wide", text)
        self.assertIn("dedicated", text)
        self.assertIn("back up", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
