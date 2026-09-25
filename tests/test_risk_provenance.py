"""TH.15: synthetic journal replay and merge-gate regressions."""

import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

from opl import hal
from opl.conductor import collect
from opl.conductor.rules.enforce import enforce, violations
from opl.conductor.rules.merge import merge
from opl.conductor.state import Item, Project, PullRequest, World
from opl.openproject import ApiError

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
APPROVAL = datetime(2026, 1, 2, tzinfo=timezone.utc)
NOW = datetime(2026, 1, 5, tzinfo=timezone.utc)


def event(day, text, user=7):
    return {"createdAt": "2026-01-%02dT00:00:00Z" % day,
            "details": [{"raw": text}],
            "_links": {"user": {"href": "/api/v3/users/%d" % user}}}


def world(risk="Low", **provenance):
    project = Project("demo", 1, "example/demo", "Public", False, None, None)
    items = {
        1: Item(1, "demo", "Epic", "Open", T0),
        2: Item(2, "demo", "Feature", "Approved", APPROVAL, parent_id=1),
        3: Item(3, "demo", "Task", "In review", T0, parent_id=2,
                assignee="spark", reviewer="codex", size="S", risk=risk,
                review_result="Pass", review_by_reviewer=True,
                reviewed_sha="a" * 40,
                pr_url="https://github.com/example/demo/pull/1", **provenance),
    }
    # TH.5 part 2: a merge needs the reviewed SHA to equal the PR head.
    pr = PullRequest(items[3].pr_url, False, None, True, head_sha="a" * 40,
                     head_repo="example/demo", base_repo="example/demo")
    return World(NOW, {"demo": project}, items, {pr.url: pr})


class RiskTests(unittest.TestCase):
    def replay(self, events, risk="Low"):
        return collect._risk_approval(events, risk, APPROVAL, {9})

    def test_old_value(self):
        self.assertEqual(hal.old_value(event(3, "Risk changed from High to Low"),
                                       "Risk"), "High")
        self.assertIsNone(hal.old_value(event(3, "Risk set to High"), "Risk"))
        self.assertEqual(hal.old_value(event(3, "Risk deleted (High)"), "Risk"),
                         "High")

    def test_builder_lowering_blocks_and_cannot_merge(self):
        w = world(**self.replay([event(3, "Risk changed from High to Low")]))
        self.assertEqual(violations(w)[3], "Risk lowered without the owner")
        self.assertEqual(enforce(w)[0].new, "Blocked")
        self.assertEqual(merge(w), [])

    def test_owner_lowering_allowed(self):
        w = world(**self.replay([event(3, "Risk changed from High to Low", 9)]))
        self.assertNotIn(3, violations(w))
        self.assertEqual(len(merge(w)), 1)

    def test_proposed_feature_is_not_gated(self):
        w = world(risk_highest_since_approval="High")
        w.items[2] = replace(w.items[2], status="Proposed")
        self.assertNotIn(3, violations(w))

    def test_preapproval_lowering_does_not_count(self):
        w = world(**self.replay([event(1, "Risk changed from High to Low")]))
        self.assertNotIn(3, violations(w))
        self.assertEqual(w.items[3].risk_highest_since_approval, "Low")

    def test_raising_to_peak_allowed(self):
        events = [event(3, "Risk changed from High to Low"),
                  event(4, "Risk changed from Low to High")]
        w = world("High", **self.replay(events, "High"))
        self.assertNotIn(3, violations(w))

    def test_partial_raise_does_not_launder_unauthorized_lowering(self):
        events = [event(3, "Risk changed from High to Low"),
                  event(4, "Risk changed from Low to Medium")]
        w = world("Medium", **self.replay(events, "Medium"))
        self.assertIn(3, violations(w))

    def test_owner_permission_is_not_blanket_for_later_lowering(self):
        events = [event(3, "Risk changed from High to Medium", 9),
                  event(4, "Risk changed from Medium to Low", 7)]
        self.assertFalse(self.replay(events)["risk_lowered_by_owner"])

    def test_owner_lowering_then_raise_keeps_permission(self):
        events = [event(3, "Risk changed from High to Low", 9),
                  event(4, "Risk changed from Low to Medium", 7)]
        self.assertTrue(self.replay(events, "Medium")["risk_lowered_by_owner"])

    def test_shuffled_journal_and_no_changes(self):
        events = [event(4, "Risk changed from Medium to Low"),
                  event(3, "Risk changed from High to Medium", 9)]
        self.assertEqual(self.replay(events)["risk_highest_since_approval"], "High")
        self.assertEqual(self.replay([])["risk_highest_since_approval"], "Low")

    def test_unknown_author_is_not_owner(self):
        entry = event(3, "Risk changed from High to Low")
        entry["_links"] = {}
        self.assertFalse(self.replay([entry])["risk_lowered_by_owner"])

    def test_bad_or_inconsistent_history_fails_closed(self):
        for entry in (event(3, "Risk changed from Unknown to Low"),
                      event(3, "Risk changed from High to Medium"),
                      event(3, "Risk changed ???")):
            with self.subTest(entry=entry), self.assertRaises(ApiError):
                self.replay([entry])
        entry = event(3, "Risk changed from High to Low")
        entry["createdAt"] = "invalid"
        with self.assertRaises(ApiError):
            self.replay([entry])

    def test_approval_boundary_does_not_reset_on_building(self):
        entries = [event(2, "Status changed from Proposed to Approved"),
                   event(4, "Status changed from Approved to Building")]
        self.assertEqual(collect._approved_since(entries, {"createdAt": T0.isoformat()}),
                         APPROVAL)

    def test_affected_journal_unreadable_aborts_collection(self):
        w = world()
        records = {2: ({"createdAt": T0.isoformat()}, {9}), 3: ({}, {9})}
        with patch.object(collect, "_journal", side_effect=ApiError(503, "/test", "offline")):
            with self.assertRaises(ApiError):
                collect._collect_risk(object(), w.items, records)

    def test_proposed_journal_does_not_need_strict_fetch(self):
        w = world()
        w.items[2] = replace(w.items[2], status="Proposed")
        with patch.object(collect, "_journal") as journal:
            collect._collect_risk(object(), w.items, {})
        journal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
