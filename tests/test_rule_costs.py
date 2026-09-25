#!/usr/bin/env python3
"""Tests for the costs rule: estimates, actuals, roll-ups, idempotency."""

import unittest

from opl.conductor.rules.costs import costs
from opl.conductor.state import Item, Project, World

PRICES = {
    "models": [
        {"match": "claude-*", "input": 3.0, "output": 15.0,
         "cache_read": 0.3, "cache_write": 3.75,
         "billing": "subscription", "as_of": "2026-09-25"},
        {"match": "gpt-*-codex*", "input": 1.75, "output": 14.0,
         "cache_read": 0.175, "cache_write": 1.75,
         "billing": "subscription", "as_of": "2026-09-25"},
        {"match": "*spark*", "input": 0.1, "output": 0.2,
         "cache_read": 0.002, "cache_write": 0.1, "billing": "api",
         "as_of": "2026-09-25"},
    ],
    "defaults": {"claude": "claude-*", "codex": "gpt-*-codex*",
                 "spark": "*spark*"},
}

ESTIMATES = {
    ("claude", "S"): {"input_tokens": 100000, "output_tokens": 20000},
    ("spark", "S"): {"input_tokens": 100000, "output_tokens": 20000},
    ("claude", "M"): {"input_tokens": 300000, "output_tokens": 60000},
}


def make_world(**over):
    task_kwargs = {"id": 5, "project": "demo", "type": "Task",
                   "status": "In progress",
                   "status_since": "2026-09-20T12:00:00Z", "parent_id": 2,
                   "assignee": "claude", "size": "S",
                   "est_cost": None, "actual_cost": None,
                   "actual_tokens": None}
    task_kwargs.update(over.get("task", {}))
    projects = {
        "demo": Project(key="demo", op_id=1, repo="example-owner/demo",
                        visibility="Public", has_test_env=False,
                        test_signal=None, prod_signal=None),
    }
    items = {
        1: Item(id=1, project="demo", type="Epic", status="Open",
                 status_since="2026-09-20T12:00:00Z",
                 est_cost=None, actual_cost=None),
        2: Item(id=2, project="demo", type="Feature", status="Building",
                 status_since="2026-09-20T12:00:00Z", parent_id=1,
                 est_cost=None, actual_cost=None),
        5: Item(**task_kwargs),
    }
    return World(now="2026-09-24T12:00:00Z", projects=projects, items=items,
                 pull_requests={})


def by_key(changes, key, field):
    return [c for c in changes
            if c.key == str(key) and c.field == field]


class EstimateTests(unittest.TestCase):
    def test_estimate_set_for_sized_assigned_task(self):
        changes = costs(make_world(), PRICES, ESTIMATES, actuals={})
        est = by_key(changes, 5, "est_cost")
        self.assertEqual(len(est), 1)
        self.assertAlmostEqual(est[0].new, 100000 / 1e6 * 3.0 + 20000 / 1e6 * 15.0)
        self.assertEqual(est[0].rule, "costs")

    def test_estimate_matches_no_change(self):
        world = make_world(task={"est_cost": 0.6})
        changes = costs(world, PRICES, ESTIMATES, actuals={})
        self.assertEqual(by_key(changes, 5, "est_cost"), [])

    def test_unassigned_or_unsized_task_gets_no_estimate(self):
        world = make_world(task={"assignee": None})
        self.assertEqual(by_key(costs(world, PRICES, ESTIMATES, actuals={}),
                                5, "est_cost"), [])
        world = make_world(task={"size": None})
        self.assertEqual(by_key(costs(world, PRICES, ESTIMATES, actuals={}),
                                5, "est_cost"), [])

    def test_unknown_assignee_gets_no_estimate(self):
        world = make_world(task={"assignee": "nobody"})
        self.assertEqual(by_key(costs(world, PRICES, ESTIMATES, actuals={}),
                                5, "est_cost"), [])


