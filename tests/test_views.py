#!/usr/bin/env python3
"""Tests for opl.configure.views against a stateful FakeServer."""

import json
import unittest
from urllib.parse import parse_qs

from opl.configure.views import apply_views
from opl.model import load as load_model
from opl.openproject import Client
from opl.settings import Conductor, OpenProject, Project, Runner, Settings
from tests.fakes.http_fake import FakeServer

MODEL_TOML = """
[progress]
mode = "status"

[[status]]
name = "Open"
closed = false
done_ratio = 0

[[type]]
name = "Feature"
statuses = ["Open"]
default_status = "Open"

[[role]]
name = "Owner"
permissions = "all"

[[field]]
name = "Needs you"
on = ["Feature"]
format = "bool"
[[field]]
name = "Risk"
on = ["Feature"]
format = "list"
values = ["Low", "High"]

[[view]]
name = "Needs me"
scope = "global"
filters = [{ field = "Needs you", op = "=", values = ["true"] }]
sort = [["priority", "desc"]]
columns = ["id", "subject", "Risk"]
starred = true
my_page = true

[[view]]
name = "Pipe"
scope = "each-project"
filters = [{ field = "Risk", op = "=", values = ["Low"] }]
sort = [["priority", "desc"]]
columns = ["id", "subject"]
group_by = "status"
"""


def _filters(query):
    try:
        return json.loads(parse_qs(query).get("filters", ["[]"])[0])
    except ValueError:
        return []


def make_settings():
    return Settings(
        openproject=OpenProject("http://127.0.0.1:8080", "OPL_TOKEN_ADMIN", "admin"),
        tokens={},
        github_token_env="OPL_GITHUB_TOKEN",
        users_email_domain="example.invalid",
        conductor=Conductor(False, 60, "/tmp/opl-state"),
        runner=Runner((), 1, {}),
        projects=(
            Project("demo-public", "Demo public project", "example-owner/demo-public",
                    "Public", False, "", None, None),
            Project("demo-private", "Demo private project", "example-owner/demo-private",
                    "Private", False, "", None, None),
        ),
    )


