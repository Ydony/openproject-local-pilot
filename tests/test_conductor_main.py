#!/usr/bin/env python3
"""opl-conductor wiring and least privilege (TH.9).

Watch mode never builds a runner; live mode ticks it once per cycle and
drains it on --once; the admin token is never read; a second conductor on
the same state dir is refused; shutdown stops running workers.
"""

import base64
import io
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import opl.conductor.__main__ as conductor_main
from opl.conductor.lock import AlreadyRunning, InstanceLock
from opl.conductor.spark import supervisor
from opl.settings import Settings
from tests.fakes.ccusage_fake import no_real_ccusage
from tests.fakes.http_fake import FakeServer
from tests.test_engine import MAIN_TOML

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

ENV = {
    "OPL_TOKEN_CLAUDE": "sentinel-claude",
    "OPL_TOKEN_CODEX": "sentinel-codex",
    "OPL_TOKEN_SPARK": "sentinel-spark",
    "OPL_TOKEN_CONDUCTOR": "sentinel-conductor",
    "OPL_GITHUB_TOKEN": "sentinel-gh",
}


class StubGH:
    def __init__(self, *args, **kwargs):
        pass

    def test_deploys(self, project, signal=None):
        return []

    def prod_deploys(self, project):
        return []


class FakeRunner:
    def __init__(self):
        self.ticks = []
        self.drained = []
        self.shutdowns = 0

    def tick(self, world):
        self.ticks.append(world)
        return ["task 3: started"]

    def drain(self, world):
        self.drained.append(world)
        return ["task 3: concluded"]

    def shutdown(self):
        self.shutdowns += 1
        return 0


def serve(server):
    base = server.base_url
    server.add("GET", "/api/v3/projects", body={"_embedded": {"elements": [
        {"id": 1, "identifier": "demo", "name": "Demo",
         "_links": {"self": {"href": base + "/api/v3/projects/1"}}}]}})
    server.add("GET", "/api/v3/work_packages", body={"_embedded": {"elements": [
        {"id": 1, "subject": "E", "updatedAt": "2026-09-20T12:00:00Z",
         "_links": {"type": {"href": "/api/v3/types/11"},
                    "status": {"href": "/api/v3/statuses/21"},
                    "parent": {"href": None}}}]}})
    server.add("GET", "/api/v3/types", body={"_embedded": {"elements": [
        {"id": 11, "name": "Epic"}, {"id": 12, "name": "Feature"},
        {"id": 13, "name": "Task"}]}})
    server.add("GET", "/api/v3/statuses", body={"_embedded": {"elements": [
        {"id": 21, "name": "Open"}]}})
    # The conductor is no admin: listing users is refused (TH.7 fallback).
    server.add("GET", "/api/v3/users", status=403,
               body={"_type": "Error", "message": "not allowed"})
    server.add("GET", "/api/v3/work_packages/schemas/1-11", body={"_type": "Schema"})
    server.add("GET", "/api/v3/work_packages/1/activities",
               body={"_embedded": {"elements": []}})
    server.add("GET", "/api/v3/relations", body={"_embedded": {"elements": []}})
    server.add("GET", "/api/v3/memberships", body={"_embedded": {"elements": []}})


class MainTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        serve(self.server)
        self.tmpd = tempfile.mkdtemp(prefix="opl-main9-")
        self.state_dir = os.path.join(self.tmpd, "state")

    def write_config(self, live):
        text = MAIN_TOML % (self.server.base_url,
                            self.state_dir.replace("\\", "/"))
        if live:
            text = text.replace("live = false", "live = true")
        with open(os.path.join(self.tmpd, "opl.toml"), "w", encoding="utf-8") as fh:
            fh.write(text)

    def run_main(self, argv, live=False, runner=None):
        self.write_config(live)
        env = dict(ENV, OPL_CONFIG_DIR=self.tmpd)
        make = mock.Mock(return_value=runner)
        out, err = io.StringIO(), io.StringIO()
        # No OPL_TOKEN_ADMIN at all: the conductor must not need it.
        with mock.patch.dict(os.environ, env), no_real_ccusage() as ccusage:
            os.environ.pop("OPL_TOKEN_ADMIN", None)
            with mock.patch.object(conductor_main, "GitHub", StubGH), \
                    mock.patch.object(conductor_main, "_make_runner", make), \
                    mock.patch.object(Settings, "token", autospec=True,
                                      side_effect=Settings.token) as token, \
                    redirect_stdout(out), redirect_stderr(err):
                rc = conductor_main.main(argv)
        self.ccusage = ccusage
        return rc, make, token, out.getvalue(), err.getvalue()

    def test_watch_mode_never_builds_a_runner(self):
        for argv, live in ((["--once"], True), (["--once", "--live"], False)):
            rc, make, _token, _out, err = self.run_main(argv, live=live)
            self.assertEqual(rc, 0, err)
            make.assert_not_called()

    def test_live_mode_ticks_then_drains_then_shuts_down(self):
        runner = FakeRunner()
        rc, make, _token, out, err = self.run_main(["--once", "--live"],
                                                   live=True, runner=runner)
        self.assertEqual(rc, 0, err)
        make.assert_called_once()
        self.assertEqual(len(runner.ticks), 1)
        self.assertEqual(runner.drained, runner.ticks)
        self.assertEqual(runner.shutdowns, 1)
        self.assertIn("spark: task 3: started", out)
        self.assertIn("spark: task 3: concluded", out)

    def test_admin_token_is_never_read(self):
        rc, _make, token, _out, err = self.run_main(["--once", "--live"],
                                                    live=True, runner=FakeRunner())
        self.assertEqual(rc, 0, err)
        asked = [call.args[1] for call in token.call_args_list]
        self.assertIn("conductor", asked)
        self.assertNotIn("admin", asked)
        conductor = "Basic " + base64.b64encode(
            b"apikey:sentinel-conductor").decode("ascii")
        auths = {r["headers"].get("Authorization") for r in self.server.requests}
        self.assertEqual(auths, {conductor})

    def test_costs_run_every_tool_without_tokens(self):
        # The cost step asks ccusage (faked here, never the real npx) for
        # all three tools, with no token variable in its environment.
        rc, *_ = self.run_main(["--once"])
        self.assertEqual(rc, 0)
        self.assertEqual([c["tool"] for c in self.ccusage],
                         ["claude", "codex", "opencode"])
        for call in self.ccusage:
            self.assertFalse([n for n in call["env"] if n.startswith("OPL_TOKEN_")
                              or n == "OPL_GITHUB_TOKEN"])
        # No Spark run yet, so only the owner's OpenCode store is listed.
        self.assertTrue(call["env"]["OPENCODE_DATA_DIR"])

    def test_second_instance_is_refused(self):
        with InstanceLock(self.state_dir):
            rc, make, _token, _out, err = self.run_main(["--once"])
        self.assertEqual(rc, 1)
        self.assertIn("another opl-conductor is running", err)
        self.assertEqual(self.server.requests, [])

    def test_lock_is_released_after_a_run(self):
        rc, *_ = self.run_main(["--once"])
        self.assertEqual(rc, 0)
        with InstanceLock(self.state_dir):
            pass


