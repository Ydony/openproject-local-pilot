#!/usr/bin/env python3
"""Tests for opl.conductor.state: apply_changes purity and semantics."""

import unittest
from datetime import datetime, timezone

from opl.conductor.state import (
    Change,
    Item,
    Project,
    PullRequest,
    World,
    apply_changes,
)


def make_world():
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    earlier = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    projects = {
        "demo": Project(
            key="demo", op_id=1, repo="example-owner/demo",
            visibility="Public", has_test_env=False,
            test_signal=None, prod_signal=None,
        ),
    }
    items = {
        1: Item(id=1, project="demo", type="Epic", status="Open",
                 status_since=earlier),
        2: Item(id=2, project="demo", type="Feature", status="Proposed",
                 status_since=earlier, parent_id=1),
        3: Item(id=3, project="demo", type="Task", status="Draft",
                 status_since=earlier, parent_id=2),
        4: Item(id=4, project="demo", type="Task", status="Draft",
                 status_since=earlier, parent_id=3),
    }
    pull_requests = {
        "https://example.invalid/pr/1": PullRequest(
            url="https://example.invalid/pr/1", merged=False,
            merged_at=None, checks_green=True),
    }
    return World(now=now, projects=projects, items=items,
                 pull_requests=pull_requests)


class ApplyChangesTests(unittest.TestCase):
    def test_status_change_updates_status_and_since(self):
        world = make_world()
        out = apply_changes(world, [Change(rule="stages", target="item",
                                           key="3", field="status",
                                           new="Ready", reason="r")])
        self.assertEqual(out.items[3].status, "Ready")
        self.assertEqual(out.items[3].status_since, world.now)

    def test_project_change(self):
        world = make_world()
        out = apply_changes(world, [Change(rule="screens", target="project",
                                           key="demo", field="at_risk",
                                           new=True, reason="r")])
        self.assertTrue(out.projects["demo"].at_risk)

    def test_pr_merge(self):
        world = make_world()
        out = apply_changes(world, [Change(rule="merge", target="pr",
                                           key="https://example.invalid/pr/1",
                                           field="merge", new=True, reason="r")])
        pr = out.pull_requests["https://example.invalid/pr/1"]
        self.assertTrue(pr.merged)
        self.assertEqual(pr.merged_at, world.now)

    def test_input_world_is_unchanged(self):
        world = make_world()
        before_item = world.items[3]
        before_projects = dict(world.projects)
        out = apply_changes(world, [
            Change(rule="x", target="item", key="3", field="status",
                   new="Ready", reason="r"),
            Change(rule="x", target="project", key="demo", field="at_risk",
                   new=True, reason="r"),
        ])
        self.assertIs(world.items[3], before_item)
        self.assertEqual(world.items[3].status, "Draft")
        self.assertEqual(world.projects, before_projects)
        self.assertIsNot(out, world)
        self.assertIsNot(out.items[3], before_item)

    def test_children_returns_only_direct_children(self):
        world = make_world()
        self.assertEqual([i.id for i in world.children(2)], [3])
        self.assertEqual([i.id for i in world.children(1)], [2])
        self.assertEqual(world.children(4), [])

    def test_empty_changes_return_equal_world(self):
        world = make_world()
        self.assertEqual(apply_changes(world, []), world)


if __name__ == "__main__":
    unittest.main(verbosity=2)
