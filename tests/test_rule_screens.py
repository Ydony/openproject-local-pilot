#!/usr/bin/env python3
"""Tests for the screens rule: actions, precedence, models, at-risk."""

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from opl.conductor.rules.screens import screens
from opl.conductor.state import Item, Project, World, apply_changes

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
T0 = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def make_world():
    projects = {
        "demo": Project(key="demo", op_id=1, repo="e/d", visibility="Public",
                        has_test_env=False, test_signal=None, prod_signal=None),
    }
    items = {
        1: Item(id=1, project="demo", type="Epic", status="Open", status_since=T0),
        2: Item(id=2, project="demo", type="Feature", status="Approved",
                 status_since=T0, parent_id=1),
    }
    return World(now=NOW, projects=projects, items=items, pull_requests={})


def add(world, item):
    world.items[item.id] = item
    return world


def task(iid, parent_id=2, status="Ready", assignee="spark", reviewer="claude",
         size="S", risk="Low", since=T0, **extra):
    return Item(id=iid, project="demo", type="Task", status=status,
                status_since=since, parent_id=parent_id, assignee=assignee,
                reviewer=reviewer, size=size, risk=risk, **extra)


def feature(iid, status, **extra):
    return Item(id=iid, project="demo", type="Feature", status=status,
                status_since=T0, parent_id=1, **extra)


def maintenance_world():
    """Exactly what `bin/opl-configure` creates per project (issue #26):

    a Maintenance epic (Open) with a Maintenance feature (Approved) and
    no tasks yet. DESIGN.md section 1 keeps this standing pair empty
    until bugs and chores arrive.
    """
    projects = {
        "demo": Project(key="demo", op_id=1, repo="e/d", visibility="Public",
                        has_test_env=False, test_signal=None, prod_signal=None),
    }
    items = {
        1: Item(id=1, project="demo", type="Epic", status="Open",
                 status_since=T0, subject="Maintenance"),
        2: Item(id=2, project="demo", type="Feature", status="Approved",
                 status_since=T0, parent_id=1, subject="Maintenance"),
    }
    return World(now=NOW, projects=projects, items=items, pull_requests={})


def permcheck_world():
    """Synthetic sandbox for issue #34: Maintenance epic plus the configured
    `[permcheck] feature` ("Test feature", Proposed) under it in "demo"."""
    world = maintenance_world()
    world.items[10] = Item(id=10, project="demo", type="Feature",
                           status="Proposed", status_since=T0, parent_id=1,
                           subject="Test feature")
    return world


PERMCHECK = {"project": "demo", "feature": "Test feature"}


