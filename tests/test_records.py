#!/usr/bin/env python3
"""Tests for run records: JSONL append/read, summary, report."""

import json
import os
import tempfile
import unittest

from opl.conductor.spark.records import (
    format_report,
    read_runs,
    record_run,
    summarize,
)


def entry(task=5, kind="build", size="S", duration=60.0, outcome="success",
          cost=0.01):
    return {"task": task, "kind": kind, "size": size,
            "started": "2026-09-24T10:00:00+00:00",
            "ended": "2026-09-24T10:01:00+00:00",
            "duration_s": duration, "outcome": outcome, "cost_usd": cost}


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opl-records-")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_roundtrip_appends_json_lines(self):
        record_run(self.tmp, **entry())
        record_run(self.tmp, **entry(task=6, outcome="timeout", duration=90.0))
        with open(os.path.join(self.tmp, "runs.jsonl"), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["task"], 5)
        self.assertEqual(json.loads(lines[1])["outcome"], "timeout")

    def test_required_keys_present(self):
        record_run(self.tmp, **entry())
        row = read_runs(self.tmp)[0]
        for key in ("task", "kind", "size", "started", "ended",
                    "duration_s", "outcome", "cost_usd", "worktree",
                    "commit"):
            self.assertIn(key, row)

    def test_manifest_keys_recorded(self):
        record_run(self.tmp, **entry(), worktree="/x/wt/opl/task-T5-1",
                   commit="abc123")
        row = read_runs(self.tmp)[0]
        self.assertEqual(row["worktree"], "/x/wt/opl/task-T5-1")
        self.assertEqual(row["commit"], "abc123")

    def test_missing_file_reads_empty(self):
        self.assertEqual(read_runs(self.tmp), [])

    def test_malformed_lines_skipped(self):
        path = os.path.join(self.tmp, "runs.jsonl")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write('{"task": 5}\nnot json\n{"task": 6}\n')
        rows = read_runs(self.tmp)
        self.assertEqual([r["task"] for r in rows], [5, 6])


class SummaryTests(unittest.TestCase):
    def test_median_max_and_timeouts(self):
        runs = [
            entry(kind="build", size="S", duration=10.0),
            entry(kind="build", size="S", duration=20.0),
            entry(kind="build", size="S", duration=30.0),
            entry(kind="build", size="S", duration=5.0, outcome="timeout"),
            entry(kind="review", size="-", duration=7.0, outcome="review-pass"),
        ]
        summary = summarize(runs)
        build = summary["groups"][("build", "S")]
        self.assertEqual(build["count"], 4)
        self.assertEqual(build["median_s"], 15.0)
        self.assertEqual(build["max_s"], 30.0)
        self.assertEqual(summary["timeouts"], 1)

    def test_empty_summary(self):
        summary = summarize([])
        self.assertEqual(summary["groups"], {})
        self.assertEqual(summary["timeouts"], 0)

    def test_report_names_groups_and_timeouts(self):
        summary = summarize([entry(), entry(outcome="timeout")])
        text = format_report(summary)
        self.assertIn("build", text)
        self.assertIn("timeout", text.lower())

    def test_empty_report_says_so(self):
        self.assertIn("no runs", format_report(summarize([])).lower())


RUNS_TOML = """
[openproject]
url = "http://127.0.0.1:8080"
admin_token_env = "OPL_TOKEN_ADMIN"
owner_login = "admin"

[tokens]
spark = "OPL_TOKEN_SPARK"
claude = "OPL_TOKEN_CLAUDE"
codex = "OPL_TOKEN_CODEX"
conductor = "OPL_TOKEN_CONDUCTOR"

[github]
token_env = "OPL_GITHUB_TOKEN"

[users]
email_domain = "example.invalid"

[conductor]
live = false
interval_seconds = 60
state_dir = "%s"

[runner]
command = ["example-worker"]
max_parallel = 2

[runner.limits_minutes]
S = 20
M = 45
L = 90
review = 15
test = 20
stall = 10

[[project]]
key = "demo"
name = "Demo"
repo = "example-owner/demo"
visibility = "Public"
has_test_env = false
prod_signal = { kind = "environment", name = "production" }
"""


class RunsCommandTests(unittest.TestCase):
    def test_runs_prints_report(self):
        import io
        import shutil
        import tempfile
        from contextlib import redirect_stdout
        from unittest import mock

        import opl.conductor.__main__ as conductor_main

        tmpd = tempfile.mkdtemp(prefix="opl-runs-")
        self.addCleanup(shutil.rmtree, tmpd, True)
        state_dir = os.path.join(tmpd, "state")
        with open(os.path.join(tmpd, "opl.toml"), "w", encoding="utf-8") as fh:
            fh.write(RUNS_TOML % state_dir.replace("\\", "/"))
        record_run(state_dir, **entry())
        record_run(state_dir, **entry(outcome="timeout", duration=90.0))
        env = {"OPL_CONFIG_DIR": tmpd,
               "OPL_TOKEN_ADMIN": "sentinel-admin",
               "OPL_TOKEN_SPARK": "sentinel-spark",
               "OPL_TOKEN_CLAUDE": "sentinel-claude",
               "OPL_TOKEN_CODEX": "sentinel-codex",
               "OPL_TOKEN_CONDUCTOR": "sentinel-conductor",
               "OPL_GITHUB_TOKEN": "sentinel-gh"}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env):
            with redirect_stdout(out):
                rc = conductor_main.main(["runs"])
        self.assertEqual(rc, 0)
        self.assertIn("build", out.getvalue())
        self.assertIn("1 of 2", out.getvalue())

    def test_runs_empty_state(self):
        import io
        import shutil
        import tempfile
        from contextlib import redirect_stdout
        from unittest import mock

        import opl.conductor.__main__ as conductor_main

        tmpd = tempfile.mkdtemp(prefix="opl-runs-")
        self.addCleanup(shutil.rmtree, tmpd, True)
        state_dir = os.path.join(tmpd, "state")
        with open(os.path.join(tmpd, "opl.toml"), "w", encoding="utf-8") as fh:
            fh.write(RUNS_TOML % state_dir.replace("\\", "/"))
        env = {"OPL_CONFIG_DIR": tmpd,
               "OPL_TOKEN_ADMIN": "sentinel-admin",
               "OPL_TOKEN_SPARK": "sentinel-spark",
               "OPL_TOKEN_CLAUDE": "sentinel-claude",
               "OPL_TOKEN_CODEX": "sentinel-codex",
               "OPL_TOKEN_CONDUCTOR": "sentinel-conductor",
               "OPL_GITHUB_TOKEN": "sentinel-gh"}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env):
            with redirect_stdout(out):
                rc = conductor_main.main(["runs"])
        self.assertEqual(rc, 0)
        self.assertIn("no runs", out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