class RunnerGateTests(unittest.TestCase):
    """TH.23 (Codex note on the reopened gate): a new runner reopening the
    process-wide gate must not release an older runner's threads."""

    def test_old_runner_thread_stays_stopped_after_gate_reopens(self):
        supervisor.reset_stop_gate()
        self.addCleanup(supervisor.reset_stop_gate)
        work = tempfile.mkdtemp(prefix="opl-gate-")
        old_runner_stop = threading.Event()
        preparing = threading.Event()
        go = threading.Event()
        results = []

        def old_run_thread():
            supervisor.bind_stop(old_runner_stop)
            preparing.set()
            go.wait(30)             # still fetching/creating a worktree
            results.append(supervisor.run_worker(
                [PY, os.path.join(HERE, "fakes", "worker_hang.py")],
                work, 3600, 3600, os.path.join(work, "logs", "old.log")))

        thread = threading.Thread(target=old_run_thread)
        thread.start()
        preparing.wait(30)
        old_runner_stop.set()           # the old runner shut down ...
        supervisor.terminate_all()
        supervisor.reset_stop_gate()    # ... and a new runner reopened the gate
        go.set()
        thread.join(60)
        self.assertEqual(results[0].outcome, "failed")
        self.assertIn("conductor shutting down", "\n".join(results[0].last_lines))
        self.assertEqual(supervisor.live_workers(), 0)

    def test_new_runner_threads_can_still_start(self):
        supervisor.reset_stop_gate()
        self.addCleanup(supervisor.reset_stop_gate)
        work = tempfile.mkdtemp(prefix="opl-gate-")
        results = []

        def new_run_thread():
            supervisor.bind_stop(threading.Event())
            results.append(supervisor.run_worker(
                [PY, "-c", "print('ok')"], work, 60, 60,
                os.path.join(work, "logs", "new.log")))

        thread = threading.Thread(target=new_run_thread)
        thread.start()
        thread.join(60)
        self.assertEqual(results[0].outcome, "success")


class InstanceLockTests(unittest.TestCase):
    def test_one_holder_at_a_time(self):
        state = tempfile.mkdtemp(prefix="opl-lock-")
        first = InstanceLock(state).acquire()
        try:
            with self.assertRaises(AlreadyRunning):
                InstanceLock(state).acquire()
        finally:
            first.release()
        InstanceLock(state).acquire().release()


class ShutdownTests(unittest.TestCase):
    def test_terminate_all_stops_a_hanging_worker(self):
        # terminate_all() closes the process-wide stop gate (TH.17); reopen
        # it so later tests in this process can still start workers.
        supervisor.reset_stop_gate()
        self.addCleanup(supervisor.reset_stop_gate)
        work = tempfile.mkdtemp(prefix="opl-hang-")
        log = os.path.join(work, "logs", "run.log")
        results = []
        thread = threading.Thread(target=lambda: results.append(
            supervisor.run_worker([PY, os.path.join(HERE, "fakes", "worker_hang.py")],
                                  work, 3600, 3600, log)))
        with mock.patch.dict(os.environ, {"OPL_TIME_SCALE": "1"}):
            thread.start()
            deadline = time.monotonic() + 30
            while supervisor.live_workers() == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(supervisor.live_workers(), 1)
            self.assertEqual(supervisor.terminate_all(), 1)
            thread.join(60)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0].outcome, "failed")
        self.assertEqual(supervisor.live_workers(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
