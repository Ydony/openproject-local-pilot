#!/usr/bin/env python3
"""Tests for the enforce rule: one violating/compliant pair per row."""

import unittest
from datetime import datetime, timezone

from opl.conductor.rules.enforce import enforce, violations
from opl.conductor.state import Item, Project, World

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def make_world():
    projects = {
        "pub": Project(key="pub", op_id=1, repo="e/p", visibility="Public",
                       has_test_env=False, test_signal=None, prod_signal=None),
        "priv": Project(key="priv", op_id=2, repo="e/q", visibility="Private",
                        has_test_env=False, test_signal=None, prod_signal=None),
    }
    items = {
        1: Item(id=1, project="pub", type="Epic", status="Open",
                 status_since=NOW),
        2: Item(id=2, project="pub", type="Feature", status="Proposed",
                 status_since=NOW, parent_id=1),
        3: Item(id=3, project="pub", type="Feature", status="Approved",
                 status_since=NOW, parent_id=1),
    }
    return World(now=NOW, projects=projects, items=items, pull_requests={})


def task(iid, parent_id=3, project="pub", status="Ready", assignee="spark",
         reviewer="claude", size="S", risk="Low", **extra):
    return Item(id=iid, project=project, type="Task", status=status,
                status_since=NOW, parent_id=parent_id, assignee=assignee,
                reviewer=reviewer, size=size, risk=risk, **extra)


class EnforceTests(unittest.TestCase):
    def check(self, item, reason=None):
        world = make_world()
        world.items[item.id] = item
        found = violations(world)
        changes = enforce(world)
        if reason is None:
            self.assertNotIn(item.id, found)
            self.assertEqual([c for c in changes if c.key == str(item.id)], [])
        else:
            self.assertEqual(found.get(item.id), reason)
            matched = [c for c in changes if c.key == str(item.id)]
            self.assertEqual(len(matched), 1)
            change = matched[0]
            self.assertEqual((change.rule, change.target, change.field, change.new),
                             ("enforce", "item", "status", "Blocked"))
            self.assertEqual(change.reason, reason)

    def test_merge_ok_needs_the_owner(self):
        reason = "Unauthorised approval: Merge OK was not set by the owner"
        self.check(task(20, status="In review", risk="High", merge_ok=True), reason)
        self.check(task(21, status="In review", risk="High", merge_ok=True,
                        merge_ok_by_owner=True))
        self.check(task(22, status="In review", risk="High"))

    def test_review_pass_needs_the_reviewer(self):
        reason = ("Unauthorised approval: Review result was not set by "
                  "the task's reviewer")
        self.check(task(23, status="In review", review_result="Pass"), reason)
        self.check(task(24, status="In review", review_result="Pass",
                        review_by_reviewer=True))
        # Only a Pass is an approval; a builder asking for changes is not.
        self.check(task(25, status="In progress",
                        review_result="Changes requested"))

    def test_pr_link_of_another_repo_blocks(self):
        # PR #6 review: the link is editable; a foreign one blocks the task.
        self.check(task(27, status="In review",
                        **{"pr_url": "https://github.com/other/repo/pull/1"}),
                   "PR link 'https://github.com/other/repo/pull/1' is not a "
                   "pull request of e/p")
        self.check(task(28, status="In review",
                        **{"pr_url": "https://github.com/e/p/pull/3"}))

    def test_merged_task_is_not_judged(self):
        self.check(task(26, status="Merged", merge_ok=True, review_result="Pass"))

    def test_e1_task_needs_feature_parent(self):
        self.check(task(10, parent_id=None), "Task must sit under a Feature")
        world = make_world()
        world.items[10] = task(10, parent_id=2)
        world.items[11] = task(11, parent_id=10)  # parent is a Task
        found = violations(world)
        self.assertEqual(found[11], "Task must sit under a Feature")
        self.check(task(12), None)

    def test_e2_feature_needs_epic_parent(self):
        world = make_world()
        world.items[20] = Item(id=20, project="pub", type="Feature",
                               status="Proposed", status_since=NOW,
                               parent_id=None)
        self.assertEqual(violations(world)[20], "Feature must sit under an Epic")
        world.items[21] = Item(id=21, project="pub", type="Feature",
                               status="Proposed", status_since=NOW, parent_id=3)
        found = violations(world)
        self.assertEqual(found[21], "Feature must sit under an Epic")
        # E2 produces no change (non-task violations are screens' business).
        self.assertEqual(enforce(world), [])

    def test_e3_spark_not_on_private(self):
        self.check(task(30, project="priv"), "Spark may not work on Private projects")
        self.check(task(31, project="pub"), None)

    def test_e4_reviewer_differs(self):
        self.check(task(40, assignee="claude", reviewer="claude"),
                   "Reviewer must differ from builder")
        # The one exemption: Public + Low + both spark.
        self.check(task(41, assignee="spark", reviewer="spark"), None)
        self.check(task(42, assignee="spark", reviewer="spark", risk="Medium"),
                   "Reviewer must differ from builder")

    def test_e5_risk_needs_claude_or_codex(self):
        self.check(task(50, risk="Medium", assignee="claude", reviewer="spark"),
                   "Risk needs a Claude or Codex reviewer")
        self.check(task(51, project="priv", risk="Low", assignee="claude",
                          reviewer="spark"),
                   "Risk needs a Claude or Codex reviewer")
        self.check(task(52, risk="High", reviewer="codex"), None)

    def test_unset_reviewer_waits_for_approval(self):
        # Planning: Draft under Proposed, risk Medium, no reviewer yet.
        world = make_world()
        world.items[90] = Item(id=90, project="pub", type="Task", status="Draft",
                               status_since=NOW, parent_id=2, assignee="spark",
                               reviewer=None, size="S", risk="Medium")
        self.assertNotIn(90, violations(world))
        # Same task after approval: E6 wants the missing reviewer.
        world.items[2] = world.items[2].__class__(
            **{**world.items[2].__dict__, "status": "Approved"})
        self.assertEqual(violations(world)[90],
                         "Task needs assignee, reviewer, size and risk")

    def test_e6_task_needs_four_fields(self):
        self.check(task(60, assignee=None), "Task needs assignee, reviewer, size and risk")
        world = make_world()
        world.items[62] = task(62, reviewer=None, risk="Low")
        found = violations(world)
        # risk Low + Public + reviewer None: E5 silent (risk not Med/High,
        # project not Private), so only E6 (missing reviewer under Approved).
        self.assertEqual(found[62], "Task needs assignee, reviewer, size and risk")

    def test_blocked_tasks_yield_no_change(self):
        world = make_world()
        world.items[70] = task(70, parent_id=None, status="Blocked")
        self.assertIn(70, violations(world))
        self.assertEqual(enforce(world), [])

    def test_draft_under_proposed_is_not_a_violation(self):
        world = make_world()
        world.items[80] = Item(id=80, project="pub", type="Task", status="Draft",
                               status_since=NOW, parent_id=2)
        self.assertEqual(violations(world), {})
        self.assertEqual(enforce(world), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
