#!/usr/bin/env python3
"""Tests for opl.configure.__main__: full run, dry run, missing token.

FakeServer for the API, the fake `docker` for bin/opl-compose, real
bin/opl-compose (it is a deliverable). No daemon, no network.
"""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from opl.configure.__main__ import main
from opl.model import load as load_model

try:
    from test_apply_api import FakeWorld
except ImportError:  # when loaded as tests.test_configure_cli instead
    from tests.test_apply_api import FakeWorld
from tests.fakes.http_fake import FakeServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SETTINGS_TOML = """
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

[[project]]
key = "demo-public"
name = "Demo public project"
repo = "example-owner/demo-public"
visibility = "Public"
has_test_env = false
prod_signal = { kind = "environment", name = "production" }

[[project]]
key = "demo-private"
name = "Demo private project"
repo = "example-owner/demo-private"
visibility = "Private"
has_test_env = false
prod_signal = { kind = "environment", name = "production" }
"""

WP_SCHEMA = {  # fields at the schema root (TH.7)
    "_type": "Schema",
    "customField11": {"name": "Risk"},
    "customField12": {"name": "Spec link"},
    "customField13": {"name": "Models"},
    "customField14": {"name": "Test result"},
    "customField15": {"name": "Reviewer"},
    "customField16": {"name": "Size"},
    "customField17": {"name": "PR link"},
    "customField18": {"name": "Review result"},
    "customField19": {"name": "Merge OK"},
    "customField20": {"name": "Needs you"},
    "customField21": {"name": "Action"},
    "customField22": {"name": "Est. cost"},
    "customField23": {"name": "Actual cost"},
    "customField24": {"name": "Actual tokens"},
}


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.world = FakeWorld(self.server)
        self.world.seed_user("admin")
        self.world.seed_project("Demo public project",
                                "https://github.com/example-owner/demo-public", "Public")
        self.world.seed_project("Demo private project",
                                "https://github.com/example-owner/demo-private", "Private")
        for project in self.world.projects.values():
            self.world.register_project_routes(project["id"])
        for project in self.world.projects.values():
            self.world.register_project_routes(project["id"])
        self.queries = []
        self.grid = {"id": 3000, "rowCount": 1, "columnCount": 2, "widgets": [],
                     "_links": {"self": {"href": self.server.base_url + "/api/v3/grids/3000"},
                                "scope": {"href": "/my/page"}}}
        self.world.wp_schema = WP_SCHEMA
        self.server.add("GET", "/api/v3/queries", handler=self._get_queries)
        self.server.add("POST", "/api/v3/queries", handler=self._post_queries)
        self.server.add("GET", "/api/v3/grids",
                        body={"_embedded": {"elements": [self.grid]}})
        self.server.add("PATCH", "/api/v3/grids/3000", handler=self._patch_grid)

        self.tmpd = tempfile.mkdtemp(prefix="opl-config-")
        with open(os.path.join(self.tmpd, "opl.toml"), "w", encoding="utf-8") as fh:
            fh.write(SETTINGS_TOML % self.server.base_url)
        self.runtime = tempfile.mkdtemp(prefix="opl-rt-")
        for name in ("docker-compose.yml", "docker-compose.override.yml", ".env"):
            with open(os.path.join(self.runtime, name), "w", encoding="utf-8") as fh:
                fh.write("# test runtime\n")
        self.docker_log = os.path.join(self.tmpd, "docker.log")
        self.model = load_model(os.path.join(REPO, "config", "pm-model.toml"))

    def _env(self, extra=None):
        env = dict(os.environ)
        env["OPL_CONFIG_DIR"] = self.tmpd
        env["OPL_TOKEN_ADMIN"] = "sentinel-admin-token"
        env["OPL_ROOT"] = REPO
        env["OPL_RUNTIME_DIR"] = self.runtime
        env["OPL_PROJECT"] = "opl-cli-test"
        env["OPL_BACKUP_ROOT"] = os.path.join(self.tmpd, "backups")
        env["FAKE_DOCKER_STATE"] = self.tmpd
        env["FAKE_DOCKER_LOG"] = self.docker_log
        # Prepend the fake dir in NATIVE form (os.pathsep): the msys runtime
        # converts the whole PATH uniformly, exactly like the inherited one.
        # (A pre-converted POSIX entry plus a replaced PATH breaks
        # shutil.which("bash") inside main on Windows.)
        env["PATH"] = os.path.join(REPO, "tests", "fakebin") + os.pathsep + env["PATH"]
        if extra:
            env.update(extra)
        return env

    def _get_queries(self, method, path, query, body, headers):
        return 200, {"_embedded": {"elements": list(self.queries)}}

    def _get_single_query(self, method, path, query, body, headers):
        try:
            qid = int(path.rsplit("/", 1)[-1])
        except ValueError:
            return 404, {}
        for stored in self.queries:
            if stored.get("id") == qid:
                return 200, stored
        return 404, {"_type": "Error", "message": "not found"}

    def _post_queries(self, method, path, query, body, headers):
        element = {k: v for k, v in body.items() if k != "_links"}
        element["id"] = 4000 + len(self.queries)
        element["_links"] = dict((body.get("_links") or {}))
        element["_links"]["self"] = {"href": "%s/api/v3/queries/%d"
                                     % (self.server.base_url, element["id"])}
        if "project" in body and "project" not in element["_links"]:
            element["_links"]["project"] = body["project"]
        self.queries.append(element)
        self.server.add("GET", "/api/v3/queries/%d" % element["id"],
                        handler=self._get_single_query)
        return 201, element

    def _patch_grid(self, method, path, query, body, headers):
        # Same replace-on-missing-id model as tests.test_views: widgets
        # without `id` are new; old widgets missing from the PATCH lose
        # their owned queries.
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
        seq = 8000 + len(incoming)
        for widget in incoming:
            if widget.get("id") is None:
                seq += 1
                widget["id"] = seq
        self.grid.update({k: body[k] for k in ("rowCount", "columnCount", "widgets")
                          if k in body})
        return 200, self.grid

    def _docker_log(self):
        if not os.path.isfile(self.docker_log):
            return ""
        with open(self.docker_log, encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def test_full_run_order(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, self._env()):
            with contextlib.redirect_stdout(out):
                rc = main([])
        self.assertEqual(rc, 0)
        log = self._docker_log()
        cp_at = log.find(" cp ")
        exec_at = log.find("rails runner")
        self.assertTrue(cp_at >= 0, "no compose cp call logged:\n%s" % log)
        self.assertTrue(exec_at > cp_at,
                        "rails runner must run after cp:\n%s" % log)
        self.assertTrue(self.server.writes(), "no API writes happened")
        self.assertIn("create user spark", out.getvalue())

    def test_dry_run_makes_no_docker_calls_and_no_writes(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, self._env()):
            with contextlib.redirect_stdout(out):
                rc = main(["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.isfile(self.docker_log),
                         "dry run must not call docker")
        self.assertEqual(self.server.writes(), [])
        self.assertIn("would create user spark", out.getvalue())

    def test_dry_run_lists_unmanaged_workflow_cleanup(self):
        # Issue #35: the dry-run plan reports the scoped cleanup for the
        # managed types (Epic and Task included), and nothing outside them.
        out = io.StringIO()
        with mock.patch.dict(os.environ, self._env()):
            with contextlib.redirect_stdout(out):
                rc = main(["--dry-run"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        for tname in ("Epic", "Feature", "Task"):
            with self.subTest(type=tname):
                self.assertIn(
                    "would remove workflows for unmanaged roles on type %s"
                    % tname, text)
        # Managed types only: no other type name may appear in a cleanup line.
        for line in text.splitlines():
            if "unmanaged roles on type" in line:
                self.assertTrue(
                    any("on type %s" % name in line
                        for name in ("Epic", "Feature", "Task")),
                    "cleanup lists a type outside the model: %s" % line)

    def test_dry_run_lists_priority_renames(self):
        # Issue #39: the dry-run plan reports the four in-place priority
        # renames (Immediate->P0, High->P1, Normal->P2, Low->P3) from the
        # same helper the plan uses.
        from opl.configure.admin_ruby import describe_priorities

        out = io.StringIO()
        with mock.patch.dict(os.environ, self._env()):
            with contextlib.redirect_stdout(out):
                rc = main(["--dry-run"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        for desc in describe_priorities():
            with self.subTest(action=desc):
                self.assertIn("would " + desc, text)
        self.assertEqual(
            describe_priorities(),
            ["rename priority Immediate -> P0",
             "rename priority High -> P1",
             "rename priority Normal -> P2",
             "rename priority Low -> P3"],
        )

    def test_missing_admin_token_fails_naming_the_var(self):
        env = self._env()
        env.pop("OPL_TOKEN_ADMIN", None)
        err = io.StringIO()
        with mock.patch.dict(os.environ, env):
            os.environ.pop("OPL_TOKEN_ADMIN", None)
            with contextlib.redirect_stderr(err):
                rc = main([])
        self.assertNotEqual(rc, 0)
        self.assertIn("OPL_TOKEN_ADMIN", err.getvalue())
        self.assertFalse(os.path.isfile(self.docker_log))
        self.assertEqual(self.server.writes(), [])


class RailsErrorTests(unittest.TestCase):
    def test_first_error_line_prefers_exception(self):
        from opl.configure.__main__ import _first_error_line

        log = ("/gems/activerecord-1/lib/base.rb:10:in `save!'\n"
               "ActiveRecord::RecordInvalid: Validation failed: Name taken\n"
               "/gems/activesupport-1/lib/log.rb:5:in `info'\n")
        self.assertEqual(
            _first_error_line(log),
            "ActiveRecord::RecordInvalid: Validation failed: Name taken")

    def test_first_error_line_empty_without_exception(self):
        from opl.configure.__main__ import _first_error_line

        self.assertEqual(_first_error_line("just some output\nno error\n"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
