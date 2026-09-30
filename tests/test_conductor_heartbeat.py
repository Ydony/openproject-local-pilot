#!/usr/bin/env python3
"""Conductor loop heartbeat (issue #60). Synthetic only, stdlib only.

The loop writes <state_dir>/conductor.heartbeat every cycle so status
can report when the loop last ran instead of trusting the lock, and the
watchdog restarts the loop when the heartbeat goes stale.
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import opl.conductor.__main__ as conductor_main
from opl.conductor.heartbeat import (
    heartbeat_age_seconds,
    heartbeat_line,
    heartbeat_path,
    is_stale,
    needs_restart,
    read_heartbeat,
    stale_after_seconds,
    write_heartbeat,
)
from opl.conductor.lock import InstanceLock
from opl.openproject import ApiError

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def stub_settings(state_dir, interval=60):
    return SimpleNamespace(conductor=SimpleNamespace(
        state_dir=state_dir, interval_seconds=interval))


class HeartbeatFileTests(unittest.TestCase):
    def setUp(self):
        self.tmpd = tempfile.mkdtemp(prefix="opl-hb-")

    def test_write_then_read_roundtrip(self):
        stamp = write_heartbeat(self.tmpd, NOW)
        self.assertIn("2026-09-30", stamp)
        self.assertEqual(read_heartbeat(self.tmpd), NOW)

    def test_missing_file_reads_none(self):
        self.assertIsNone(read_heartbeat(self.tmpd))
        self.assertIsNone(heartbeat_age_seconds(self.tmpd, NOW))
        self.assertIsNone(is_stale(self.tmpd, 60, NOW))
        self.assertIn("none yet", heartbeat_line(self.tmpd, 60, NOW))

    def test_garbage_content_falls_back_to_mtime(self):
        path = heartbeat_path(self.tmpd)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("not a timestamp\n")
        # mtime is just now: an age, not silence.
        self.assertIsNotNone(read_heartbeat(self.tmpd))
        age = heartbeat_age_seconds(self.tmpd, NOW)
        self.assertIsNotNone(age)
        self.assertGreaterEqual(age, 0.0)

    def test_naive_stamp_is_read_as_utc(self):
        path = heartbeat_path(self.tmpd)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("2026-09-30T12:00:00\n")
        self.assertEqual(read_heartbeat(self.tmpd), NOW)

    def test_backward_clock_clamps_to_zero(self):
        write_heartbeat(self.tmpd, NOW + timedelta(hours=1))
        self.assertEqual(heartbeat_age_seconds(self.tmpd, NOW), 0.0)
        self.assertFalse(is_stale(self.tmpd, 60, NOW))

    def test_write_never_raises(self):
        # A file where the directory should be: the loop must survive it.
        blocker = os.path.join(self.tmpd, "blocker")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("x")
        write_heartbeat(os.path.join(blocker, "sub"), NOW)

    def test_stale_threshold_is_three_intervals(self):
        self.assertEqual(stale_after_seconds(60), 180)
        write_heartbeat(self.tmpd, NOW - timedelta(seconds=179))
        self.assertFalse(is_stale(self.tmpd, 60, NOW))
        write_heartbeat(self.tmpd, NOW - timedelta(seconds=181))
        self.assertTrue(is_stale(self.tmpd, 60, NOW))

    def test_status_line_names_loop_age_and_flags_stale(self):
        write_heartbeat(self.tmpd, NOW - timedelta(seconds=30))
        fresh = heartbeat_line(self.tmpd, 60, NOW)
        self.assertIn("loop last ran", fresh)
        self.assertNotIn("STALE", fresh)
        write_heartbeat(self.tmpd, NOW - timedelta(hours=2))
        stale = heartbeat_line(self.tmpd, 60, NOW)
        self.assertIn("loop last ran", stale)
        self.assertIn("STALE", stale)


class WatchdogVerdictTests(unittest.TestCase):
    def setUp(self):
        self.tmpd = tempfile.mkdtemp(prefix="opl-hbwd-")
        self.lock = os.path.join(self.tmpd, "conductor.lock")

    def touch_lock(self, when):
        with open(self.lock, "w", encoding="utf-8") as fh:
            fh.write("1234\n")
        stamp = when.timestamp()
        os.utime(self.lock, (stamp, stamp))

    def test_fresh_heartbeat_needs_no_restart(self):
        write_heartbeat(self.tmpd, NOW - timedelta(seconds=30))
        restart, reason = needs_restart(self.tmpd, 60, NOW)
        self.assertFalse(restart)
        self.assertIn("loop last ran", reason)

    def test_stale_heartbeat_needs_restart(self):
        write_heartbeat(self.tmpd, NOW - timedelta(hours=2))
        restart, reason = needs_restart(self.tmpd, 60, NOW)
        self.assertTrue(restart)
        self.assertIn("STALE", reason)

    def test_missing_heartbeat_with_young_lock_is_startup(self):
        self.touch_lock(NOW - timedelta(seconds=30))
        restart, reason = needs_restart(self.tmpd, 60, NOW)
        self.assertFalse(restart)
        self.assertIn("starting up", reason)

    def test_missing_heartbeat_with_old_lock_needs_restart(self):
        self.touch_lock(NOW - timedelta(hours=2))
        restart, reason = needs_restart(self.tmpd, 60, NOW)
        self.assertTrue(restart)


class LoopWritesHeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.tmpd = tempfile.mkdtemp(prefix="opl-hbloop-")
        self.settings = stub_settings(self.tmpd)
        self.args = SimpleNamespace(once=True)

    def run_loop(self, cycle):
        with mock.patch.object(conductor_main, "_cycle", cycle), \
                mock.patch.object(conductor_main, "redact",
                                  lambda text, _s: text):
            return conductor_main._loop(self.args, None, None, self.settings,
                                        None, False, None)

    def test_successful_cycle_writes_heartbeat(self):
        rc = self.run_loop(mock.Mock(return_value=object()))
        self.assertEqual(rc, 0)
        self.assertIsNotNone(read_heartbeat(self.tmpd))

    def test_failed_read_still_writes_heartbeat(self):
        # The loop is alive even when the APIs are down: the heartbeat
        # must move so the watchdog does not restart a healthy loop.
        def boom(*args, **kwargs):
            raise ApiError(500, "/api/v3/projects", "synthetic outage")

        rc = self.run_loop(boom)
        self.assertEqual(rc, 0)
        self.assertIsNotNone(read_heartbeat(self.tmpd))


if __name__ == "__main__":
    unittest.main(verbosity=2)
