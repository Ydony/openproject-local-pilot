#!/usr/bin/env python3
"""Tests for opl.configure.apply_api against a stateful FakeServer.

The fake world behaves like OpenProject: POSTs change what later GETs
return, so calling apply_api twice proves real idempotency (zero writes on
the second run), not just canned silence.
"""

import json
import unittest
from urllib.parse import parse_qs

from opl.configure.apply_api import apply_api
from opl.model import Field, Model, Role, Status, Type, User, View, Workflow
from opl.openproject import Client
from opl.settings import Conductor, OpenProject, Project, Runner, Settings
from tests.fakes.http_fake import FakeServer


def make_model():
    statuses = (
        Status("Open", False, 0),
        Status("Approved", False, 0),
        Status("Draft", False, 0),
    )
    return Model(
        statuses=statuses,
        types=(
            Type("Epic", ("Open", "Closed"), "Open"),
            Type("Feature", ("Proposed", "Approved"), "Proposed"),
            Type("Task", ("Draft", "Ready"), "Draft"),
        ),
        roles=(
            Role("Owner", "all"),
            Role("Model", ("view_work_packages",)),
            Role("Conductor", ("view_work_packages",)),
        ),
        workflows=(),
        fields=(),
        project_fields=(),
        users=(
            User("spark", "Spark", "Model", "public"),
            User("conductor", "Conductor", "Conductor", "all"),
        ),
        versions=("Now",),
        views=(),
    )


