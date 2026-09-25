#!/usr/bin/env python3
"""Tests for collect_openproject against documented v3 HAL shapes (TH.7).

Schemas are per project/type with fields at the root; list and user values
sit under `_links`; the work-package listing is paged; journal details are
formattables. TH.7a swaps these for sanitised live samples.
"""

import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs

from opl.conductor.collect import collect_openproject
from opl.model import User
from opl.openproject import ApiError, Client
from opl.settings import Conductor, OpenProject, Project as SettingsProject, Runner, Settings
from tests.fakes.http_fake import FakeServer

BASE_SETTINGS = {
    "openproject": OpenProject("http://127.0.0.1:8080", "OPL_TOKEN_ADMIN", "admin"),
    "tokens": {},
    "github_token_env": "OPL_GITHUB_TOKEN",
    "users_email_domain": "example.invalid",
    "conductor": Conductor(False, 60, "/tmp/opl-state"),
    "runner": Runner((), 1, {}),
}


class _Model:
    users = (User("claude", "Claude", "Model", "all"),
             User("codex", "Codex", "Model", "all"),
             User("spark", "Spark", "Model", "public"))


def make_settings():
    from opl.settings import Signal as SettingsSignal

    return Settings(
        **BASE_SETTINGS,
        projects=(
            SettingsProject("demo", "Demo", "example-owner/demo", "Public",
                            True, "http://127.0.0.1:3001",
                            SettingsSignal("workflow", "deploy-test"),
                            SettingsSignal("environment", "production")),
        ),
    )


def schema(type_id):
    """Per project/type schema: field definitions at the root."""
    body = {
        "_type": "Schema",
        "subject": {"type": "String", "name": "Subject"},
        "customField7": {
            "type": "CustomOption", "name": "Risk", "location": "_links",
            "_links": {"allowedValues": [
                {"href": "/api/v3/custom_options/71", "title": "Low"},
                {"href": "/api/v3/custom_options/73", "title": "High"}]}},
        "customField8": {"type": "User", "name": "Reviewer", "location": "_links"},
        "customField9": {"type": "Boolean", "name": "Merge OK"},
        "customField10": {"type": "Float", "name": "Est. cost"},
        "customField11": {"type": "[]CustomOption", "name": "Models",
                          "location": "_links"},
        "customField12": {"type": "Link", "name": "PR link"},
        "customField13": {"type": "CustomOption", "name": "Review result",
                          "location": "_links"},
        "_links": {"self": {"href": "/api/v3/work_packages/schemas/1-%d" % type_id}},
    }
    return body


def wp(wid, subject, type_id, status_id, project_id, parent_id=None,
       updated="2026-09-20T12:00:00Z", created=None, extra=None, links=None,
       assignee=None):
    all_links = {
        "self": {"href": "/api/v3/work_packages/%d" % wid, "title": subject},
        "type": {"href": "/api/v3/types/%d" % type_id},
        "status": {"href": "/api/v3/statuses/%d" % status_id},
        "project": {"href": "/api/v3/projects/%d" % project_id},
        # OpenProject always sends parent; a top-level item has href null.
        "parent": ({"href": "/api/v3/work_packages/%d" % parent_id}
                   if parent_id is not None else {"href": None}),
        "assignee": ({"href": "/api/v3/users/%d" % assignee[0], "title": assignee[1]}
                     if assignee else {"href": None}),
        "customField7": {"href": None},
        "customField8": {"href": None},
        "customField11": [],
        "customField13": {"href": None},
        "activities": {"href": "/api/v3/work_packages/%d/activities" % wid},
    }
    all_links.update(links or {})
    element = {"_type": "WorkPackage", "id": wid, "subject": subject,
               "updatedAt": updated, "lockVersion": 7,
               "customField9": False, "customField10": None,
               "customField12": None, "_links": all_links}
    if created:
        element["createdAt"] = created
    element.update(extra or {})
    return element


SHA_A = "a" * 40
SHA_B = "b" * 40


def activity(created, user_id, details=(), comment=""):
    return {"_type": "Activity::Comment" if comment else "Activity",
            "createdAt": created,
            "comment": {"format": "markdown", "raw": comment,
                        "html": "<p>%s</p>" % comment},
            "details": [{"format": "custom", "raw": d, "html": d} for d in details],
            "_links": {"user": {"href": "/api/v3/users/%d" % user_id}}}


