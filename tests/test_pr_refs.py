#!/usr/bin/env python3
"""Tests: pr_base validation and open-PR lookup (TH.11)."""

import os
import tempfile
import unittest
from unittest import mock

from opl.github import GitHub
from opl.openproject import ApiError
from opl.settings import SettingsError, load, valid_pr_base
from tests.fakes.http_fake import FakeServer


def load_project(extra=""):
    base = """
[openproject]
url = "http://127.0.0.1:8080"
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
state_dir = "/tmp/opl-state"

[runner]
command = ["w"]
max_parallel = 2

[runner.limits_minutes]
S = 20
M = 45
L = 90
review = 15
test = 20
stall = 10

[[project]]
key = "demo"
name = "Demo"
repo = "example-owner/demo"
visibility = "Public"
has_test_env = false
prod_signal = { kind = "environment", name = "production" }
"""
    tmpd = tempfile.mkdtemp(prefix="opl-prbase-")
    path = os.path.join(tmpd, "opl.toml")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(base + extra)
    with mock.patch.dict(os.environ, {"OPL_CONFIG_DIR": tmpd}):
        return load()


class PrBaseTests(unittest.TestCase):
    def test_plain_branch_accepted(self):
        self.assertTrue(valid_pr_base("main"))
        self.assertTrue(valid_pr_base("release/1.x"))

    def test_remote_tracking_ref_rejected(self):
        self.assertFalse(valid_pr_base("origin/main"))

    def test_bad_syntax_rejected(self):
        for bad in ("", "main ", " main", "a..b", "a@{b}", "-x", "a//b",
                    "a/", ".", "HEAD"):
            self.assertFalse(valid_pr_base(bad), bad)

    def test_settings_default_is_main(self):
        settings = load_project()
        self.assertEqual(settings.projects[0].pr_base, "main")

    def test_settings_accepts_plain_branch(self):
        settings = load_project('pr_base = "develop"\n')
        self.assertEqual(settings.projects[0].pr_base, "develop")

    def test_settings_rejects_origin_main(self):
        with self.assertRaises(SettingsError):
            load_project('pr_base = "origin/main"\n')


class FindOpenPrTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)

    def test_reuses_open_pr_from_same_head(self):
        url = self.server.base_url + "/example-owner/demo/pull/9"

        def list_pulls(method, path, query, body, headers):
            assert "state=open" in query, query
            return 200, [{"html_url": url, "number": 9,
                          "head": {"ref": "opl/task-T5-x"}}]

        self.server.add("GET", "/repos/example-owner/demo/pulls",
                        handler=list_pulls)
        gh = GitHub("gh-token", self.server.base_url)
        self.assertEqual(
            gh.find_open_pr("example-owner", "demo", "opl/task-T5-x"), url)

    def test_reuse_sends_head_and_base_and_checks_the_base(self):
        url = self.server.base_url + "/example-owner/demo/pull/9"
        queries = []
        pulls = [{"html_url": url + "0", "base": {"ref": "release"}},
                 {"html_url": url, "base": {"ref": "main"}}]

        def list_pulls(method, path, query, body, headers):
            queries.append(query)
            return 200, pulls

        self.server.add("GET", "/repos/example-owner/demo/pulls",
                        handler=list_pulls)
        gh = GitHub("gh-token", self.server.base_url)
        self.assertEqual(
            gh.find_open_pr("example-owner", "demo", "opl/task-T5-x", "main"), url)
        self.assertIn("head=example-owner%3Aopl%2Ftask-T5-x", queries[0])
        self.assertIn("base=main", queries[0])
        pulls[1]["base"]["ref"] = "develop"
        self.assertEqual(
            gh.find_open_pr("example-owner", "demo", "opl/task-T5-x", "main"), "")

    def test_no_open_pr_returns_empty(self):
        self.server.add("GET", "/repos/example-owner/demo/pulls", body=[])
        gh = GitHub("gh-token", self.server.base_url)
        self.assertEqual(
            gh.find_open_pr("example-owner", "demo", "opl/task-T5-x"), "")

    def test_lookup_failure_raises(self):
        # TH.20 (Codex E7): "could not look" is not "no PR"; the caller
        # retries later instead of risking a duplicate.
        gh = GitHub("gh-token", self.server.base_url)
        with self.assertRaises(ApiError):
            gh.find_open_pr("example-owner", "demo", "opl/task-T5-x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