class ActualTests(unittest.TestCase):
    ACTUALS = {("task", 5): {"tokens": 1700, "cost": 0.5}}

    def test_actual_written_in_progress(self):
        changes = costs(make_world(), PRICES, ESTIMATES,
                        actuals=self.ACTUALS)
        tokens = by_key(changes, 5, "actual_tokens")
        cost = by_key(changes, 5, "actual_cost")
        self.assertEqual([(t.new, c.new) for t, c in zip(tokens, cost)],
                         [(1700, 0.5)])

    def test_actual_written_once_more_after_merged(self):
        world = make_world(task={"status": "Merged"})
        changes = costs(world, PRICES, ESTIMATES, actuals=self.ACTUALS)
        self.assertEqual(len(by_key(changes, 5, "actual_tokens")), 1)

    def test_actual_not_recomputed_when_done(self):
        world = make_world(task={"status": "Ready"})
        changes = costs(world, PRICES, ESTIMATES, actuals=self.ACTUALS)
        self.assertEqual(by_key(changes, 5, "actual_tokens"), [])

    def test_unknown_actual_stays_empty(self):
        changes = costs(make_world(), PRICES, ESTIMATES, actuals={})
        self.assertEqual(by_key(changes, 5, "actual_tokens"), [])
        self.assertEqual(by_key(changes, 5, "actual_cost"), [])

    def test_ccusage_failure_still_estimates(self):
        changes = costs(make_world(), PRICES, ESTIMATES, actuals=None)
        self.assertEqual(len(by_key(changes, 5, "est_cost")), 1)
        self.assertEqual(by_key(changes, 5, "actual_tokens"), [])

    def test_no_comments_on_cost_changes(self):
        # Screen fields: the engine only comments status changes; cost
        # changes must never carry a comment trigger. Costs use custom
        # fields exclusively.
        changes = costs(make_world(), PRICES, ESTIMATES,
                        actuals=self.ACTUALS)
        self.assertTrue(changes)
        for change in changes:
            self.assertNotEqual(change.field, "status")


class RollupTests(unittest.TestCase):
    def test_feature_sums_tasks(self):
        world = make_world(task={"est_cost": 0.6, "actual_cost": 0.5,
                                  "actual_tokens": 1700})
        changes = costs(world, PRICES, ESTIMATES, actuals={})
        est = by_key(changes, 2, "est_cost")
        act = by_key(changes, 2, "actual_cost")
        self.assertEqual([e.new for e in est], [0.6])
        self.assertEqual([a.new for a in act], [0.5])

    def test_epic_sums_features(self):
        world = make_world(task={"est_cost": 0.6, "actual_cost": 0.5,
                                  "actual_tokens": 1700})
        changes = costs(world, PRICES, ESTIMATES, actuals={})
        self.assertEqual([e.new for e in by_key(changes, 1, "est_cost")],
                         [0.6])
        self.assertEqual([a.new for a in by_key(changes, 1, "actual_cost")],
                         [0.5])

    def test_unknown_actuals_excluded_from_rollup(self):
        changes = costs(make_world(), PRICES, ESTIMATES, actuals={})
        self.assertEqual(by_key(changes, 2, "actual_cost"), [])
        self.assertEqual(by_key(changes, 1, "actual_cost"), [])

    def test_unpriceable_session_clears_a_stale_actual(self):
        # Codex TH.R F3: an item that had a known actual and then gains a
        # session that can't be priced must not keep showing the old,
        # now-partial number.
        world = make_world(task={"actual_cost": 0.5, "actual_tokens": 1700})
        world.items[2] = world.items[2].__class__(
            **dict(world.items[2].__dict__, actual_cost=0.5))
        world.items[1] = world.items[1].__class__(
            **dict(world.items[1].__dict__, actual_cost=0.5))
        changes = costs(world, PRICES, ESTIMATES,
                        actuals={("task", 5): {"unknown": True}})
        for key, field in ((5, "actual_tokens"), (5, "actual_cost"),
                           (2, "actual_cost"), (1, "actual_cost")):
            self.assertEqual([c.new for c in by_key(changes, key, field)],
                             [None], (key, field))

    def test_unknown_clears_a_blocked_task_too(self):
        # TH.23 (Codex N1): a Blocked task still used tokens; an explicit
        # unknown clears its old number whatever the status.
        world = make_world(task={"status": "Blocked", "actual_cost": 0.5,
                                  "actual_tokens": 1700})
        changes = costs(world, PRICES, ESTIMATES,
                        actuals={("task", 5): {"unknown": True}})
        self.assertEqual([c.new for c in by_key(changes, 5, "actual_cost")], [None])
        self.assertEqual([c.new for c in by_key(changes, 5, "actual_tokens")], [None])

    def test_failed_collection_never_rebuilds_a_partial_rollup(self):
        # TH.23 (Codex N2): cycle 1 clears an unpriceable task and its
        # parents; cycle 2's collection fails (actuals=None). The feature
        # must not be repopulated from the sibling that stayed priced.
        from dataclasses import replace

        world = make_world(task={"actual_cost": None, "actual_tokens": None})
        world.items[6] = replace(world.items[5], id=6, actual_cost=1.0,
                                 actual_tokens=900, status="Merged")
        changes = costs(world, PRICES, ESTIMATES, actuals=None)
        self.assertEqual(by_key(changes, 2, "actual_cost"), [])
        self.assertEqual(by_key(changes, 1, "actual_cost"), [])
        self.assertTrue(by_key(changes, 5, "est_cost"))   # estimates still run

    def test_unknown_test_run_makes_the_feature_unknown(self):
        world = make_world(task={"actual_cost": 0.5, "actual_tokens": 1700})
        changes = costs(world, PRICES, ESTIMATES,
                        actuals={("task", 5): {"tokens": 1700, "cost": 0.5},
                                 ("feature", 2): {"unknown": True}})
        self.assertEqual(by_key(changes, 2, "actual_cost"), [])  # was None
        self.assertEqual(by_key(changes, 1, "actual_cost"), [])

    def test_idempotent_via_drop_noops(self):
        from opl.conductor.engine import drop_noops
        from opl.conductor.state import apply_changes

        world = make_world()
        first = costs(world, PRICES, ESTIMATES,
                      actuals={("task", 5): {"tokens": 1700, "cost": 0.5}})
        world2 = apply_changes(world, first)
        second = costs(world2, PRICES, ESTIMATES,
                       actuals={("task", 5): {"tokens": 1700, "cost": 0.5}})
        kept, _dropped = drop_noops(second, world2)
        self.assertEqual(kept, [])