def membership(user_id, role):
    return {"_type": "Membership",
            "_links": {"principal": {"href": "/api/v3/users/%d" % user_id},
                       "roles": [{"href": "/api/v3/roles/%d" % len(role),
                                  "title": role}]}}


def collection(elements, next_href=None):
    links = {"self": {"href": "/api/v3/work_packages"}}
    if next_href:
        links["nextByOffset"] = {"href": next_href}
    return {"_type": "Collection", "total": 5, "count": len(elements),
            "_embedded": {"elements": elements}, "_links": links}


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        base = self.server.base_url
        self.server.add("GET", "/api/v3/types", body={"_embedded": {"elements": [
            {"id": 11, "name": "Epic"}, {"id": 12, "name": "Feature"},
            {"id": 13, "name": "Task"}]}})
        self.server.add("GET", "/api/v3/statuses", body={"_embedded": {"elements": [
            {"id": 21, "name": "Open"}, {"id": 22, "name": "Proposed"},
            {"id": 23, "name": "Draft"}, {"id": 24, "name": "Merged"}]}})
        self.server.add("GET", "/api/v3/users", body={"_embedded": {"elements": [
            {"id": 51, "login": "spark"}, {"id": 52, "login": "codex"}]}})
        for type_id in (11, 12, 13):
            self.server.add("GET", "/api/v3/work_packages/schemas/1-%d" % type_id,
                            body=schema(type_id))
        self.server.add("GET", "/api/v3/projects", body={"_embedded": {"elements": [
            {"id": 1, "identifier": "demo", "name": "Demo",
             "_links": {"self": {"href": base + "/api/v3/projects/1"}}}]}})

        self.page1 = [
            wp(1, "E", 11, 21, 1),
            wp(2, "F", 12, 22, 1, parent_id=1),
            wp(3, "T1", 13, 23, 1, parent_id=2, assignee=(51, "Spark"),
               links={"customField7": {"href": "/api/v3/custom_options/73",
                                       "title": "High"},
                      "customField8": {"href": "/api/v3/users/52",
                                       "title": "Codex"},
                      "customField11": [
                          {"href": "/api/v3/custom_options/81", "title": "Spark"}],
                      "customField13": {"href": "/api/v3/custom_options/91",
                                        "title": "Pass"}},
               extra={"customField9": True, "customField10": 0.42,
                      "customField12": "https://github.com/example-owner/demo/pull/7"}),
        ]
        # Page 2: an unfinished child of feature 2 plus a closed task.
        self.page2 = [
            wp(4, "T2", 13, 23, 1, parent_id=2, updated="2026-09-21T08:00:00Z"),
            wp(5, "T3", 13, 24, 1, parent_id=2, created="2026-09-19T08:00:00Z",
               updated="2026-09-23T08:00:00Z"),
        ]
        self.page_queries = []

        def pages(method, path, query, body, headers):
            params = parse_qs(query)
            self.page_queries.append(params)
            if params.get("offset") == ["2"]:
                return 200, collection(self.page2)
            return 200, collection(
                self.page1, "/api/v3/work_packages?offset=2&pageSize=200")

        self.server.add("GET", "/api/v3/work_packages", handler=pages)
        # Task 3's journal: users 1 = owner, 51 = spark (builder),
        # 52 = codex (the task's reviewer).
        self.journal3 = [
            activity("2026-09-20T12:00:00Z", 51),
            activity("2026-09-22T09:30:00Z", 51,
                     details=["Status changed from Proposed to Draft"]),
            activity("2026-09-23T09:30:00Z", 51, details=["Risk set to High"]),
            activity("2026-09-23T10:00:00Z", 52,
                     details=["Review result set to Pass"],
                     comment="Looks good.\nreviewed: " + SHA_A),
            activity("2026-09-23T11:00:00Z", 1, details=["Merge OK set to Yes"]),
        ]
        self.server.add("GET", "/api/v3/work_packages/3/activities",
                        handler=lambda *a: (200, {"_embedded": {"elements": self.journal3}}))
        self.server.add("GET", "/api/v3/memberships", body={"_embedded": {"elements": [
            membership(1, "Owner"), membership(51, "Model"), membership(52, "Model"),
            membership(60, "Conductor")]}})
        self.server.add("GET", "/api/v3/relations", body={
            "_embedded": {"elements": [
                {"type": "precedes",
                 "_links": {"from": {"href": base + "/api/v3/work_packages/3"},
                            "to": {"href": base + "/api/v3/work_packages/4"}}},
            ]}})
        for wid in (1, 2, 4, 5):
            self.server.add(
                "GET", "/api/v3/work_packages/%d/activities" % wid,
                body={"_embedded": {"elements": []}})
        self.client = Client(base, "admin-token")
        self.now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)

    def collect(self):
        return collect_openproject(self.client, make_settings(), _Model(), self.now)

    def test_maps_epic_feature_tasks(self):
        projects, items = self.collect()
        self.assertEqual(set(projects), {"demo"})
        project = projects["demo"]
        self.assertEqual((project.op_id, project.visibility), (1, "Public"))
        self.assertTrue(project.has_test_env)
        self.assertEqual((project.test_signal.kind, project.test_signal.name),
                         ("workflow", "deploy-test"))
        self.assertFalse(project.at_risk)
        self.assertEqual(items[1].type, "Epic")
        self.assertEqual(items[2].parent_id, 1)
        self.assertEqual(items[3].parent_id, 2)
        self.assertEqual(items[3].assignee, "spark")
        self.assertEqual(items[3].status, "Draft")
        self.assertEqual(items[3].lock_version, 7)
        self.assertEqual(items[4].predecessors, (3,))
        self.assertEqual(items[3].predecessors, ())

    def _approve_feature_for_risk(self):
        self.server.add("GET", "/api/v3/statuses", body=collection([
            {"id": 21, "name": "Open"}, {"id": 22, "name": "Approved"},
            {"id": 23, "name": "Draft"}, {"id": 24, "name": "Merged"}]))
        self.server.add("GET", "/api/v3/work_packages/2/activities",
                        body=collection([activity("2026-09-22T00:00:00Z", 1,
                             details=["Status changed from Proposed to Approved"])]))

    def test_risk_history_collected_with_membership_authority(self):
        self._approve_feature_for_risk()
        self.page1[2]["_links"]["customField7"]["title"] = "Low"
        self.journal3.append(activity("2026-09-24T00:00:00Z", 51,
                              details=["Risk changed from High to Low"]))
        _, items = self.collect()
        self.assertEqual(items[3].risk_highest_since_approval, "High")
        self.assertFalse(items[3].risk_lowered_by_owner)
        self.journal3[-1]["_links"]["user"]["href"] = "/api/v3/users/1"
        _, items = self.collect()
        self.assertTrue(items[3].risk_lowered_by_owner)

    def test_approved_task_without_review_still_requires_readable_journal(self):
        self._approve_feature_for_risk()
        self.server.add("GET", "/api/v3/work_packages/4/activities", status=503,
                        body={"message": "synthetic failure"})
        with self.assertRaises(ApiError):
            self.collect()

    def test_approved_parent_requires_readable_journal(self):
        self._approve_feature_for_risk()
        self.server.add("GET", "/api/v3/work_packages/2/activities", status=503,
                        body={"message": "synthetic failure"})
        with self.assertRaises(ApiError):
            self.collect()

    def test_null_parent_is_no_parent(self):
        _, items = self.collect()
        self.assertIsNone(items[1].parent_id)

    def test_child_on_a_later_page_is_collected(self):
        _, items = self.collect()
        self.assertEqual(set(items), {1, 2, 3, 4, 5})
        self.assertEqual((items[4].parent_id, items[4].status), (2, "Draft"))
        self.assertEqual(items[5].status, "Merged")
        first = self.page_queries[0]
        self.assertIn("project", first["filters"][0])
        self.assertEqual(first["pageSize"], ["200"])

    def test_list_user_and_scalar_fields(self):
        _, items = self.collect()
        task = items[3]
        self.assertEqual(task.risk, "High")
        self.assertEqual(task.reviewer, "codex")
        self.assertEqual(task.models, ("Spark",))
        self.assertTrue(task.merge_ok)
        self.assertEqual(task.est_cost, 0.42)
        self.assertEqual(task.pr_url, "https://github.com/example-owner/demo/pull/7")
        self.assertIsNone(items[4].risk)
        self.assertIsNone(items[4].reviewer)
        self.assertFalse(items[4].merge_ok)
        self.assertEqual(items[4].models, ())

    def test_status_since_from_formattable_journal(self):
        _, items = self.collect()
        self.assertEqual(items[3].status_since,
                         datetime(2026, 9, 22, 9, 30, tzinfo=timezone.utc))
        # No status change journaled: since creation, else last update.
        self.assertEqual(items[5].status_since,
                         datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc))
        self.assertEqual(items[4].status_since,
                         datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc))

    def test_users_without_admin_fall_back_to_model_names(self):
        self.server.add("GET", "/api/v3/users", status=403,
                        body={"_type": "Error", "message": "not allowed"})
        _, items = self.collect()
        self.assertEqual(items[3].assignee, "spark")
        self.assertEqual(items[3].reviewer, "codex")

    def test_unknown_user_keeps_title_but_is_no_model(self):
        self.server.add("GET", "/api/v3/users", status=403,
                        body={"_type": "Error", "message": "not allowed"})
        self.page1[2]["_links"]["assignee"] = {"href": "/api/v3/users/77",
                                               "title": "Spark Imposter"}
        _, items = self.collect()
        self.assertEqual(items[3].assignee, "Spark Imposter")

    def test_paging_failure_raises_so_the_cycle_is_skipped(self):
        def broken(method, path, query, body, headers):
            if "offset=2" in query:
                return 500, {"_type": "Error", "message": "boom"}
            return 200, collection(
                self.page1, "/api/v3/work_packages?offset=2&pageSize=200")

        self.server.add("GET", "/api/v3/work_packages", handler=broken)
        with self.assertRaises(ApiError):
            self.collect()

    def test_approvals_come_from_the_journal(self):
        _, items = self.collect()
        task = items[3]
        self.assertEqual(task.review_result, "Pass")
        self.assertTrue(task.merge_ok_by_owner)
        self.assertTrue(task.review_by_reviewer)
        self.assertEqual(task.reviewed_sha, SHA_A)
        self.assertFalse(items[4].merge_ok_by_owner)
        self.assertIsNone(items[4].reviewed_sha)

    def test_builder_ticking_merge_ok_is_not_the_owner(self):
        self.journal3.append(activity("2026-09-23T12:00:00Z", 51,
                                      details=["Merge OK changed from No to Yes"]))
        _, items = self.collect()
        self.assertFalse(items[3].merge_ok_by_owner)

    def test_builder_setting_review_pass_is_not_the_reviewer(self):
        self.journal3.append(activity("2026-09-23T12:00:00Z", 51,
                                      details=["Review result set to Pass"]))
        _, items = self.collect()
        self.assertFalse(items[3].review_by_reviewer)

    def test_reviewed_sha_only_from_the_reviewer(self):
        self.journal3.append(activity("2026-09-23T12:00:00Z", 51,
                                      comment="reviewed: " + SHA_B))
        _, items = self.collect()
        self.assertEqual(items[3].reviewed_sha, SHA_A)
        self.journal3.append(activity("2026-09-23T13:00:00Z", 52,
                                      comment="reviewed: " + SHA_B.upper()))
        _, items = self.collect()
        self.assertEqual(items[3].reviewed_sha, SHA_B)

    def test_short_or_embedded_sha_is_not_a_review_record(self):
        self.journal3[3] = activity("2026-09-23T10:00:00Z", 52,
                                    details=["Review result set to Pass"],
                                    comment="reviewed: abc1234 and more")
        _, items = self.collect()
        self.assertIsNone(items[3].reviewed_sha)

    def test_unreadable_journal_of_an_approved_task_skips_the_cycle(self):
        self.server.add("GET", "/api/v3/work_packages/3/activities", status=500,
                        body={"_type": "Error", "message": "boom"})
        with self.assertRaises(ApiError):
            self.collect()

    def test_unreadable_journal_otherwise_only_loses_the_timestamp(self):
        self.server.add("GET", "/api/v3/work_packages/4/activities", status=500,
                        body={"_type": "Error", "message": "boom"})
        _, items = self.collect()
        self.assertEqual(items[4].status_since,
                         datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc))

    def test_unreadable_memberships_skip_the_cycle(self):
        self.server.add("GET", "/api/v3/memberships", status=403,
                        body={"_type": "Error", "message": "no"})
        with self.assertRaises(ApiError):
            self.collect()

    def test_missing_project_raises_clear_error(self):
        self.server.add("GET", "/api/v3/projects",
                        body={"_embedded": {"elements": []}})
        with self.assertRaises(ApiError) as ctx:
            self.collect()
        self.assertIn("demo", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