class ScreensTests(unittest.TestCase):
    def actions(self, world, permcheck=None):
        return {(c.key, c.field): (c.new, c.reason)
                for c in screens(world, permcheck)
                if c.target == "item" and c.field == "action"}

    def test_unblock_blocked_task(self):
        world = add(make_world(), task(10, status="Blocked"))
        actions = self.actions(world)
        self.assertEqual(actions[("10", "action")][0], "Unblock")

    def test_unblock_violation(self):
        world = add(make_world(), task(11, parent_id=None))
        actions = self.actions(world)
        new, reason = actions[("11", "action")]
        self.assertEqual(new, "Unblock")
        self.assertIn("Task must sit under a Feature", reason)

    def test_unblock_empty_feature(self):
        world = make_world()  # feature 2 Approved, no tasks
        actions = self.actions(world)
        new, reason = actions[("2", "action")]
        self.assertEqual(new, "Unblock")
        self.assertIn("no tasks", reason)

    def test_maintenance_feature_empty_is_quiet(self):
        # Live finding (issue #26): configure's standing Maintenance
        # feature (Approved, no tasks) must not fill Needs me.
        world = maintenance_world()
        self.assertNotIn(("2", "action"), self.actions(world))

    def test_maintenance_name_outside_maintenance_epic_still_unblocked(self):
        # The exemption is the configured pair, not the bare word: a
        # "Maintenance" feature under an ordinary epic is still stuck.
        world = maintenance_world()
        world.items[1] = replace(world.items[1], subject="User module")
        new, reason = self.actions(world)[("2", "action")]
        self.assertEqual(new, "Unblock")
        self.assertIn("no tasks", reason)

    def test_ordinary_approved_feature_named_otherwise_still_unblocked(self):
        world = maintenance_world()
        world.items[2] = replace(world.items[2], subject="Registration")
        new, reason = self.actions(world)[("2", "action")]
        self.assertEqual(new, "Unblock")
        self.assertIn("no tasks", reason)

    def test_permcheck_fixture_proposed_is_quiet(self):
        # Live finding (issue #34): the permcheck fixture is not real work.
        world = permcheck_world()
        self.assertNotIn(("10", "action"), self.actions(world, PERMCHECK))
        needs = [c for c in screens(world, PERMCHECK)
                 if c.key == "10" and c.field == "needs_you"]
        self.assertEqual(needs, [])

    def test_permcheck_fixture_without_config_still_approve(self):
        world = permcheck_world()
        self.assertEqual(self.actions(world)[("10", "action")][0], "Approve")
        self.assertEqual(
            self.actions(world, {"project": "", "feature": "Test feature"})
            [("10", "action")][0], "Approve")

    def test_permcheck_same_subject_other_project_still_approve(self):
        world = permcheck_world()
        world.projects["other"] = replace(world.projects["demo"], key="other")
        world.items[20] = Item(id=20, project="other", type="Epic",
                               status="Open", status_since=T0,
                               subject="Maintenance")
        world.items[21] = Item(id=21, project="other", type="Feature",
                               status="Proposed", status_since=T0,
                               parent_id=20, subject="Test feature")
        self.assertNotIn(("10", "action"), self.actions(world, PERMCHECK))
        self.assertEqual(
            self.actions(world, PERMCHECK)[("21", "action")][0], "Approve")

    def test_permcheck_same_subject_outside_maintenance_epic_still_approve(self):
        world = permcheck_world()
        world.items[30] = Item(id=30, project="demo", type="Epic",
                               status="Open", status_since=T0,
                               subject="User module")
        world.items[31] = Item(id=31, project="demo", type="Feature",
                               status="Proposed", status_since=T0,
                               parent_id=30, subject="Test feature")
        self.assertNotIn(("10", "action"), self.actions(world, PERMCHECK))
        self.assertEqual(
            self.actions(world, PERMCHECK)[("31", "action")][0], "Approve")

    def test_permcheck_stale_action_is_cleared(self):
        world = permcheck_world()
        world.items[10] = replace(world.items[10], action="Approve",
                                  needs_you=True)
        changes = {(c.key, c.field): c.new for c in screens(world, PERMCHECK)
                   if c.key == "10" and c.field in ("action", "needs_you")}
        self.assertEqual(changes, {("10", "action"): None,
                                   ("10", "needs_you"): False})

    def test_permcheck_default_feature_when_missing(self):
        # Reuses opl.permcheck.DEFAULT_FEATURE: no "feature" key still
        # exempts "Test feature".
        world = permcheck_world()
        self.assertNotIn(("10", "action"),
                         self.actions(world, {"project": "demo"}))

    def test_permcheck_settings_name_resolves_to_key(self):
        from types import SimpleNamespace

        world = permcheck_world()
        settings = SimpleNamespace(
            permcheck={"project": "Sandbox", "feature": "Test feature"},
            projects=(SimpleNamespace(key="demo", name="Sandbox"),),
        )
        self.assertNotIn(("10", "action"), self.actions(world, settings))

    def test_permcheck_run_once_ignores_fixture(self):
        from types import SimpleNamespace

        from opl.conductor.engine import run_once

        model = SimpleNamespace(transitions=lambda *a: set())
        world = permcheck_world()
        changes = run_once(world, model, permcheck=PERMCHECK)
        self.assertEqual(
            [c for c in changes if c.key == "10" and c.field == "action"], [])
        plain = run_once(world, model)
        self.assertEqual(
            [(c.new, c.reason) for c in plain
             if c.key == "10" and c.field == "action"][0][0], "Approve")

    def test_ok_merge(self):
        world = add(make_world(), task(12, status="In review", risk="High",
                                       review_result="Pass",
                                       review_by_reviewer=True))
        self.assertEqual(self.actions(world)[("12", "action")][0], "OK merge")

    def test_ok_merge_for_medium_too(self):
        # TH.D: Medium-risk tasks also ask the owner for Merge OK.
        world = add(make_world(), task(14, status="In review", risk="Medium",
                                       review_result="Pass", assignee="claude",
                                       reviewer="codex",
                                       review_by_reviewer=True))
        new, reason = self.actions(world)[("14", "action")]
        self.assertEqual(new, "OK merge")
        self.assertIn("Medium-risk", reason)

    def test_precedence_unblock_beats_ok_merge(self):
        # In review + High + Pass would qualify for OK merge, but a
        # reviewer==assignee violation must surface as Unblock instead.
        world = add(make_world(), task(13, status="In review", risk="High",
                                       review_result="Pass", assignee="claude",
                                       reviewer="claude"))
        self.assertEqual(self.actions(world)[("13", "action")][0], "Unblock")

    def test_approve(self):
        world = add(make_world(), feature(20, "Proposed"))
        self.assertEqual(self.actions(world)[("20", "action")][0], "Approve")

    def test_decide_deploy(self):
        world = add(make_world(), feature(21, "In test", test_result="Pass"))
        add(world, task(211, parent_id=21, status="Merged"))
        self.assertEqual(self.actions(world)[("21", "action")][0], "Decide deploy")
        world2 = add(make_world(), feature(21, "In test"))
        add(world2, task(212, parent_id=21, status="Merged"))
        self.assertNotIn(("21", "action"), self.actions(world2))

    def test_confirm_done(self):
        world = add(make_world(), feature(22, "In production"))
        add(world, task(221, parent_id=22, status="Merged"))
        self.assertEqual(self.actions(world)[("22", "action")][0], "Confirm done")

    def test_reassign_timing(self):
        young = add(make_world(), task(30, assignee="claude", reviewer="spark",
                                       status="Ready",
                                       since=NOW - timedelta(days=2)))
        self.assertNotIn(("30", "action"), self.actions(young))
        old = add(make_world(), task(30, assignee="claude", reviewer="spark",
                                     status="Ready",
                                     since=NOW - timedelta(days=3)))
        self.assertEqual(self.actions(old)[("30", "action")][0], "Reassign")
        spark = add(make_world(), task(31, assignee="spark", status="Ready",
                                       since=NOW - timedelta(days=9)))
        self.assertNotIn(("31", "action"), self.actions(spark))

    def test_models_list(self):
        world = make_world()
        add(world, task(40, assignee="codex"))
        add(world, task(41, assignee="spark"))
        changes = [c for c in screens(world)
                   if c.key == "2" and c.field == "models"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].new, ("Codex", "Spark"))

    def test_models_ignore_non_model_assignees(self):
        # #50: a task assigned to the owner is valid, but the owner is not
        # a Models option; listing them failed the feature's update forever.
        world = make_world()
        add(world, task(40, assignee="codex"))
        add(world, task(41, assignee="admin", status="Draft"))
        changes = [c for c in screens(world)
                   if c.key == "2" and c.field == "models"]
        self.assertEqual([c.new for c in changes], [("Codex",)])

    def _handover_world(self, status="In test", assignee=None):
        world = make_world()
        world.projects["demo"] = replace(world.projects["demo"],
                                         owner_id=1, owner_login="admin")
        world.items[2] = replace(world.items[2], status=status, assignee=assignee)
        add(world, task(40, status="Merged", pr_url="https://github.com/e/d/pull/7"))
        add(world, task(41, status="Dropped"))
        return world

    def test_finished_feature_goes_to_the_owner_with_a_summary(self):
        # #55: the owner tests and closes a feature once its tasks are done.
        world = self._handover_world()
        (change,) = [c for c in screens(world)
                     if c.key == "2" and c.field == "assignee"]
        self.assertEqual(change.new, "admin")
        self.assertIn("#40 https://github.com/e/d/pull/7", change.reason)
        self.assertIn("Dropped: #41", change.reason)

    def test_handover_happens_once(self):
        world = self._handover_world(assignee="admin")
        self.assertEqual([c for c in screens(world) if c.field == "assignee"], [])

    def test_building_feature_is_not_handed_over(self):
        world = self._handover_world(status="Building")
        self.assertEqual([c for c in screens(world) if c.field == "assignee"], [])

    def test_at_risk(self):
        world = add(make_world(), task(50, status="Blocked"))
        changes = [c for c in screens(world) if c.target == "project"]
        self.assertEqual(len(changes), 1)
        self.assertEqual((changes[0].key, changes[0].new), ("demo", True))
        calm = make_world()
        add(calm, task(51))
        self.assertEqual([c for c in screens(calm) if c.target == "project"], [])

    def test_unblock_type_status_violation(self):
        # A Draft Epic is flagged by the enforce rule and must surface as
        # Unblock/Needs me like any other violation, never fixed silently.
        world = make_world()
        add(world, Item(id=70, project="demo", type="Epic", status="Draft",
                        status_since=T0))
        new, reason = self.actions(world)[("70", "action")]
        self.assertEqual(new, "Unblock")
        self.assertIn("Epic has Task status Draft; set Open", reason)

    def test_idempotent(self):
        world = add(make_world(), task(60, status="Blocked"))
        add(world, task(61, assignee="codex"))
        first = screens(world)
        self.assertTrue(first)
        again = screens(apply_changes(world, first))
        self.assertEqual(again, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