class FakeViewsWorld:
    def __init__(self, server):
        self.server = server
        self.base = server.base_url
        self.next_id = 2000
        self.widget_seq = 7000
        self.projects = {}
        self.queries = []
        # Live v17 shape: scope is a link, the page starts with 1 row x 2.
        self.grid = {"id": 3000, "rowCount": 1, "columnCount": 2, "widgets": [],
                     "_links": {"self": {"href": self.base + "/api/v3/grids/3000"},
                                "scope": {"href": "/my/page"}}}
        # Per project/type schemas, fields at the root (TH.7).
        self.wp_schema = {
            "_type": "Schema",
            "customField7": {"type": "Boolean", "name": "Needs you"},
            "customField8": {"type": "CustomOption", "name": "Risk"},
            "customField9": {"type": "[]CustomOption", "name": "Models"},
        }
        server.add("GET", "/api/v3/types", body={"_embedded": {"elements": [
            {"id": 11, "name": "Epic",
             "_links": {"self": {"href": self.base + "/api/v3/types/11"}}},
            {"id": 12, "name": "Feature",
             "_links": {"self": {"href": self.base + "/api/v3/types/12"}}},
            {"id": 13, "name": "Task",
             "_links": {"self": {"href": self.base + "/api/v3/types/13"}}}]}})
        server.add("GET", "/api/v3/statuses", body={"_embedded": {"elements": [
            {"id": 21, "name": "Open",
             "_links": {"self": {"href": self.base + "/api/v3/statuses/21"}}}]}})
        server.add("GET", "/api/v3/versions", body={"_embedded": {"elements": []}})
        server.add("GET", "/api/v3/projects", handler=self.get_projects)
        server.add("GET", "/api/v3/users", handler=self.get_users)
        server.add("GET", "/api/v3/queries", handler=self.get_queries)
        server.add("POST", "/api/v3/queries", handler=self.post_queries)
        server.add("GET", "/api/v3/grids",
                   body={"_embedded": {"elements": [self.grid]}})
        server.add("PATCH", "/api/v3/grids/3000", handler=self.patch_grid)

    def seed_project(self, name, pid):
        for tid in (11, 12, 13):
            self.server.add("GET", "/api/v3/work_packages/schemas/%d-%d" % (pid, tid),
                            body=self.wp_schema)
        self.projects[name] = {"id": pid, "name": name,
                               "_links": {"self": {"href": "%s/api/v3/projects/%d" % (self.base, pid)}}}

    def get_projects(self, method, path, query, body, headers):
        wanted = [f["name"]["values"] for f in _filters(query) if "name" in f]
        elements = [p for p in self.projects.values() if not wanted or p["name"] in wanted[0]]
        return 200, {"_embedded": {"elements": elements}}

    def get_users(self, method, path, query, body, headers):
        admin = {"id": 50, "login": "admin",
                 "_links": {"self": {"href": self.base + "/api/v3/users/50"}}}
        return 200, {"_embedded": {"elements": [admin]}}

    def get_queries(self, method, path, query, body, headers):
        return 200, {"_embedded": {"elements": list(self.queries)}}

    def post_queries(self, method, path, query, body, headers):
        self.next_id += 1
        element = {k: v for k, v in body.items() if k != "_links"}
        element["id"] = self.next_id
        element["_links"] = dict((body.get("_links") or {}))
        element["_links"]["self"] = {
            "href": "%s/api/v3/queries/%d" % (self.base, self.next_id)}
        # Legacy top-level project link (pre-HAL) still mirrored for safety.
        if "project" in body and "project" not in element["_links"]:
            element["_links"]["project"] = body["project"]
        self.queries.append(element)
        self.register_query(element["id"])
        return 201, element

    def get_single_query(self, method, path, query, body, headers):
        try:
            qid = int(path.rsplit("/", 1)[-1])
        except ValueError:
            return 404, {}
        for stored in self.queries:
            if stored.get("id") == qid:
                return 200, stored
        return 404, {"_type": "Error", "message": "not found"}

    def register_query(self, qid):
        self.server.add("GET", "/api/v3/queries/%d" % qid,
                        handler=self.get_single_query)
        self.server.add("PATCH", "/api/v3/queries/%d" % qid,
                        handler=self.patch_query)

    def seed_query(self, name, qid, hidden=False):
        element = {"id": qid, "name": name, "public": not hidden,
                   "hidden": hidden,
                   "_links": {"self": {"href": "%s/api/v3/queries/%d"
                                       % (self.base, qid)}}}
        self.queries.append(element)
        self.register_query(qid)
        self.next_id = max(self.next_id, qid)
        return element

    def seed_default_widgets(self):
        """Owner's two default work-package widgets owning their queries."""
        self.seed_query("Work packages assigned to me", 9001, hidden=True)
        self.seed_query("Work packages created by me", 9002, hidden=True)
        self.grid["widgets"] = [
            {"id": 6101, "identifier": "work_packages_assigned",
             "startRow": 1, "endRow": 2, "startColumn": 1, "endColumn": 2,
             "options": {"name": "Work packages assigned to me",
                         "queryId": "9001"}},
            {"id": 6102, "identifier": "work_packages_created",
             "startRow": 1, "endRow": 2, "startColumn": 2, "endColumn": 3,
             "options": {"name": "Work packages created by me",
                         "queryId": "9002"}},
        ]
        self.grid["rowCount"] = 1
        self.grid["columnCount"] = 2

    def patch_query(self, method, path, query, body, headers):
        try:
            qid = int(path.rsplit("/", 1)[-1])
        except ValueError:
            return 404, {}
        for stored in self.queries:
            if stored.get("id") == qid:
                for key, value in body.items():
                    if key == "_links":
                        stored.setdefault("_links", {}).update(value or {})
                    else:
                        stored[key] = value
                return 200, stored
        return 404, {}

    def patch_grid(self, method, path, query, body, headers):
        # Widgets change only by PATCHing the whole grid (no sub-resource).
        # Live OpenProject replaces id-less widgets: any pre-existing
        # widget whose id is absent from the PATCH is destroyed, and the
        # query it owned is destroyed with it. Id-less incoming widgets
        # arrive as new widgets (fresh ids) still pointing at deleted ids.
        old_widgets = list(self.grid.get("widgets", []))
        old_qid_by_wid = {
            w.get("id"): (w.get("options") or {}).get("queryId")
            for w in old_widgets if w.get("id") is not None
        }
        incoming = list(body.get("widgets", []))
        incoming_ids = {w.get("id") for w in incoming
                        if w.get("id") is not None}
        for wid, qid in old_qid_by_wid.items():
            if wid not in incoming_ids and qid is not None:
                self.queries = [q for q in self.queries
                                if str(q.get("id")) != str(qid)]
        for widget in incoming:
            if widget.get("id") is None:
                self.widget_seq += 1
                widget["id"] = self.widget_seq
        self.grid.update({k: body[k] for k in ("rowCount", "columnCount", "widgets")
                          if k in body})
        return 200, self.grid

    def register_patches(self):
        for stored in self.queries:
            self.register_query(stored["id"])


class ViewsTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.server = FakeServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.world = FakeViewsWorld(self.server)
        self.world.seed_project("Demo public project", 101)
        self.world.seed_project("Demo private project", 102)
        self.client = Client(self.server.base_url, "admin-token")
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write(MODEL_TOML)
            path = fh.name
        try:
            self.model = load_model(path)
        finally:
            import os

            os.unlink(path)
        self.settings = make_settings()

    def test_needs_me_payload(self):
        apply_views(self.client, self.model, self.settings)
        posts = [r for r in self.server.writes()
                 if r["method"] == "POST" and r["path"] == "/api/v3/queries"]
        mine = [r for r in posts if r["body"]["name"] == "Needs me"]
        self.assertEqual(len(mine), 1)
        body = mine[0]["body"]
        # Global queries carry no project link (neither top-level nor HAL).
        self.assertNotIn("project", body)
        self.assertNotIn("project", body.get("_links", {}))
        # HAL link form: custom boolean "Needs you" true -> scalar "t".
        self.assertEqual(len(body["filters"]), 1)
        filt = body["filters"][0]
        self.assertEqual(filt["_links"]["filter"]["href"],
                         "/api/v3/queries/filters/customField7")
        self.assertEqual(filt["_links"]["operator"]["href"],
                         "/api/v3/queries/operators/%3D")
        self.assertEqual(filt["values"], ["t"])
        self.assertNotIn("values", filt["_links"])
        # Columns and sort live under _links as hrefs.
        self.assertNotIn("orders", body)
        self.assertNotIn("columns", body)
        self.assertEqual(
            [c["href"] for c in body["_links"]["columns"]],
            ["/api/v3/queries/columns/id",
             "/api/v3/queries/columns/subject",
             "/api/v3/queries/columns/customField8"])
        self.assertEqual(
            [s["href"] for s in body["_links"]["sortBy"]],
            ["/api/v3/queries/sort_bys/priority-desc"])
        self.assertTrue(body["starred"])
        self.assertTrue(body["public"])
        self.assertFalse(body["timelineVisible"])

    def test_each_project_copies_and_roadmap_shape(self):
        apply_views(self.client, self.model, self.settings)
        posts = [r for r in self.server.writes()
                 if r["method"] == "POST" and r["path"] == "/api/v3/queries"]
        pipes = [r for r in posts if r["body"]["name"] == "Pipe"]
        self.assertEqual(len(pipes), 2)
        hrefs = sorted(r["body"]["_links"]["project"]["href"] for r in pipes)
        self.assertTrue(hrefs[0].endswith("/101"))
        self.assertTrue(hrefs[1].endswith("/102"))
        self.assertEqual(pipes[0]["body"]["_links"]["groupBy"]["href"],
                         "/api/v3/queries/group_bys/status")
        # timelineVisible is always present (false when the view has none).
        self.assertFalse(pipes[0]["body"]["timelineVisible"])

    def test_type_filter_resolves_to_link_values(self):
        # Live v17: link-valued filter values (type) travel as _links.values
        # hrefs resolved by name; the open-status shorthand uses operator `o`.
        from opl.model import load as load_model
        import tempfile, os

        toml = MODEL_TOML.replace(
            'filters = [{ field = "Risk", op = "=", values = ["Low"] }]',
            'filters = [{ field = "type", op = "=", values = ["Feature"] }]')
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write(toml)
            path = fh.name
        try:
            model = load_model(path)
        finally:
            os.unlink(path)
        apply_views(self.client, model, self.settings)
        posts = [r for r in self.server.writes()
                 if r["method"] == "POST" and r["path"] == "/api/v3/queries"]
        pipes = [r for r in posts if r["body"]["name"] == "Pipe"]
        self.assertTrue(pipes)
        filt = pipes[0]["body"]["filters"][0]
        self.assertEqual(filt["_links"]["filter"]["href"],
                         "/api/v3/queries/filters/type")
        self.assertEqual(filt["_links"]["operator"]["href"],
                         "/api/v3/queries/operators/%3D")
        self.assertEqual(len(filt["_links"]["values"]), 1)
        self.assertTrue(
            filt["_links"]["values"][0]["href"].endswith("/12"))

    def test_column_mapping_for_progress_version_parent(self):
        # T5.1 S1: model column names that differ from query column ids go
        # through one small table; progress/version mappings are UNVERIFIED
        # live (lead to confirm).
        from opl.configure.views import COLUMN_MAP, _query_body

        self.assertEqual(COLUMN_MAP["progress"], "percentageDone")
        self.assertIn("version", COLUMN_MAP)
        self.assertIn("parent", COLUMN_MAP)
        body = _query_body(
            type("V", (), {"name": "X", "filters": (), "sort": (),
                           "columns": ("progress", "version", "parent"),
                           "group_by": "", "timeline": False,
                           "starred": False})(),
            {}, {})
        hrefs = [c["href"] for c in body["_links"]["columns"]]
        self.assertEqual(hrefs, [
            "/api/v3/queries/columns/percentageDone",
            "/api/v3/queries/columns/%s" % COLUMN_MAP["version"],
            "/api/v3/queries/columns/%s" % COLUMN_MAP["parent"],
        ])

    def test_second_run_is_silent(self):
        self.world.register_patches()
        actions1 = apply_views(self.client, self.model, self.settings)
        self.assertTrue(actions1)
        self.world.register_patches()
        writes_before = len(self.server.writes())
        actions2 = apply_views(self.client, self.model, self.settings)
        self.assertEqual(actions2, [])
        self.assertEqual(len(self.server.writes()), writes_before)

    def test_my_page_pin_once(self):
        apply_views(self.client, self.model, self.settings)
        pins = [r for r in self.server.writes() if r["path"].endswith("/grids/3000")]
        self.assertEqual(len(pins), 1)
        needs_me = [q for q in self.world.queries if q["name"] == "Needs me"][0]
        added = pins[0]["body"]["widgets"][-1]
        self.assertEqual(added["identifier"], "work_packages_table")
        self.assertEqual(added["options"]["queryId"], str(needs_me["id"]))
        self.assertEqual((added["startRow"], added["endRow"]), (2, 3))
        self.assertEqual(pins[0]["body"]["rowCount"], 2)
        writes_before = len(self.server.writes())
        apply_views(self.client, self.model, self.settings)
        pins2 = [r for r in self.server.writes()[writes_before:]
                 if r["path"].endswith("/grids/3000")]
        self.assertEqual(pins2, [])

    def test_pin_keeps_widget_ids_and_views(self):
        # Issue #33: pinning must update widgets in place (ids kept) so
        # the default widgets' queries survive.
        self.world.seed_default_widgets()
        apply_views(self.client, self.model, self.settings)
        pins = [r for r in self.server.writes() if r["path"].endswith("/grids/3000")]
        self.assertEqual(len(pins), 1)
        sent = pins[0]["body"]["widgets"]
        self.assertEqual(len(sent), 3)
        # The two pre-existing widgets keep their ids.
        self.assertEqual(sent[0].get("id"), 6101)
        self.assertEqual(sent[1].get("id"), 6102)
        # Read-back: every work-package widget's query still resolves.
        for widget in self.world.grid["widgets"]:
            if not str(widget.get("identifier", "")).startswith("work_packages_"):
                continue
            qid = (widget.get("options") or {}).get("queryId")
            self.assertIsNotNone(qid)
            got = self.client.get("/api/v3/queries/%s" % qid)
            self.assertEqual(str(got.get("id")), str(qid))
        self.assertEqual(len(self.world.queries),
                         len([q for q in self.world.queries]))

    def test_fake_models_replace_on_missing_id(self):
        # The fake grid models OpenProject: a PATCH whose widgets lack
        # `id` destroys the old widgets and the queries they owned.
        self.world.seed_default_widgets()
        self.assertEqual(len(self.world.queries), 2)
        self.client.patch("/api/v3/grids/3000", {
            "rowCount": 1, "columnCount": 2,
            "widgets": [
                {"identifier": w["identifier"], "startRow": w["startRow"],
                 "endRow": w["endRow"], "startColumn": w["startColumn"],
                 "endColumn": w["endColumn"], "options": dict(w["options"])}
                for w in self.world.grid["widgets"]
            ],
        })
        # Old code's shape (no ids) deletes the owned queries.
        self.assertEqual(self.world.queries, [])
        for dead in ("9001", "9002"):
            with self.assertRaises(Exception):
                self.client.get("/api/v3/queries/%s" % dead)

    def test_pin_read_back_fails_when_query_gone(self):
        from opl.openproject import ApiError

        self.world.seed_default_widgets()
        # Simulate a page the old code already broke: one query is gone.
        self.world.queries = [q for q in self.world.queries
                              if str(q.get("id")) != "9001"]
        with self.assertRaises(ApiError) as ctx:
            apply_views(self.client, self.model, self.settings)
        self.assertIn("9001", str(ctx.exception))

    def test_repair_my_page_recreates_hidden_query(self):
        self.world.seed_default_widgets()
        self.world.queries = [q for q in self.world.queries
                              if str(q.get("id")) != "9001"]
        actions = apply_views(self.client, self.model, self.settings,
                              repair_my_page=True)
        self.assertTrue(any(a.startswith("repair My page widget") for a in actions))
        posts = [r for r in self.server.writes()
                 if r["method"] == "POST" and r["path"] == "/api/v3/queries"]
        repairs = [r for r in posts if r["body"].get("hidden") is True]
        self.assertEqual(len(repairs), 1)
        body = repairs[0]["body"]
        self.assertFalse(body.get("public"))
        # Reuses the shared HAL link form: status open + user link value.
        filter_hrefs = [f["_links"]["filter"]["href"] for f in body["filters"]]
        self.assertIn("/api/v3/queries/filters/status", filter_hrefs)
        self.assertTrue(any(
            href in ("/api/v3/queries/filters/assignee",
                     "/api/v3/queries/filters/author")
            for href in filter_hrefs))
        # The repaired widget points at the new query and everything resolves.
        for widget in self.world.grid["widgets"]:
            if not str(widget.get("identifier", "")).startswith("work_packages_"):
                continue
            qid = (widget.get("options") or {}).get("queryId")
            got = self.client.get("/api/v3/queries/%s" % qid)
            self.assertEqual(str(got.get("id")), str(qid))

    def test_dry_run_matches_live(self):
        dry_actions = apply_views(self.client, self.model, self.settings, dry_run=True)
        self.assertEqual(self.server.writes(), [])
        live_actions = apply_views(self.client, self.model, self.settings, dry_run=False)
        self.assertTrue(self.server.writes())
        self.assertEqual(dry_actions, live_actions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
