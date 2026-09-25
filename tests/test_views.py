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
        self.projects = {}
        self.queries = []
        self.grid = {"id": 3000, "scope": "My page", "widgets": [],
                     "_links": {"self": {"href": self.base + "/api/v3/grids/3000"}}}
        # Per project/type schemas, fields at the root (TH.7).
        self.wp_schema = {
            "_type": "Schema",
            "customField7": {"type": "Boolean", "name": "Needs you"},
            "customField8": {"type": "CustomOption", "name": "Risk"},
            "customField9": {"type": "[]CustomOption", "name": "Models"},
        }
        server.add("GET", "/api/v3/types", body={"_embedded": {"elements": [
            {"id": 11, "name": "Epic"}, {"id": 12, "name": "Feature"},
            {"id": 13, "name": "Task"}]}})
        server.add("GET", "/api/v3/projects", handler=self.get_projects)
        server.add("GET", "/api/v3/users", handler=self.get_users)
        server.add("GET", "/api/v3/queries", handler=self.get_queries)
        server.add("POST", "/api/v3/queries", handler=self.post_queries)
        server.add("GET", "/api/v3/grids",
                   body={"_embedded": {"elements": [self.grid]}})
        server.add("POST", "/api/v3/grids/3000/widgets", handler=self.post_widget)

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
        element = dict(body)
        element["id"] = self.next_id
        element["_links"] = {"self": {"href": "%s/api/v3/queries/%d" % (self.base, self.next_id)}}
        if "project" in body:
            element["_links"]["project"] = body["project"]
        self.queries.append(element)
        return 201, element

    def patch_query(self, method, path, query, body, headers):
        qid = int(path.rsplit("/", 1)[-1])
        for stored in self.queries:
            if stored["id"] == qid:
                stored.update(body)
                return 200, stored
        return 404, {}

    def post_widget(self, method, path, query, body, headers):
        widget = {"query": body["query"]}
        self.grid["widgets"].append(widget)
        return 201, widget

    def register_patches(self):
        for stored in self.queries:
            self.server.add("PATCH", "/api/v3/queries/%d" % stored["id"],
                            handler=self.patch_query)


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
        self.assertNotIn("project", body)
        self.assertEqual(body["filters"],
                         [{"customField7": {"operator": "=", "values": ["true"]}}])
        self.assertEqual(body["orders"], [["priority", "desc"]])
        self.assertEqual(body["columns"], ["id", "subject", "customField8"])
        self.assertTrue(body["starred"])
        self.assertTrue(body["public"])

    def test_each_project_copies_and_roadmap_shape(self):
        apply_views(self.client, self.model, self.settings)
        posts = [r for r in self.server.writes()
                 if r["method"] == "POST" and r["path"] == "/api/v3/queries"]
        pipes = [r for r in posts if r["body"]["name"] == "Pipe"]
        self.assertEqual(len(pipes), 2)
        hrefs = sorted(r["body"]["project"]["href"] for r in pipes)
        self.assertTrue(hrefs[0].endswith("/101"))
        self.assertTrue(hrefs[1].endswith("/102"))
        self.assertEqual(pipes[0]["body"]["groupBy"], "status")
        self.assertNotIn("timelineVisible", pipes[0]["body"])

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
        widgets = [r for r in self.server.writes() if r["path"].endswith("/widgets")]
        self.assertEqual(len(widgets), 1)
        needs_me = [q for q in self.world.queries if q["name"] == "Needs me"][0]
        self.assertTrue(widgets[0]["body"]["query"]["href"].endswith("/%d" % needs_me["id"]))
        writes_before = len(self.server.writes())
        apply_views(self.client, self.model, self.settings)
        widgets2 = [r for r in self.server.writes()[writes_before:]
                    if r["path"].endswith("/widgets")]
        self.assertEqual(widgets2, [])

    def test_dry_run_matches_live(self):
        dry_actions = apply_views(self.client, self.model, self.settings, dry_run=True)
        self.assertEqual(self.server.writes(), [])
        live_actions = apply_views(self.client, self.model, self.settings, dry_run=False)
        self.assertTrue(self.server.writes())
        self.assertEqual(dry_actions, live_actions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
