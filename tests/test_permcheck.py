#!/usr/bin/env python3
"""Tests for opl.permcheck against a scripted FakeServer."""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from opl.openproject import Client
from opl.permcheck import Check, main, run_all
from opl.settings import Conductor, OpenProject, Runner, Settings
from tests.fakes.http_fake import FakeServer


def make_settings():
    return Settings(
        openproject=OpenProject("http://127.0.0.1:8080", "OPL_TOKEN_ADMIN", "admin"),
        tokens={"claude": "C", "codex": "X", "spark": "S"},
        github_token_env="OPL_GITHUB_TOKEN",
        users_email_domain="example.invalid",
        conductor=Conductor(False, 60, "/tmp/opl-state"),
        runner=Runner((), 1, {}),
        projects=(),
        permcheck={"project": "Sandbox", "feature": "Test feature"},
    )


class PermWorld:
    """Scripted sandbox: knobs are move_status and spark_sees_private."""

    def __init__(self, server, move_status=422, spark_sees_private=False):
        self.server = server
        self.base = server.base_url
        self.move_status = move_status
        self.created = []
        self.deleted = []
        # Visibility is a list field: its value is a link with a title.
        projects = [{"id": 101, "name": "Sandbox",
                     "_links": {"self": {"href": self.base + "/api/v3/projects/101"},
                                "customField6": {"href": "/api/v3/custom_options/61",
                                                 "title": "Public"}}}]
        if spark_sees_private:
            projects.append({"id": 102, "name": "Secret",
                             "_links": {"self": {"href": self.base + "/api/v3/projects/102"},
                                        "customField6": {"href": "/api/v3/custom_options/62",
                                                         "title": "Private"}}})
        server.add("GET", "/api/v3/projects", body={"_embedded": {"elements": projects}})
        server.add("GET", "/api/v3/projects/schema", body={
            "customField6": {"type": "CustomOption", "name": "Visibility",
                             "location": "_links"}})
        server.add("GET", "/api/v3/types", body={
            "_embedded": {"elements": [{"id": 13, "name": "Task"}]}})
        server.add("GET", "/api/v3/statuses", body={
            "_embedded": {"elements": [{"id": 23, "name": "Draft"},
                                       {"id": 24, "name": "Approved"}]}})
        server.add("GET", "/api/v3/work_packages", handler=self.get_wps)
        server.add("POST", "/api/v3/work_packages", handler=self.post_wps)
        server.add("DELETE", "/api/v3/work_packages/999", handler=self.delete_wp)
        server.add("PATCH", "/api/v3/work_packages/55", handler=self.patch_feature)

    def get_wps(self, method, path, query, body, headers):
        feature = {"id": 55, "subject": "Test feature",
                   "_links": {"self": {"href": self.base + "/api/v3/work_packages/55"}}}
        return 200, {"_embedded": {"elements": [feature]}}

    def post_wps(self, method, path, query, body, headers):
        self.created.append(body)
        return 201, {"id": 999}

    def delete_wp(self, method, path, query, body, headers):
        self.deleted.append(path)
        return 204, {}

    def patch_feature(self, method, path, query, body, headers):
        if self.move_status == 200:
            return 200, {"id": 55}
        return self.move_status, {"message": "forbidden"}


PERMCHECK_TOML = """
[openproject]
url = "%s"
admin_token_env = "OPL_TOKEN_ADMIN"
owner_login = "admin"

[tokens]
claude = "OPL_TOKEN_CLAUDE"
codex = "OPL_TOKEN_CODEX"
spark = "OPL_TOKEN_SPARK"
conductor = "OPL_TOKEN_CONDUCTOR"

[github]
token_env = "OPL_GITHUB_TOKEN"

[users]
email_domain = "example.invalid"

[permcheck]
project = "Sandbox"
feature = "Test feature"

[conductor]
live = false
interval_seconds = 60
state_dir = "~/.local/state/opl"

[runner]
command = ["example-worker"]
max_parallel = 2

[runner.limits_minutes]
S = 20
M = 45
L = 90
review = 15
test = 20
stall = 10
"""


