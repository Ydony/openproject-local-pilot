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


class ScreensTests(unittest.TestCase):
    def actions(self, world):
        return {(c.key, c.field): (c.new, c.reason) for c in screens(world)
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

    def test_at_risk(self):
        world = add(make_world(), task(50, status="Blocked"))
        changes = [c for c in screens(world) if c.target == "project"]
        self.assertEqual(len(changes), 1)
        self.assertEqual((changes[0].key, changes[0].new), ("demo", True))
        calm = make_world()
        add(calm, task(51))
        self.assertEqual([c for c in screens(calm) if c.target == "project"], [])

    def test_idempotent(self):
        world = add(make_world(), task(60, status="Blocked"))
        add(world, task(61, assignee="codex"))
        first = screens(world)
        self.assertTrue(first)
        again = screens(apply_changes(world, first))
        self.assertEqual(again, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
