#!/usr/bin/env python3
"""Readiness gate (#44): not-ready projects get no work and one owner action."""

import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

from opl import projectcheck as pc
from opl.conductor.readiness import CACHE_NAME, RECHECK_SECONDS, ReadinessGate
from opl.conductor.rules.screens import screens
from opl.conductor.rules.stages import stages
from opl.conductor.state import Item, Project, World
from opl.settings import Project as SettingsProject

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Checker:
    """check(project) -> results; the answer can be changed between calls."""

    def __init__(self, results):
        self.results = results
        self.calls = 0

    def __call__(self, project):
        self.calls += 1
        return list(self.results)


FAIL = [pc.Result(pc.FAIL, "github: a PR check exists", "none",
                  "add a CI workflow"),
        pc.Result(pc.FAIL, "sandbox: launcher", "gone", "reinstall")]
OK = [pc.Result(pc.PASS, "all")]
EXPIRY = [pc.Result(pc.WARN, "github: token expiry", "in 5 days", "renew it")]


def make_settings(state_dir):
    projects = [SettingsProject("demo", "Demo", "o/r", "Public", False)]
    return SimpleNamespace(conductor=SimpleNamespace(state_dir=state_dir),
                           projects=projects)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opl-gate-")
        self.clock = Clock()

    def gate(self, checker):
        return ReadinessGate(make_settings(self.tmp), checker,
                             background=False, clock=self.clock)

    def test_failing_project_reports_first_failure_and_fix(self):
        issues = self.gate(Checker(FAIL)).issues({"demo"})
        level, text = issues["demo"]
        self.assertEqual(level, "fail")
        self.assertIn("Project not ready: github: a PR check exists - add a "
                      "CI workflow", text)
        self.assertIn("+1 more", text)
        self.assertIn("bin/opl-project-check demo", text)

    def test_ready_project_has_no_issue(self):
        self.assertEqual(self.gate(Checker(OK)).issues({"demo"}), {})

    def test_token_expiry_is_a_warning_not_a_stop(self):
        issues = self.gate(Checker(EXPIRY)).issues({"demo"})
        self.assertEqual(issues["demo"][0], "warn")
        self.assertIn("renew it", issues["demo"][1])
        world = World(now=NOW, projects={}, items={}, pull_requests={},
                      not_ready=issues)
        self.assertFalse(world.blocks_work("demo"))

    def test_only_the_expiry_warning_surfaces(self):
        other = [pc.Result(pc.WARN, "github: branch protection readable", "x")]
        self.assertEqual(self.gate(Checker(other)).issues({"demo"}), {})

    def test_result_is_cached_for_a_day_and_rechecked_after(self):
        checker = Checker(FAIL)
        gate = self.gate(checker)
        gate.issues({"demo"})
        gate.issues({"demo"})
        self.assertEqual(checker.calls, 1)
        self.clock.now += RECHECK_SECONDS - 10
        gate.issues({"demo"})
        self.assertEqual(checker.calls, 1)
        self.clock.now += 20
        gate.issues({"demo"})
        self.assertEqual(checker.calls, 2)

    def test_recovery_clears_the_issue(self):
        checker = Checker(FAIL)
        gate = self.gate(checker)
        self.assertIn("demo", gate.issues({"demo"}))
        checker.results = OK
        self.clock.now += RECHECK_SECONDS + 1
        self.assertEqual(gate.issues({"demo"}), {})

    def test_restart_rechecks_but_serves_the_cache_meanwhile(self):
        self.gate(Checker(FAIL)).issues({"demo"})
        with open(os.path.join(self.tmp, CACHE_NAME), encoding="utf-8") as fh:
            self.assertIn("demo", json.load(fh))
        checker = Checker(OK)
        # A new process: the cache answers at once, the re-check replaces it.
        gate = ReadinessGate(make_settings(self.tmp), checker,
                             background=True, clock=self.clock)
        gate.background = False  # run the re-check inline for the assertion
        first = gate.issues({"demo"})
        self.assertEqual(checker.calls, 1)
        self.assertEqual(first, {})

    def test_failure_detail_is_cached_and_old_entries_still_read(self):
        self.gate(Checker(FAIL)).issues({"demo"})
        with open(os.path.join(self.tmp, CACHE_NAME), encoding="utf-8") as fh:
            entry = json.load(fh)["demo"]
        self.assertEqual(entry["fail"][0][2], "none")
        from opl.conductor.readiness import describe

        self.assertEqual(describe("demo", {"fail": [["a", "fix it"]]})[0],
                         "fail")

    def test_first_ever_check_is_pending_while_it_runs(self):
        release = []

        def slow(project):
            release.append(1)
            return OK

        gate = ReadinessGate(make_settings(self.tmp), slow, background=True,
                             clock=self.clock)
        gate._running.add("demo")  # a check is in flight and has no answer
        gate._started.add("demo")
        self.assertEqual(gate.issues({"demo"}), {"demo": ("pending", "")})
        self.assertEqual(release, [])

    def test_a_crashing_check_fails_closed_with_a_pointer(self):
        def boom(project):
            raise RuntimeError("kaboom")

        issues = self.gate(boom).issues({"demo"})
        self.assertEqual(issues["demo"][0], "fail")
        self.assertIn("kaboom", issues["demo"][1])

    def test_projects_not_in_the_world_are_skipped(self):
        checker = Checker(FAIL)
        self.assertEqual(self.gate(checker).issues(set()), {})
        self.assertEqual(checker.calls, 0)


