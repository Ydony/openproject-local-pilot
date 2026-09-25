#!/usr/bin/env python3
"""Tests for the Spark supervisor: outcomes, slots, logs, scaling."""

import os
import sys
import tempfile
import unittest
from unittest import mock

from opl.conductor.spark.supervisor import (
    RunResult,
    Slots,
    run_worker,
    worker_environment,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKES = os.path.join(REPO, "tests", "fakes")

# OPL_TIME_SCALE shrinks every limit: real seconds per test in brackets.
SCALE = {"OPL_TIME_SCALE": "0.02"}


def fake(name):
    return [sys.executable, os.path.join(FAKES, name)]


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opl-sup-")
        self.workdir = os.path.join(self.tmp, "work")
        os.mkdir(self.workdir)
        self.log = os.path.join(self.tmp, "run.log")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_fake(self, name, limit_s=300, stall_s=600):
        with mock.patch.dict(os.environ, SCALE):
            return run_worker(fake(name), self.workdir, limit_s, stall_s,
                              self.log)

    def test_ok(self):
        result = self.run_fake("worker_ok.py")
        self.assertIsInstance(result, RunResult)
        self.assertEqual(result.outcome, "success")
        self.assertGreaterEqual(result.duration_s, 0)
        self.assertIn("OPL-RESULT: DONE all green", result.last_lines)
        self.assertAlmostEqual(result.cost_usd, 0.042)
        with open(self.log, encoding="utf-8") as fh:
            self.assertIn("OPL-RESULT: DONE", fh.read())

    def test_fail(self):
        result = self.run_fake("worker_fail.py")
        self.assertEqual(result.outcome, "failed")
        self.assertIn("something broke", result.last_lines)

    def test_hang_times_out(self):
        result = self.run_fake("worker_hang.py", limit_s=300, stall_s=3600)
        self.assertEqual(result.outcome, "timeout")
        # Killed, not still running: the log stops growing.
        with open(self.log, encoding="utf-8") as fh:
            size = len(fh.read())
        import time

        time.sleep(1.0)
        with open(self.log, encoding="utf-8") as fh:
            self.assertEqual(len(fh.read()), size)

    def test_silent_stalls(self):
        result = self.run_fake("worker_silent.py", limit_s=3600, stall_s=300)
        self.assertEqual(result.outcome, "stalled")

    def test_output_spam_without_files_is_stall(self):
        # TH.10: log growth alone is not progress. worker_hang prints
        # forever but never touches the workdir: short stall must win
        # over the long timeout.
        result = self.run_fake("worker_hang.py", limit_s=3600, stall_s=30)
        self.assertEqual(result.outcome, "stalled")

    def _probe(self, allow=()):
        """Run the env probe with a canary secret and a canary real HOME."""
        real_home = os.path.join(self.tmp, "real-home")
        os.mkdir(real_home)
        with open(os.path.join(real_home, "opl-canary.txt"), "w") as fh:
            fh.write("private")
        worker_home = os.path.join(self.tmp, "worker-home")
        leaked = {"OPL_CANARY_SECRET": "canary-value", "OPL_WORKER_KEY": "k",
                  "HOME": real_home, "USERPROFILE": real_home}
        with mock.patch.dict(os.environ, dict(SCALE, **leaked)):
            env = worker_environment(allow, worker_home)
            result = run_worker(fake("worker_env_probe.py"), self.workdir,
                                300, 600, self.log, env=env)
        self.assertEqual(result.outcome, "success")
        lines = dict(line.split(":", 1) for line in result.last_lines
                     if ":" in line and line.split(":", 1)[0].isupper())
        return lines, worker_home

    def test_worker_gets_no_conductor_secrets(self):
        lines, _ = self._probe()
        keys = lines["ENVKEYS"].split(",")
        self.assertNotIn("OPL_CANARY_SECRET", keys)
        self.assertNotIn("OPL_WORKER_KEY", keys)
        self.assertEqual(lines["APIKEY"], "unset")

    def test_worker_home_is_fresh_not_the_owners(self):
        lines, worker_home = self._probe()
        self.assertEqual(lines["CANARY"], "absent")
        self.assertTrue(os.path.normcase(lines["HOME"]).startswith(
            os.path.normcase(worker_home)))

    def test_allowlisted_worker_key_is_passed(self):
        lines, _ = self._probe(allow=("OPL_WORKER_KEY",))
        self.assertEqual(lines["APIKEY"], "set")
        self.assertNotIn("OPL_CANARY_SECRET", lines["ENVKEYS"].split(","))

    def test_default_env_is_isolated_too(self):
        # A caller that forgets env= still gets an isolated environment.
        with mock.patch.dict(os.environ, dict(SCALE, OPL_CANARY_SECRET="x")):
            result = run_worker(fake("worker_env_probe.py"), self.workdir,
                                300, 600, self.log)
        keys = [l for l in result.last_lines if l.startswith("ENVKEYS:")][0]
        self.assertNotIn("OPL_CANARY_SECRET", keys)

    def test_slots(self):
        slots = Slots(2)
        self.assertTrue(slots.acquire("a"))
        self.assertTrue(slots.acquire("b"))
        self.assertFalse(slots.acquire("c"))
        self.assertEqual(len(slots), 2)
        slots.release("a")
        self.assertTrue(slots.acquire("c"))
        slots.release("missing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
