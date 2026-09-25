#!/usr/bin/env python3
"""Tests for the estimate calibration report: medians, p90, suggestions."""

import unittest

from opl.calibration import calibrate, format_calibration


def task(tid, assignee, size, tokens):
    from opl.conductor.state import Item

    return Item(id=tid, project="demo", type="Task", status="Merged",
                status_since="2026-09-20T12:00:00Z", parent_id=2,
                assignee=assignee, size=size, actual_tokens=tokens)


def world_of(*tasks):
    from opl.conductor.state import Project, World

    projects = {
        "demo": Project(key="demo", op_id=1, repo="example-owner/demo",
                        visibility="Public", has_test_env=False,
                        test_signal=None, prod_signal=None),
    }
    return World(now="2026-09-24T12:00:00Z", projects=projects,
                 items={t.id: t for t in tasks}, pull_requests={})


ESTIMATES = {
    ("claude", "S"): {"input_tokens": 100000, "output_tokens": 20000},
    ("spark", "S"): {"input_tokens": 100000, "output_tokens": 20000},
}


class CalibrateTests(unittest.TestCase):
    def test_median_and_p90(self):
        world = world_of(task(5, "claude", "S", 100),
                         task(6, "claude", "S", 200),
                         task(7, "claude", "S", 300))
        rows = calibrate(world, ESTIMATES)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["model"], row["size"]), ("claude", "S"))
        self.assertEqual(row["n"], 3)
        self.assertEqual(row["median_tokens"], 200)
        self.assertEqual(row["p90_tokens"], 300)
        self.assertEqual(row["estimate_tokens"], 120000)

    def test_tasks_without_actuals_ignored(self):
        world = world_of(task(5, "claude", "S", None),
                         task(6, "claude", "S", 200))
        rows = calibrate(world, ESTIMATES)
        self.assertEqual(rows[0]["n"], 1)
        self.assertEqual(rows[0]["median_tokens"], 200)

    def test_empty_world(self):
        self.assertEqual(calibrate(world_of(), ESTIMATES), [])

    def test_unknown_model_size_skipped(self):
        world = world_of(task(5, "nobody", "S", 100))
        self.assertEqual(calibrate(world, ESTIMATES), [])

    def test_suggestion_raise(self):
        world = world_of(task(5, "claude", "S", 200000))
        row = calibrate(world, ESTIMATES)[0]
        self.assertIn("raise", row["suggestion"])

    def test_suggestion_lower(self):
        world = world_of(task(5, "claude", "S", 1000))
        row = calibrate(world, ESTIMATES)[0]
        self.assertIn("lower", row["suggestion"])

    def test_suggestion_ok(self):
        world = world_of(task(5, "claude", "S", 50000),
                         task(6, "claude", "S", 150000))
        row = calibrate(world, ESTIMATES)[0]
        self.assertIn("ok", row["suggestion"])

    def test_report_names_everything(self):
        world = world_of(task(5, "claude", "S", 200000))
        text = format_calibration(calibrate(world, ESTIMATES))
        for needle in ("claude", "S", "200000", "120000", "raise"):
            self.assertIn(needle, text)

    def test_empty_report_says_so(self):
        self.assertIn("no actuals", format_calibration([]))


class EstimatesCommandTests(unittest.TestCase):
    def test_estimates_never_edits_file(self):
        import os
        import shutil
        import tempfile

        tmpd = tempfile.mkdtemp(prefix="opl-est-")
        self.addCleanup(shutil.rmtree, tmpd, True)
        path = os.path.join(tmpd, "estimates.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('[claude.S]\ninput_tokens = 1\noutput_tokens = 1\n')
        before = open(path, encoding="utf-8").read()
        world = world_of(task(5, "claude", "S", 100))
        estimates = {("claude", "S"): {"input_tokens": 1,
                                       "output_tokens": 1}}
        format_calibration(calibrate(world, estimates))
        self.assertEqual(open(path, encoding="utf-8").read(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
