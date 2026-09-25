#!/usr/bin/env python3
"""Tests: shutdown never misses a worker, even one still starting (TH.17)."""

import os
import sys
import tempfile
import threading
import time
import unittest

from opl.conductor.spark.supervisor import (
    live_workers,
    reset_stop_gate,
    run_worker,
    terminate_all,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKES = os.path.join(REPO, "tests", "fakes")


def fake(name):
    return [sys.executable, os.path.join(FAKES, name)]


class StopGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opl-shutdown-")
        self.workdir = os.path.join(self.tmp, "work")
        os.mkdir(self.workdir)
        self.log = os.path.join(self.tmp, "run.log")
        self.addCleanup(reset_stop_gate)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_shutdown_during_preparation_starts_no_worker(self):
        # A run thread blocked in preparation (fetch, worktree) calls
        # run_worker after the gate is set: nothing spawns.
        marker = os.path.join(self.tmp, "spawned")
        barrier = threading.Barrier(2)
        outcomes = []

        def preparing_run():
            barrier.wait(timeout=30)  # preparation happens here
            outcomes.append(run_worker(
                [sys.executable, "-c",
                 "open(%r, 'w').write('x')" % marker],
                self.workdir, 300, 300, self.log))

        thread = threading.Thread(target=preparing_run, daemon=True)
        thread.start()
        self.assertEqual(terminate_all(), 0)
        barrier.wait(timeout=30)  # release preparation after the gate
        thread.join(timeout=60)
        self.assertFalse(thread.is_alive())
        self.assertFalse(os.path.exists(marker), "worker spawned anyway")
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].outcome, "failed")
        self.assertIn("conductor shutting down",
                      "\n".join(outcomes[0].last_lines))

    def test_shutdown_after_spawn_kills_it(self):
        outcomes = []

        def hanging_run():
            outcomes.append(run_worker(fake("worker_hang.py"), self.workdir,
                                       3600, 3600, self.log))

        thread = threading.Thread(target=hanging_run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 30
        while live_workers() != 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(live_workers(), 1)
        self.assertGreaterEqual(terminate_all(), 1)
        thread.join(timeout=60)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].outcome, "failed")
        self.assertEqual(live_workers(), 0)

    def test_gate_resets_for_the_next_run(self):
        terminate_all()
        reset_stop_gate()
        result = run_worker(fake("worker_ok.py"), self.workdir,
                            300, 300, self.log)
        self.assertEqual(result.outcome, "success")


class RunnerShutdownTests(unittest.TestCase):
    def _runner(self):
        from opl.conductor.spark.runner import SparkRunner

        runner = SparkRunner.__new__(SparkRunner)
        runner.settings = None
        runner.model = None
        runner.op = None
        runner.gh = None
        runner.slots = None
        runner._active = {}
        runner._lock = threading.Lock()
        runner._stop = threading.Event()   # the runner's own gate (TH.23)
        return runner

    def test_shutdown_waits_for_records_then_returns(self):
        runner = self._runner()
        done = threading.Event()
        runner._active[("build", 5)] = {"done": done}

        def finish():
            time.sleep(0.3)
            done.set()

        thread = threading.Thread(target=finish, daemon=True)
        thread.start()
        started = time.monotonic()
        self.assertEqual(runner.shutdown(timeout=30), 0)
        self.assertLess(time.monotonic() - started, 30)
        thread.join(timeout=10)

    def test_shutdown_stops_at_a_bounded_deadline(self):
        runner = self._runner()
        runner._active[("build", 5)] = {"done": threading.Event()}
        started = time.monotonic()
        runner.shutdown(timeout=2)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 1.5)
        self.assertLess(elapsed, 30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
