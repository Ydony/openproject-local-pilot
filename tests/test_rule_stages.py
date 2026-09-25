#!/usr/bin/env python3
"""Tests for the stages rule: each move, closest negatives, idempotency."""

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from opl.conductor.rules.stages import stages
from opl.conductor.state import Deploy, Item, Project, PullRequest, World, apply_changes

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
T0 = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def project(has_test_env=True):
    return Project(key="demo", op_id=1, repo="e/d", visibility="Public",
                   has_test_env=has_test_env, test_signal=None,
                   prod_signal=None)


def pr(url, merged=True, at=None, green=True):
    return PullRequest(base_repo="e/d", head_repo="e/d", url=url, merged=merged, merged_at=at, checks_green=green)


def build(has_test_env=True, feature_status="Approved"):
    items = {
        1: Item(id=1, project="demo", type="Epic", status="Open", status_since=T0),
        2: Item(id=2, project="demo", type="Feature", status=feature_status,
                 status_since=T0, parent_id=1),
    }
    return World(now=NOW, projects={"demo": project(has_test_env)},
                 items=items, pull_requests={})


def add_task(world, iid, status="Draft", predecessors=(), pr_url=None,
             assignee="spark", reviewer="claude", size="S", risk="Low"):
    world.items[iid] = Item(
        id=iid, project="demo", type="Task", status=status, status_since=T0,
        parent_id=2, assignee=assignee, reviewer=reviewer, size=size,
        risk=risk, pr_url=pr_url, predecessors=tuple(predecessors))


def merged_task(world, iid, pr_url, merged_at):
    world.pull_requests[pr_url] = pr(pr_url, True, merged_at)
    world.items[iid] = Item(
        id=iid, project="demo", type="Task", status="Merged", status_since=T0,
        parent_id=2, assignee="spark", reviewer="claude", size="S",
        risk="Low", pr_url=pr_url)