def make_settings():
    return Settings(
        openproject=OpenProject("http://127.0.0.1:8080", "OPL_TOKEN_ADMIN", "admin"),
        tokens={"claude": "C", "codex": "X", "spark": "S", "conductor": "D"},
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


def _filters(query):
    try:
        raw = parse_qs(query).get("filters", ["[]"])[0]
        return json.loads(raw)
    except ValueError:
        return []


def _tail(href):
    return str(href).rstrip("/").rsplit("/", 1)[-1]


VISIBILITY_OPTIONS = {"Public": 61, "Private": 62}


class FakeWorld:
    """Stateful OpenProject: writes change later reads."""

    def __init__(self, server):
        self.server = server
        self.base = server.base_url
        self.next_id = 1000
        self.users = {}
        self.projects = {}
        self.memberships = []
        self.versions = {}
        self.work_packages = []
        self._static()
        self._stateful()

    def _new_id(self):
        self.next_id += 1
        return self.next_id

    def _static(self):
        roles = [
            {"id": 1, "name": "Owner"},
            {"id": 2, "name": "Model"},
            {"id": 3, "name": "Conductor"},
        ]
        for role in roles:
            role["_links"] = {"self": {"href": "%s/api/v3/roles/%d" % (self.base, role["id"])}}
        self.server.add("GET", "/api/v3/roles",
                        body={"_embedded": {"elements": roles}})
        types = [
            {"id": 11, "name": "Epic"},
            {"id": 12, "name": "Feature"},
            {"id": 13, "name": "Task"},
        ]
        for item in types:
            item["_links"] = {"self": {"href": "%s/api/v3/types/%d" % (self.base, item["id"])}}
        self.server.add("GET", "/api/v3/types",
                        body={"_embedded": {"elements": list(types)}})
        statuses = [
            {"id": 21, "name": "Open"},
            {"id": 22, "name": "Approved"},
            {"id": 23, "name": "Draft"},
        ]
        for item in statuses:
            item["_links"] = {"self": {"href": "%s/api/v3/statuses/%d" % (self.base, item["id"])}}
        self.server.add("GET", "/api/v3/statuses",
                        body={"_embedded": {"elements": list(statuses)}})
        # Documented v3 shape: definitions at the schema root. Repo is a
        # "link" (URL) field, so a plain string; Visibility is a list field
        # whose options are links (TH.7).
        self.server.add("GET", "/api/v3/projects/schema", body={
            "_type": "Schema",
            "customField5": {"type": "Link", "name": "Repo"},
            # A plain schema: no allowed values, as on real OpenProject;
            # the options come from the project form (TH.23, Codex N3).
            "customField6": {"type": "CustomOption", "name": "Visibility",
                             "location": "_links", "_links": {}},
        })
        self.wp_schema = {"_type": "Schema"}

    def _project_element(self, project):
        pid = project["id"]
        return {
            "id": pid,
            "name": project["name"],
            "customField5": project["repo"],
            "_links": {
                "customField6": ({"href": "/api/v3/custom_options/%d"
                                          % VISIBILITY_OPTIONS[project["visibility"]],
                                  "title": project["visibility"]}
                                 if project["visibility"] else {"href": None}),
                "self": {"href": "%s/api/v3/projects/%d" % (self.base, pid)},
                "memberships": {"href": "%s/api/v3/projects/%d/memberships" % (self.base, pid)},
                "versions": {"href": "%s/api/v3/projects/%d/versions" % (self.base, pid)},
            },
        }

    def seed_user(self, login):
        uid = self._new_id()
        self.users[login] = {"id": uid, "login": login,
                             "_links": {"self": {"href": "%s/api/v3/users/%d" % (self.base, uid)}}}
        return uid

    def seed_project(self, name, repo="", visibility="Public"):
        pid = self._new_id()
        self.projects[name] = {"id": pid, "name": name, "repo": repo,
                               "visibility": visibility}
        self.versions[pid] = []
        return pid

    def seed_membership(self, project_name, login, role_id):
        self.memberships.append({"id": self._new_id(),
                                 "project_id": self.projects[project_name]["id"],
                                 "principal_id": self.users[login]["id"],
                                 "role_ids": [role_id]})

    def _stateful(self):
        server = self.server

        def get_users(method, path, query, body, headers):
            wanted = [f["login"]["values"] for f in _filters(query) if "login" in f]
            elements = [u for u in self.users.values()
                        if not wanted or u["login"] in wanted[0]]
            return 200, {"_embedded": {"elements": elements}}

        def post_users(method, path, query, body, headers):
            uid = self._new_id()
            element = {"id": uid, "login": body["login"],
                       "_links": {"self": {"href": "%s/api/v3/users/%d" % (self.base, uid)}}}
            self.users[body["login"]] = element
            return 201, element

        def get_projects(method, path, query, body, headers):
            wanted = [f["name"]["values"] for f in _filters(query) if "name" in f]
            elements = [self._project_element(p) for p in self.projects.values()
                        if not wanted or p["name"] in wanted[0]]
            return 200, {"_embedded": {"elements": elements}}

        def post_projects(method, path, query, body, headers):
            pid = self._new_id()
            self.projects[body["name"]] = {"id": pid, "name": body["name"],
                                           "repo": "", "visibility": ""}
            self.versions[pid] = []
            return 201, self._project_element(self.projects[body["name"]])

        def patch_project(method, path, query, body, headers):
            pid = int(_tail(path))
            for project in self.projects.values():
                if project["id"] == pid:
                    if "customField5" in body:
                        project["repo"] = body["customField5"]
                    link = body.get("_links", {}).get("customField6")
                    if link is not None:
                        by_id = {v: k for k, v in VISIBILITY_OPTIONS.items()}
                        project["visibility"] = by_id[int(_tail(link["href"]))]
                    return 200, self._project_element(project)
            return 404, {}

        def membership_element(membership):
            return {
                "id": membership["id"],
                "_links": {
                    "principal": {"href": "%s/api/v3/users/%d" % (self.base, membership["principal_id"])},
                    "roles": [{"href": "%s/api/v3/roles/%d" % (self.base, rid)}
                              for rid in membership["role_ids"]],
                },
            }

        def get_memberships(method, path, query, body, headers):
            pid = int(path.split("/projects/")[1].split("/")[0])
            elements = [membership_element(m) for m in self.memberships
                        if m["project_id"] == pid]
            return 200, {"_embedded": {"elements": elements}}

        def post_memberships(method, path, query, body, headers):
            membership = {"id": self._new_id(),
                          "project_id": int(_tail(body["project"]["href"])),
                          "principal_id": int(_tail(body["principal"]["href"])),
                          "role_ids": [int(_tail(r["href"])) for r in body["roles"]]}
            self.memberships.append(membership)
            return 201, membership_element(membership)

        def delete_membership(method, path, query, body, headers):
            mid = int(_tail(path))
            self.memberships = [m for m in self.memberships if m["id"] != mid]
            return 204, {}

        def get_versions(method, path, query, body, headers):
            pid = int(path.split("/projects/")[1].split("/")[0])
            return 200, {"_embedded": {"elements": list(self.versions.get(pid, []))}}

        def post_versions(method, path, query, body, headers):
            pid = int(path.split("/projects/")[1].split("/")[0])
            version = {"id": self._new_id(), "name": body["name"]}
            self.versions.setdefault(pid, []).append(version)
            return 201, version

        def wp_element(item):
            links = {"type": {"href": "%s/api/v3/types/%d" % (self.base, item["type_id"])},
                     "status": {"href": "%s/api/v3/statuses/%d" % (self.base, item["status_id"])},
                     "project": {"href": "%s/api/v3/projects/%d" % (self.base, item["project_id"])}}
            # OpenProject always sends parent; href null means none.
            links["parent"] = ({"href": "%s/api/v3/work_packages/%d"
                                        % (self.base, item["parent_id"])}
                               if item.get("parent_id") else {"href": None})
            return {"id": item["id"], "subject": item["subject"], "_links": links}

        def get_wps(method, path, query, body, headers):
            wanted = [f["project"]["values"] for f in _filters(query) if "project" in f]
            elements = [wp_element(i) for i in self.work_packages
                        if not wanted or str(i["project_id"]) in wanted[0]]
            return 200, {"_embedded": {"elements": elements}}

        def post_wps(method, path, query, body, headers):
            links = body.get("_links", {})
            item = {"id": self._new_id(),
                    "subject": body.get("subject", ""),
                    "type_id": int(_tail(links["type"]["href"])),
                    "status_id": int(_tail(links["status"]["href"])),
                    "project_id": int(_tail(links["project"]["href"])),
                    "parent_id": int(_tail(links["parent"]["href"])) if "parent" in links else None}
            self.work_packages.append(item)
            return 201, {"id": item["id"]}

        server.add("GET", "/api/v3/users", handler=get_users)
        server.add("POST", "/api/v3/users", handler=post_users)
        server.add("GET", "/api/v3/projects", handler=get_projects)
        server.add("POST", "/api/v3/projects", handler=post_projects)
        server.add("POST", "/api/v3/memberships", handler=post_memberships)
        server.add("GET", "/api/v3/work_packages", handler=get_wps)
        server.add("POST", "/api/v3/work_packages", handler=post_wps)
        self._handlers = {
            "patch": patch_project,
            "get_memberships": get_memberships,
            "get_versions": get_versions,
            "post_versions": post_versions,
            "delete": delete_membership,
        }

    def register_project_routes(self, pid):
        # Per-project routes use the shared stateful handlers, which parse
        # the project id out of the request path.
        self.server.add("PATCH", "/api/v3/projects/%d" % pid, handler=self._handlers["patch"])
        self.server.add("GET", "/api/v3/projects/%d/memberships" % pid,
                        handler=self._handlers["get_memberships"])
        self.server.add("GET", "/api/v3/projects/%d/versions" % pid,
                        handler=self._handlers["get_versions"])
        self.server.add("POST", "/api/v3/projects/%d/versions" % pid,
                        handler=self._handlers["post_versions"])
        self.server.add("POST", "/api/v3/projects/%d/form" % pid,
                        handler=lambda *a: (200, {"_type": "Form", "_embedded": {
                            "payload": {}, "validationErrors": {},
                            "schema": {"customField6": {
                                "type": "CustomOption", "name": "Visibility",
                                "location": "_links",
                                "_links": {"allowedValues": [
                                    {"href": "/api/v3/custom_options/61",
                                     "title": "Public"},
                                    {"href": "/api/v3/custom_options/62",
                                     "title": "Private"}]}}}}}))
        for tid in (11, 12, 13):
            self.server.add("GET", "/api/v3/work_packages/schemas/%d-%d" % (pid, tid),
                            handler=lambda *a: (200, self.wp_schema))

class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.world = FakeWorld(self.server)
        self.client = Client(self.server.base_url, "admin-token")
        self.model = make_model()
        self.settings = make_settings()

    def _register_all(self):
        for project in self.world.projects.values():
            self.world.register_project_routes(project["id"])
        # DELETE needs one handler per membership id; re-register whenever
        # the world gains memberships (i.e. between two apply runs).
        for membership in self.world.memberships:
            path = "/api/v3/memberships/%d" % membership["id"]
            self.server.add("DELETE", path, handler=self.world._handlers["delete"])

    def test_first_run_creates_and_second_is_silent(self):
        for project in ("Demo public project", "Demo private project"):
            self.world.seed_project(project)
        self.world.seed_user("admin")
        self._register_all()
        actions1 = apply_api(self.client, self.model, self.settings)
        writes1 = self.server.writes()
        self.assertTrue(writes1, "first run wrote nothing")
        self.assertIn("create user spark", actions1)
        self.assertIn("add spark to Demo public project as Model", actions1)
        self.assertIn("create Maintenance epic in Demo public project", actions1)
        self.assertIn("create Maintenance feature in Demo public project", actions1)
        visibility = [r["body"]["_links"]["customField6"]["href"]
                      for r in writes1 if r["method"] == "PATCH"
                      and "customField6" in r["body"].get("_links", {})]
        # Both were seeded Public: only the private one is patched, and as
        # an option link, not a plain string.
        self.assertEqual(visibility, ["/api/v3/custom_options/62"])
        self._register_all()
        writes_before = len(self.server.writes())
        actions2 = apply_api(self.client, self.model, self.settings)
        self.assertEqual(actions2, [])
        self.assertEqual(len(self.server.writes()), writes_before)

    def test_spark_membership_in_private_is_removed(self):
        self.world.seed_user("admin")
        self.world.seed_user("spark")
        self.world.seed_project("Demo public project", "example-owner/demo-public", "Public")
        self.world.seed_project("Demo private project", "example-owner/demo-private", "Private")
        self.world.seed_membership("Demo private project", "spark", 2)
        self._register_all()
        actions = apply_api(self.client, self.model, self.settings)
        deletes = [r for r in self.server.writes() if r["method"] == "DELETE"]
        self.assertEqual(len(deletes), 1)
        self.assertIn("remove spark from Demo private project", actions)
        before = len(self.server.requests)
        self._register_all()
        apply_api(self.client, self.model, self.settings)
        deletes2 = [r for r in self.server.requests[before:] if r["method"] == "DELETE"]
        self.assertEqual(deletes2, [])

    def test_dry_run_matches_live_without_writing(self):
        servers = []
        worlds = []
        for _ in range(2):
            server = FakeServer()
            server.__enter__()
            self.addCleanup(server.__exit__, None, None, None)
            world = FakeWorld(server)
            world.seed_project("Demo public project")
            world.seed_project("Demo private project")
            world.seed_user("admin")
            servers.append(server)
            worlds.append(world)
        dry_client = Client(servers[0].base_url, "t")
        live_client = Client(servers[1].base_url, "t")
        for world in worlds:
            for project in world.projects.values():
                world.register_project_routes(project["id"])
        dry_actions = apply_api(dry_client, self.model, self.settings, dry_run=True)
        self.assertEqual(servers[0].writes(), [])
        live_actions = apply_api(live_client, self.model, self.settings, dry_run=False)
        self.assertTrue(servers[1].writes())
        self.assertEqual(dry_actions, live_actions)

    def test_visibility_only_drift_is_planned_in_a_dry_run(self):
        # Codex final3 D2: Repo already right, only Visibility differs.
        # The dry run lists the same "set Visibility" as the live run, and
        # makes no call at all (not even the POST project form).
        runs = []
        for dry in (True, False):
            server = FakeServer()
            server.__enter__()
            self.addCleanup(server.__exit__, None, None, None)
            world = FakeWorld(server)
            world.seed_user("admin")
            world.seed_project("Demo public project",
                               "example-owner/demo-public", "Public")
            world.seed_project("Demo private project",
                               "example-owner/demo-private", "Public")
            for project in world.projects.values():
                world.register_project_routes(project["id"])
            actions = apply_api(Client(server.base_url, "t"), self.model,
                                self.settings, dry_run=dry)
            runs.append((actions, server.writes(), world))
        (dry_actions, dry_writes, _), (live_actions, _, live_world) = runs
        self.assertIn("set Visibility on Demo private project", dry_actions)
        self.assertEqual(dry_writes, [])
        self.assertEqual(dry_actions, live_actions)
        self.assertEqual(live_world.projects["Demo private project"]["visibility"],
                         "Private")

    def test_maintenance_feature_created_once_under_epic(self):
        self.world.seed_user("admin")
        self.world.seed_user("spark")
        self.world.seed_user("conductor")
        pid = self.world.seed_project("Demo public project", "example-owner/demo-public", "Public")
        self.world.seed_project("Demo private project", "example-owner/demo-private", "Private")
        epic_id = self.world._new_id()
        self.world.work_packages.append({"id": epic_id, "subject": "Maintenance",
                                         "type_id": 11, "status_id": 21,
                                         "project_id": pid, "parent_id": None})
        self._register_all()
        actions = apply_api(self.client, self.model, self.settings)
        self.assertNotIn("create Maintenance epic in Demo public project", actions)
        self.assertIn("create Maintenance feature in Demo public project", actions)
        posts = [r for r in self.server.writes()
                 if r["method"] == "POST" and r["path"] == "/api/v3/work_packages"]
        self.assertEqual(len(posts), 3)  # private epic + feature, public feature
        public_feature = [r for r in posts
                          if r["body"]["_links"]["project"]["href"].endswith("/%d" % pid)]
        self.assertEqual(len(public_feature), 1)
        parent = public_feature[0]["body"]["_links"]["parent"]["href"]
        self.assertTrue(parent.endswith("/%d" % epic_id))


if __name__ == "__main__":
    unittest.main(verbosity=2)
