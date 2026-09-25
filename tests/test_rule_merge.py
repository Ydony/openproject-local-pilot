#!/usr/bin/env python3
"""Tests for the merge rule: each gate from the task text."""

import unittest
from datetime import datetime, timezone

from opl.conductor.rules.merge import merge
from opl.conductor.state import Item, Project, PullRequest, World

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
T0 = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
SHA = "c" * 40
MOVED = "d" * 40


def make_world(pr=None, **task_over):
    projects = {
        "demo": Project(key="demo", op_id=1, repo="e/d", visibility="Public",
                        has_test_env=False, test_signal=None, prod_signal=None),
    }
    task = {"id": 1, "project": "demo", "type": "Task", "status": "In review",
            "status_since": T0, "parent_id": 3, "assignee": "spark",
            "reviewer": "claude", "size": "S", "risk": "Low",
            "pr_url": "u://pr/1", "review_result": "Pass",
            "review_by_reviewer": True, "reviewed_sha": SHA}
    task.update(task_over)
    items = {1: Item(**task),
             2: Item(id=2, project="demo", type="Epic", status="Open",
                      status_since=T0),
             3: Item(id=3, project="demo", type="Feature", status="Approved",
                      status_since=T0, parent_id=2)}
    pull_requests = {}
    if pr is not None:
        pull_requests["u://pr/1"] = pr
    return World(now=NOW, projects=projects, items=items,
                 pull_requests=pull_requests)


def green_pr(merged=False, head_sha=SHA):
    return PullRequest(url="u://pr/1", merged=merged, merged_at=None,
                       checks_green=True, head_sha=head_sha)


class MergeTests(unittest.TestCase):
    def test_positive_low(self):
        changes = merge(make_world(pr=green_pr()))
        self.assertEqual(len(changes), 1)
        change = changes[0]
        self.assertEqual((change.rule, change.target, change.key, change.field,
                          change.new),
                         ("merge", "pr", "u://pr/1", "merge", SHA))
        self.assertEqual(change.reason,
                         "Merge: review passed and checks green at cccccccccccc")

    def test_high_without_merge_ok(self):
        self.assertEqual(merge(make_world(pr=green_pr(), risk="High")), [])

    def test_high_with_merge_ok(self):
        changes = merge(make_world(pr=green_pr(), risk="High", merge_ok=True,
                                   merge_ok_by_owner=True))
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].reason,
                         "Merge: review passed and checks green at cccccccccccc, "
                         "owner OK")
        self.assertEqual(changes[0].new, SHA)

    def test_checks_red(self):
        pr = PullRequest(url="u://pr/1", merged=False, merged_at=None,
                         checks_green=False)
        self.assertEqual(merge(make_world(pr=pr)), [])

    def test_review_changes_requested(self):
        world = make_world(pr=green_pr(), review_result="Changes requested")
        self.assertEqual(merge(world), [])

    def test_pr_already_merged(self):
        self.assertEqual(merge(make_world(pr=green_pr(merged=True))), [])

    def test_medium_waits_for_the_owner_during_the_pilot(self):
        # TH.D (owner decision 2026-09-25): Medium needs Merge OK too.
        self.assertEqual(merge(make_world(pr=green_pr(), risk="Medium")), [])
        changes = merge(make_world(pr=green_pr(), risk="Medium", merge_ok=True,
                                   merge_ok_by_owner=True))
        self.assertEqual([c.target for c in changes], ["pr"])
        self.assertTrue(changes[0].reason.endswith(", owner OK"))

    def test_low_still_merges_after_review(self):
        self.assertEqual([c.target for c in merge(make_world(pr=green_pr(),
                                                             risk="Low"))], ["pr"])

    def test_builder_set_merge_ok_does_not_merge(self):
        world = make_world(pr=green_pr(), risk="High", merge_ok=True,
                           merge_ok_by_owner=False)
        self.assertEqual(merge(world), [])

    def test_builder_set_review_pass_does_not_merge(self):
        world = make_world(pr=green_pr(), review_by_reviewer=False)
        self.assertEqual(merge(world), [])

    def test_head_moved_after_review_clears_the_pass(self):
        changes = merge(make_world(pr=green_pr(head_sha=MOVED)))
        self.assertEqual(len(changes), 1)
        change = changes[0]
        self.assertEqual((change.target, change.key, change.field, change.new),
                         ("item", "1", "review_result", None))
        self.assertIn("moved to dddddddddddd", change.reason)
        self.assertIn("review of cccccccccccc", change.reason)

    def test_moved_head_never_merges_even_with_owner_ok(self):
        world = make_world(pr=green_pr(head_sha=MOVED), risk="High",
                           merge_ok=True, merge_ok_by_owner=True)
        self.assertEqual([c.target for c in merge(world)], ["item"])

    def test_pass_without_reviewed_sha_is_cleared(self):
        changes = merge(make_world(pr=green_pr(), reviewed_sha=None))
        self.assertEqual([(c.field, c.new) for c in changes],
                         [("review_result", None)])
        self.assertIn("no `reviewed: <sha>`", changes[0].reason)

    def test_sha_compare_ignores_case(self):
        changes = merge(make_world(pr=green_pr(head_sha=SHA.upper())))
        self.assertEqual([c.target for c in changes], ["pr"])

    def test_unknown_head_waits(self):
        self.assertEqual(merge(make_world(pr=green_pr(head_sha=""))), [])

    def test_red_checks_still_clear_a_moved_review(self):
        pr = PullRequest(url="u://pr/1", merged=False, merged_at=None,
                         checks_green=False, head_sha=MOVED)
        self.assertEqual([c.field for c in merge(make_world(pr=pr))],
                         ["review_result"])

    def test_violating_task(self):
        world = make_world(pr=green_pr(), assignee="claude", reviewer="claude")
        self.assertEqual(merge(world), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