class StagesTests(unittest.TestCase):
    def moves(self, world, model=None):
        # stages() only reads model.global_default; a namespace keeps these
        # unit tests independent of the shipped TOML.
        return {(c.key, c.new): c.reason
                for c in stages(world, model or SimpleNamespace(global_default="Draft"))}

    def test_s0_moves_created_without_status(self):
        world = build(feature_status="Draft")
        moves = self.moves(world)
        self.assertEqual(moves.get(("2", "Proposed")),
                         "Proposed: created without a status")

    def test_s0_moves_epic_without_status(self):
        world = build(feature_status="Draft")
        world.items[1] = world.items[1].__class__(
            **{**world.items[1].__dict__, "status": "Draft"})
        moves = self.moves(world)
        self.assertEqual(moves.get(("1", "Open")),
                         "Open: created without a status")

    def test_s0_skips_violating_items(self):
        world = build(feature_status="Draft")
        world.items[2] = world.items[2].__class__(
            **{**world.items[2].__dict__, "parent_id": None})
        self.assertNotIn(("2", "Proposed"), self.moves(world))

    def test_s1_positive(self):
        world = build()
        add_task(world, 3, predecessors=())
        merged_task(world, 4, "https://github.com/e/d/pull/4", NOW - timedelta(days=1))
        world.items[3] = world.items[3].__class__(
            **{**world.items[3].__dict__, "predecessors": (4,)})
        moves = self.moves(world)
        self.assertEqual(moves.get(("3", "Ready")),
                         "Ready: feature approved and predecessors merged")

    def test_s1_predecessor_not_merged(self):
        world = build()
        add_task(world, 3, predecessors=(4,))
        add_task(world, 4, status="In review", pr_url="https://github.com/e/d/pull/4")
        self.assertNotIn(("3", "Ready"), self.moves(world))

    def test_s1_needs_approved_or_building_parent(self):
        world = build(feature_status="Proposed")
        add_task(world, 3)
        self.assertNotIn(("3", "Ready"), self.moves(world))

    def test_s2_positive(self):
        world = build()
        add_task(world, 3, status="In progress")
        moves = self.moves(world)
        self.assertEqual(moves.get(("2", "Building")), "Building: work started")

    def test_s3_positive(self):
        world = build()
        add_task(world, 3, status="In review", pr_url="https://github.com/e/d/pull/3")
        world.pull_requests["https://github.com/e/d/pull/3"] = pr("https://github.com/e/d/pull/3", True, NOW)
        moves = self.moves(world)
        self.assertEqual(moves.get(("3", "Merged")), "Merged: pull request merged")

    def test_s3_unmerged_pr_no_move(self):
        world = build()
        add_task(world, 3, status="In review", pr_url="https://github.com/e/d/pull/3")
        world.pull_requests["https://github.com/e/d/pull/3"] = pr("https://github.com/e/d/pull/3", False, None)
        self.assertNotIn(("3", "Merged"), self.moves(world))

    def test_s4_with_test_deploy(self):
        world = build()
        world.items[2] = world.items[2].__class__(
            **{**world.items[2].__dict__, "status": "Building"})
        merged_task(world, 3, "https://github.com/e/d/pull/3", NOW - timedelta(days=2))
        world = replace(world, deploys=(Deploy(project="demo", target="test",
                                               at=NOW - timedelta(days=1)),))
        moves = self.moves(world)
        self.assertEqual(moves.get(("2", "In test")),
                         "In test: all tasks merged and deployed to test")

    def test_s4_old_deploy_no_move(self):
        world = build()
        world.items[2] = world.items[2].__class__(
            **{**world.items[2].__dict__, "status": "Building"})
        merged_task(world, 3, "https://github.com/e/d/pull/3", NOW - timedelta(days=1))
        world = replace(world, deploys=(Deploy(project="demo", target="test",
                                               at=NOW - timedelta(days=2)),))
        self.assertNotIn(("2", "In test"), self.moves(world))

    def test_s4_no_test_env(self):
        world = build(has_test_env=False)
        world.items[2] = world.items[2].__class__(
            **{**world.items[2].__dict__, "status": "Building"})
        merged_task(world, 3, "https://github.com/e/d/pull/3", NOW - timedelta(days=1))
        moves = self.moves(world)
        self.assertEqual(moves.get(("2", "In test")),
                         "In test: all tasks merged (no test environment)")

    def test_s5_positive_and_negative(self):
        world = build()
        world.items[2] = world.items[2].__class__(
            **{**world.items[2].__dict__, "status": "In test"})
        merged_task(world, 3, "https://github.com/e/d/pull/3", NOW - timedelta(days=2))
        self.assertNotIn(("2", "In production"), self.moves(world))
        world = replace(world, deploys=(Deploy(project="demo", target="production",
                                               at=NOW - timedelta(days=1)),))
        moves = self.moves(world)
        self.assertEqual(moves.get(("2", "In production")),
                         "In production: production deploy seen")

    def test_unknown_merge_time_blocks_deploy_moves(self):
        for status, target in (("Building", "In test"), ("In test", "In production")):
            world = build(feature_status=status)
            add_task(world, 3, status="Merged")
            world = replace(world, deploys=(
                Deploy(project="demo", target="test", at=NOW - timedelta(days=30)),
                Deploy(project="demo", target="production", at=NOW - timedelta(days=30)),
            ))
            self.assertNotIn(("2", target), self.moves(world),
                             "stale deploy must not promote without merge time")

    def test_one_unknown_merge_time_blocks_deploy_moves(self):
        # One task's merge time is known, another's is not: a deploy newer
        # than the known merge can still predate the unknown one.
        for status, target in (("Building", "In test"), ("In test", "In production")):
            world = build(feature_status=status)
            merged_task(world, 3, "https://github.com/e/d/pull/3", NOW - timedelta(days=5))
            add_task(world, 4, status="Merged")
            world = replace(world, deploys=(
                Deploy(project="demo", target="test", at=NOW - timedelta(days=2)),
                Deploy(project="demo", target="production", at=NOW - timedelta(days=2)),
            ))
            self.assertNotIn(("2", target), self.moves(world),
                             "a partly unknown merge time must not promote")

    def test_feature_without_tasks_never_moves(self):
        for status in ("Approved", "Building", "In test", "Proposed"):
            world = build(feature_status=status)
            self.assertEqual(stages(world, SimpleNamespace(global_default="Draft")), [], status)

    def test_violating_items_are_skipped(self):
        world = build()
        # S3-eligible (In review + merged PR) but reviewer == assignee (E4).
        add_task(world, 3, status="In review", pr_url="https://github.com/e/d/pull/3",
                 assignee="claude", reviewer="claude")
        world.pull_requests["https://github.com/e/d/pull/3"] = pr("https://github.com/e/d/pull/3", True, NOW)
        self.assertNotIn(("3", "Merged"), self.moves(world))

    def test_idempotent(self):
        world = build()
        add_task(world, 3, status="In progress")
        first = stages(world, SimpleNamespace(global_default="Draft"))
        self.assertTrue(first)
        again = stages(apply_changes(world, first), SimpleNamespace(global_default="Draft"))
        self.assertEqual(again, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