class PermcheckTests(unittest.TestCase):
    def _run(self, move_status=422, spark_sees_private=False):
        server = FakeServer()
        server.__enter__()
        self.addCleanup(server.__exit__, None, None, None)
        world = PermWorld(server, move_status, spark_sees_private)
        settings = make_settings()
        admin = Client(server.base_url, "tok-admin")
        clients = {who: Client(server.base_url, "tok-" + who)
                   for who in ("claude", "codex", "spark")}
        checks = run_all(settings, clients.__getitem__, admin)
        return checks, world

    def _by_name(self, checks, who, name):
        for check in checks:
            if check.user == who and check.name == name:
                return check
        self.fail("no check %s/%s" % (who, name))

    def test_all_pass(self):
        checks, world = self._run()
        self.assertTrue(checks)
        self.assertTrue(all(c.passed for c in checks),
                        [c for c in checks if not c.passed])
        self.assertEqual(len(world.created), 3)
        self.assertEqual(len(world.deleted), 3)
        create = world.created[0]
        self.assertEqual(create["subject"], "opl-permcheck probe")
        self.assertIn("parent", create["_links"])

    def test_model_allowed_to_approve_fails(self):
        checks, world = self._run(move_status=200)
        bad = self._by_name(checks, "claude", "forbidden-move")
        self.assertFalse(bad.passed)
        self.assertTrue(self._by_name(checks, "claude", "read").passed)
        self.assertTrue(self._by_name(checks, "claude", "create-delete").passed)

    def test_spark_seeing_private_fails(self):
        checks, world = self._run(spark_sees_private=True)
        bad = self._by_name(checks, "spark", "no-private")
        self.assertFalse(bad.passed)
        self.assertTrue(all(c.passed for c in checks
                            if not (c.user == "spark" and c.name == "no-private")),
                        [c for c in checks if not c.passed])

    def test_main_reports_pass_and_exits_zero(self):
        server = FakeServer()
        server.__enter__()
        self.addCleanup(server.__exit__, None, None, None)
        PermWorld(server)
        tmpd = tempfile.mkdtemp(prefix="opl-perm-")
        with open(os.path.join(tmpd, "opl.toml"), "w", encoding="utf-8") as fh:
            fh.write(PERMCHECK_TOML % server.base_url)
        env = {
            "OPL_CONFIG_DIR": tmpd,
            "OPL_TOKEN_ADMIN": "sentinel-admin",
            "OPL_TOKEN_CLAUDE": "sentinel-claude",
            "OPL_TOKEN_CODEX": "sentinel-codex",
            "OPL_TOKEN_SPARK": "sentinel-spark",
            "OPL_TOKEN_CONDUCTOR": "sentinel-conductor",
            "OPL_GITHUB_TOKEN": "sentinel-gh",
        }
        out = io.StringIO()
        with mock.patch.dict(os.environ, env):
            with contextlib.redirect_stdout(out):
                rc = main([])
        self.assertEqual(rc, 0)
        self.assertIn("permcheck spark no-private: PASS", out.getvalue())

    def test_missing_token_is_a_fail_not_a_crash(self):
        server = FakeServer()
        server.__enter__()
        self.addCleanup(server.__exit__, None, None, None)
        PermWorld(server)
        settings = make_settings()
        admin = Client(server.base_url, "tok-admin")

        def client_for(who):
            if who == "codex":
                raise_no_token()
            return Client(server.base_url, "tok-" + who)

        def raise_no_token():
            from opl.settings import MissingSecret

            raise MissingSecret("OPL_TOKEN_CODEX")

        checks = run_all(settings, client_for, admin)
        token_check = self._by_name(checks, "codex", "token")
        self.assertFalse(token_check.passed)
        self.assertIn("OPL_TOKEN_CODEX", token_check.detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
