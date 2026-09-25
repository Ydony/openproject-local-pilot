#!/usr/bin/env python3
"""End-to-end conductor scenarios on in-memory Worlds.

run_once + apply_changes drive each story; an explicit second run_once
after every step proves idempotency. Uses the shipped model, so the
ownership guard constrains conductor moves exactly as in production.
"""

import os
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from opl.conductor.engine import run_once
from opl.conductor.state import (
    Change,
    Deploy,
    Item,
    Project,
    PullRequest,
    World,
    apply_changes,
)
from opl.model import load as load_model

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
T0 = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def model():
    return load_model(os.path.join(REPO, "config", "pm-model.toml"))


def project(key="demo", visibility="Public", has_test_env=True):
    return Project(key=key, op_id=1, repo="e/d", visibility=visibility,
                   has_test_env=has_test_env, test_signal=None,
                   prod_signal=None)


def item(iid, type, status, parent_id=None, project="demo", **extra):
    fields = {"id": iid, "project": project, "type": type, "status": status,
              "status_since": T0, "parent_id": parent_id}
    fields.update(extra)
    return Item(**fields)


def complete_task(iid, parent_id, status="Draft", predecessors=(),
                  pr_url=None, assignee="spark", **extra):
    fields = {"assignee": assignee, "reviewer": "claude", "size": "S",
              "risk": "Low", "predecessors": tuple(predecessors),
              "pr_url": pr_url}
    fields.update(extra)
    return item(iid, "Task", status, parent_id, **fields)


class ScenarioBase(unittest.TestCase):
    def setUp(self):
        self.model = model()

    def settle(self, world):
        """Run rounds until quiet; every intermediate rerun must be empty after."""
        for _ in range(10):
            changes = run_once(world, self.model)
            if not changes:
                return world
            world = apply_changes(world, changes)
        self.fail("conductor did not converge")

    def action_of(self, world, iid):
        return world.items[iid].action

    def external_status(self, world, iid, status):
        return apply_changes(world, [Change(rule="external", target="item",
                                            key=str(iid), field="status",
                                            new=status, reason="test")])

    def external_field(self, world, iid, field, value):
        return apply_changes(world, [Change(rule="external", target="item",
                                            key=str(iid), field=field,
                                            new=value, reason="test")])


class LifecycleTests(ScenarioBase):
    def test_full_lifecycle(self):
        world = World(
            now=NOW,
            projects={"demo": project()},
            items={
                1: item(1, "Epic", "Open"),
                2: item(2, "Feature", "Proposed", parent_id=1),
                3: complete_task(3, 2),
                4: complete_task(4, 2, predecessors=(3,)),
            },
            pull_requests={},
        )
        world = self.settle(world)
        self.assertEqual(self.action_of(world, 2), "Approve")

        world = self.external_status(world, 2, "Approved")
        world = self.settle(world)
        self.assertEqual(world.items[3].status, "Ready")
        self.assertEqual(world.items[4].status, "Draft")
        self.assertIsNone(self.action_of(world, 2))

        world = self.external_status(world, 3, "In progress")
        world = self.settle(world)
        self.assertEqual(world.items[2].status, "Building")

        world = self.external_status(world, 3, "In review")
        world.pull_requests["https://github.com/e/d/pull/3"] = PullRequest(
            base_repo="e/d", head_repo="e/d", url="https://github.com/e/d/pull/3", merged=False, merged_at=None, checks_green=True,
            head_sha="3" * 40)
        world.items[3] = replace(world.items[3], pr_url="https://github.com/e/d/pull/3")
        world = self.external_field(world, 3, "review_result", "Pass")
        world = self.external_field(world, 3, "review_by_reviewer", True)
        world = self.external_field(world, 3, "reviewed_sha", "3" * 40)
        world = self.settle(world)
        self.assertEqual(world.items[3].status, "Merged")
        self.assertTrue(world.pull_requests["https://github.com/e/d/pull/3"].merged)
        self.assertEqual(world.items[4].status, "Ready")

        world = self.external_status(world, 4, "In progress")
        world = self.settle(world)
        self.assertEqual(world.items[4].status, "In progress")
        self.assertEqual(world.items[2].status, "Building")
        world = self.external_status(world, 4, "In review")
        world.pull_requests["https://github.com/e/d/pull/4"] = PullRequest(
            base_repo="e/d", head_repo="e/d", url="https://github.com/e/d/pull/4", merged=False, merged_at=None, checks_green=True,
            head_sha="4" * 40)
        world.items[4] = replace(world.items[4], pr_url="https://github.com/e/d/pull/4")
        world = self.external_field(world, 4, "review_result", "Pass")
        world = self.external_field(world, 4, "review_by_reviewer", True)
        world = self.external_field(world, 4, "reviewed_sha", "4" * 40)
        world = self.settle(world)
        self.assertEqual(world.items[4].status, "Merged")

        world = replace(world, deploys=world.deploys + (
            Deploy(project="demo", target="test", at=NOW),))
        world = self.settle(world)
        self.assertEqual(world.items[2].status, "In test")

        world = self.external_field(world, 2, "test_result", "Pass")
        world = self.settle(world)
        self.assertEqual(self.action_of(world, 2), "Decide deploy")

        world = replace(world, deploys=world.deploys + (
            Deploy(project="demo", target="production", at=NOW),))
        world = self.settle(world)
        self.assertEqual(world.items[2].status, "In production")
        self.assertEqual(self.action_of(world, 2), "Confirm done")

        world = self.external_status(world, 2, "Done")
        world = self.settle(world)
        self.assertIsNone(self.action_of(world, 2))
        self.assertFalse(world.items[2].needs_you)


class PrivateProjectTests(ScenarioBase):
    def test_spark_task_blocked_and_at_risk(self):
        world = World(
            now=NOW,
            projects={"priv": project(key="priv", visibility="Private",
                                      has_test_env=False)},
            items={
                1: item(1, "Epic", "Open", project="priv"),
                2: item(2, "Feature", "Approved", parent_id=1, project="priv"),
                3: complete_task(3, 2, project="priv"),
            },
            pull_requests={},
        )
        first = run_once(world, self.model)
        block = [c for c in first if c.field == "status"]
        self.assertEqual(len(block), 1)
        self.assertIn("Spark may not work on Private projects", block[0].reason)
        world = self.settle(world)
        self.assertEqual(world.items[3].status, "Blocked")
        self.assertEqual(self.action_of(world, 3), "Unblock")
        self.assertTrue(world.projects["priv"].at_risk)


class ReassignTests(ScenarioBase):
    def test_claude_task_waiting_three_days(self):
        old = NOW - timedelta(days=3)
        world = World(
            now=NOW,
            projects={"demo": project(has_test_env=False)},
            items={
                1: item(1, "Epic", "Open"),
                2: item(2, "Feature", "Approved", parent_id=1),
                3: complete_task(3, 2, status="Ready", assignee="claude",
                                 reviewer="codex"),
            },
            pull_requests={},
        )
        world.items[3] = replace(world.items[3], status_since=old)
        world = self.settle(world)
        self.assertEqual(self.action_of(world, 3), "Reassign")


if __name__ == "__main__":
    unittest.main(verbosity=2)