def world(not_ready, status="Draft"):
    proj = Project(key="demo", op_id=1, repo="e/d", visibility="Public",
                   has_test_env=False, test_signal=None, prod_signal=None)
    items = {
        1: Item(id=1, project="demo", type="Epic", status="Open",
                status_since=T0, subject="Maintenance"),
        2: Item(id=2, project="demo", type="Feature", status="Approved",
                status_since=T0, parent_id=1, subject="Maintenance"),
        3: Item(id=3, project="demo", type="Feature", status="Approved",
                status_since=T0, parent_id=1, subject="Project setup"),
        4: Item(id=4, project="demo", type="Feature", status="Approved",
                status_since=T0, parent_id=1, subject="Real feature"),
        5: Item(id=5, project="demo", type="Task", status=status,
                status_since=T0, parent_id=4, assignee="spark",
                reviewer="claude", size="S", risk="Low"),
    }
    return World(now=NOW, projects={"demo": proj}, items=items,
                 pull_requests={}, not_ready=not_ready)


MODEL = SimpleNamespace(global_default="Draft")


class RuleTests(unittest.TestCase):
    def ready_moves(self, w):
        return [c for c in stages(w, MODEL)
                if c.field == "status" and c.new == "Ready"]

    def test_ready_project_gets_ready_moves(self):
        self.assertEqual(len(self.ready_moves(world({}))), 1)

    def test_failing_or_pending_project_gets_none(self):
        for level in ("fail", "pending"):
            with self.subTest(level=level):
                self.assertEqual(
                    self.ready_moves(world({"demo": (level, "x")})), [])

    def test_warning_does_not_stop_ready_moves(self):
        self.assertEqual(len(self.ready_moves(
            world({"demo": ("warn", "Project warning: x")}))), 1)

    def test_one_action_on_the_setup_feature_only(self):
        w = world({"demo": ("fail", "Project not ready: a - fix it")})
        actions = [c for c in screens(w) if c.field == "action"]
        self.assertEqual([(c.key, c.new, c.reason) for c in actions],
                         [("3", "Unblock", "Project not ready: a - fix it")])

    def test_action_clears_itself_when_ready_again(self):
        w = world({})
        w.items[3] = replace(w.items[3], action="Unblock", needs_you=True)
        cleared = {c.field: c.new for c in screens(w) if c.key == "3"}
        self.assertEqual(cleared, {"needs_you": False, "action": None})

    def test_pending_raises_no_action(self):
        w = world({"demo": ("pending", "")})
        self.assertEqual([c for c in screens(w) if c.key == "3"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