class FeatureActualTests(unittest.TestCase):
    def test_test_run_cost_books_at_feature(self):
        world = make_world()
        changes = costs(world, PRICES, ESTIMATES,
                        actuals={("feature", 2): {"tokens": 500,
                                                  "cost": 0.25}})
        self.assertEqual([a.new for a in by_key(changes, 2, "actual_cost")],
                         [0.25])
        self.assertEqual([a.new for a in by_key(changes, 1, "actual_cost")],
                         [0.25])
        self.assertEqual(by_key(changes, 5, "actual_cost"), [])

    def test_feature_sums_test_cost_and_tasks(self):
        world = make_world(task={"est_cost": 0.6, "actual_cost": 0.5,
                                  "actual_tokens": 1700})
        changes = costs(world, PRICES, ESTIMATES,
                        actuals={("feature", 2): {"tokens": 500,
                                                  "cost": 0.25}})
        self.assertEqual([a.new for a in by_key(changes, 2, "actual_cost")],
                         [0.75])
        self.assertEqual([a.new for a in by_key(changes, 1, "actual_cost")],
                         [0.75])


class EngineTests(unittest.TestCase):
    def test_costs_flow_with_other_rules(self):
        import os

        from opl.conductor.engine import run_once
        from opl.model import load

        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        model = load(os.path.join(repo, "config", "pm-model.toml"))
        # Task 7 violates nesting (parent is not a Feature): enforce fires
        # alongside the costs estimate; neither drops the other.
        world = make_world()
        import dataclasses

        items = dict(world.items)
        items[7] = dataclasses.replace(items[5], id=7, parent_id=5)
        world = dataclasses.replace(world, items=items)
        mine = costs(world, PRICES, ESTIMATES, actuals=None)
        self.assertTrue(mine)
        changes = run_once(world, model, costs=mine)
        rules = {c.rule for c in changes}
        self.assertIn("enforce", rules)
        self.assertIn("costs", rules)


if __name__ == "__main__":
    unittest.main(verbosity=2)
