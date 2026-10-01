#!/usr/bin/env python3
"""The runner provisions declared runtimes and passes them to the adapter
(issue #45). Synthetic: fake publisher, fake workers, no network."""

import base64
import dataclasses
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from opl import runtimes
from tests.test_runtimes import Publisher
from tests.test_spark_runner import (FAKES, RunnerHarness, make_world,
                                     ready_task, settle)


class RuntimeWiringTests(RunnerHarness):
    def setUp(self):
        super().setUp()
        self.rt_root = tempfile.mkdtemp(prefix="opl-rtw-")
        self.addCleanup(shutil.rmtree, self.rt_root, True)
        patcher = mock.patch.object(runtimes, "_arch", return_value="x64")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _with_runtime(self, runner, runtime=("node@22",), setup="npm ci",
                      root=None):
        projects = tuple(
            dataclasses.replace(p, runtime=runtime, setup=setup)
            if p.key == "demo" else p for p in runner.settings.projects)
        conductor = dataclasses.replace(
            runner.settings.conductor, runtimes_dir=root or self.rt_root)
        runner.settings = dataclasses.replace(
            runner.settings, projects=projects, conductor=conductor)
        self.publisher = Publisher(["22.17.0"])
        runner._runtime_fetch = self.publisher
        return runner

    def test_args_carry_the_installed_version_and_the_setup(self):
        runner = self._with_runtime(self._runner("worker_commit.py"))
        args = runner._runtime_args("demo", with_setup=True)
        self.assertEqual(args[:2], ["--runtime", "node@22.17.0"])
        self.assertEqual(args[2], "--setup-b64")
        self.assertEqual(base64.b64decode(args[3]).decode(), "npm ci")
        self.assertEqual(runner._runtime_args("demo", with_setup=False),
                         ["--runtime", "node@22.17.0"])

    def test_project_without_declarations_gets_no_extra_arguments(self):
        runner = self._runner("worker_commit.py")
        self.assertEqual(runner._runtime_args("demo", with_setup=True), [])
        self.assertTrue(runner._runtime_ready("demo"))

    def test_build_command_passes_runtime_and_setup_to_the_adapter(self):
        runner = self._with_runtime(self._runner("worker_argv.py"))
        world = make_world(ready_task(5))
        runner.tick(world)
        settle(runner)
        marker = os.path.join(self.tmp, "state", "worktrees", "opl", "argv.txt")
        with open(marker, encoding="utf-8") as fh:
            argv = fh.read()
        self.assertIn("--runtime node@22.17.0", argv)
        self.assertIn("--setup-b64 " + base64.b64encode(b"npm ci").decode(),
                      argv)

    def test_unprovisionable_project_starts_no_runs_and_says_why(self):
        runner = self._with_runtime(
            self._runner("worker_commit.py"),
            root=os.path.join(self.rt_root, "absent"))
        with self.assertLogs("opl.conductor.spark",
                             level="WARNING") as logs:
            runner.tick(make_world(ready_task(5)))
            settle(runner)
        self.assertTrue(any("not ready" in line and "one-time owner step"
                            in line for line in logs.output), logs.output)
        self.assertIn("demo", runner.runtime_problems())
        logdir = os.path.join(self.tmp, "state", "logs")
        started = [f for _r, _d, fs in os.walk(logdir) for f in fs] \
            if os.path.isdir(logdir) else []
        self.assertEqual(started, [])
        self.assertEqual(self.patches, [])   # task untouched, not Blocked

    def test_failed_install_is_not_retried_every_tick(self):
        runner = self._with_runtime(self._runner("worker_commit.py"))
        runner._runtime_fetch = Publisher([], corrupt=[])
        calls = []
        orig = runtimes.ensure

        def counting(*a, **k):
            calls.append(1)
            return orig(*a, **k)
        with mock.patch.object(runtimes, "ensure", counting):
            for _ in range(3):
                self.assertFalse(runner._runtime_ready("demo"))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
