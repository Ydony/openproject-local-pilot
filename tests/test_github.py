#!/usr/bin/env python3
"""Tests for opl.github and collect_github, FakeServer as the GitHub API."""

import unittest
from datetime import datetime, timezone

from opl.conductor.collect import collect_github
from opl.conductor.state import Item, Project, Signal
from opl.github import GitHub
from opl.openproject import ApiError
from tests.fakes.http_fake import FakeServer

PR_MERGED = "https://github.com/example-owner/demo-public/pull/7"
PR_OPEN = "https://github.com/example-owner/demo-public/pull/8"


def make_project(**over):
    args = {"key": "demo-public", "op_id": 1, "repo": "example-owner/demo-public",
            "visibility": "Public", "has_test_env": True,
            "test_signal": Signal("workflow", "deploy-test"),
            "prod_signal": Signal("environment", "production")}
    args.update(over)
    return Project(**args)


class GitHubTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        base = self.server.base_url
        self.server.add("GET", "/repos/example-owner/demo-public/pulls/7", body={
            "merged": True, "merged_at": "2026-09-23T10:00:00Z",
            "head": {"sha": "aaa"},
        })
        self.server.add("GET", "/repos/example-owner/demo-public/commits/aaa/status",
                        body={"state": "success"})
        self.server.add("GET", "/repos/example-owner/demo-public/commits/aaa/check-runs",
                        body={"check_runs": [{"conclusion": "success"},
                                            {"conclusion": "skipped"}]})
        self.server.add("GET", "/repos/example-owner/demo-public/pulls/8", body={
            "merged": False, "merged_at": None, "head": {"sha": "bbb"},
        })
        self.server.add("GET", "/repos/example-owner/demo-public/commits/bbb/status",
                        body={"state": "success"})
        self.server.add("GET", "/repos/example-owner/demo-public/commits/bbb/check-runs",
                        body={"check_runs": [{"conclusion": "failure"}]})
        self.gh = GitHub("gh-token", base_url=base)

    def _seed_deploys(self):
        self.server.add("GET", "/repos/example-owner/demo-public/actions/workflows",
                        body={"workflows": [
                            {"id": 9, "name": "deploy-test",
                             "path": ".github/workflows/deploy-test.yml"}]})
        self.server.add("GET", "/repos/example-owner/demo-public",
                        body={"default_branch": "main"})
        self.server.add("GET", "/repos/example-owner/demo-public/actions/workflows/9/runs",
                        body={"workflow_runs": [
                            {"conclusion": "success",
                             "updated_at": "2026-09-23T11:00:00Z"},
                            {"conclusion": "failure",
                             "updated_at": "2026-09-23T12:00:00Z"},
                        ]})
        self.server.add("GET", "/repos/example-owner/demo-public/deployments",
                        body=[{"id": 5,
                               "url": self.server.base_url + "/repos/example-owner/demo-public/deployments/5"}])
        self.server.add(
            "GET", "/repos/example-owner/demo-public/deployments/5/statuses",
            body=[{"state": "in_progress", "created_at": "2026-09-23T11:00:00Z"},
                  {"state": "success", "created_at": "2026-09-23T11:05:00Z"}])

    def test_collector_never_reads_a_pr_of_another_repo(self):
        from opl.conductor.collect import collect_github
        from opl.conductor.state import Item, Project

        calls = []

        class GH:
            def pull_request(self, url):
                calls.append(url)
                raise AssertionError("must not be read")

            def prod_deploys(self, project):
                return []

        project = Project(key="p", op_id=1, repo="example-owner/demo-public",
                          visibility="Public", has_test_env=False,
                          test_signal=None, prod_signal=None)
        items = {5: Item(id=5, project="p", type="Task", status="In review",
                         status_since="2026-09-20T12:00:00Z",
                         pr_url="https://github.com/other/secret/pull/2")}
        prs, _ = collect_github(GH(), {"p": project}, items)
        self.assertEqual((prs, calls), ({}, []))

    def test_deploy_filters_go_in_the_query_string(self):
        # PR #6 review: a GET body is ignored, so filters must be query
        # parameters or other branches/environments count as deploys.
        self._seed_deploys()
        self.gh.test_deploys(make_project())
        self.gh.prod_deploys(make_project())
        runs = [r for r in self.server.requests if r["path"].endswith("/runs")]
        deps = [r for r in self.server.requests if r["path"].endswith("/deployments")]
        self.assertIn("branch=main", runs[0]["query"])
        self.assertIn("status=success", runs[0]["query"])
        self.assertIn("environment=production", deps[0]["query"])
        self.assertTrue(all(r["body"] is None for r in runs + deps))

    def test_merged_pr(self):
        pr = self.gh.pull_request(PR_MERGED)
        self.assertTrue(pr.merged)
        self.assertEqual(pr.merged_at,
                         datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc))
        self.assertTrue(pr.checks_green)

    def test_open_pr_with_failing_check(self):
        pr = self.gh.pull_request(PR_OPEN)
        self.assertFalse(pr.merged)
        self.assertIsNone(pr.merged_at)
        self.assertFalse(pr.checks_green)
        auths = {r["headers"].get("Authorization")
                 for r in self.server.requests}
        self.assertEqual(auths, {"Bearer gh-token"})

    def test_token_not_in_errors(self):
        self.server.add("GET", "/repos/o/r/pulls/1", status=500,
                        body={"message": "boom"})
        with self.assertRaises(ApiError) as ctx:
            self.gh.pull_request("https://github.com/o/r/pull/1")
        self.assertNotIn("gh-token", str(ctx.exception))

    def test_reflected_auth_in_error_body_is_scrubbed(self):
        # TH.2: error bodies echoing the Authorization header or the token.
        def reflect(method, path, query, body, headers):
            return 500, {"echo": "Authorization: %s" % headers.get("Authorization"),
                         "raw": "gh-token"}

        self.server.add("GET", "/repos/o/r/pulls/2", handler=reflect)
        with self.assertRaises(ApiError) as ctx:
            self.gh.pull_request("https://github.com/o/r/pull/2")
        self.assertNotIn("gh-token", str(ctx.exception))

    def test_secret_straddling_the_cut_leaves_no_prefix(self):
        # TH.20 (Codex E3): scrub the whole error body, then cut to 500.
        def reflect(method, path, query, body, headers):
            return 500, {"m": "x" * 490 + "gh-token" + "y" * 100}

        self.server.add("GET", "/repos/o/r/pulls/3", handler=reflect)
        with self.assertRaises(ApiError) as ctx:
            self.gh.pull_request("https://github.com/o/r/pull/3")
        self.assertNotIn("gh-t", ctx.exception.message)

    def test_merge_calls_with_squash(self):
        self.server.add("PUT", "/repos/example-owner/demo-public/pulls/8/merge",
                        body={"merged": True})
        self.assertTrue(self.gh.merge(PR_OPEN))
        (call,) = [r for r in self.server.requests if r["method"] == "PUT"]
        self.assertEqual(call["body"], {"merge_method": "squash"})

    def test_workflow_run_deploys(self):
        self._seed_deploys()
        deploys = self.gh.test_deploys(make_project())
        self.assertEqual(len(deploys), 1)
        self.assertEqual((deploys[0].project, deploys[0].target),
                         ("demo-public", "test"))
        self.assertEqual(deploys[0].at,
                         datetime(2026, 9, 23, 11, 0, tzinfo=timezone.utc))

    def test_environment_deploys(self):
        self._seed_deploys()
        deploys = self.gh.prod_deploys(make_project())
        self.assertEqual(len(deploys), 1)
        self.assertEqual((deploys[0].project, deploys[0].target),
                         ("demo-public", "production"))

    def test_collect_github(self):
        self._seed_deploys()
        items = {
            1: Item(id=1, project="demo-public", type="Task", status="In review",
                    status_since=self.now(), pr_url=PR_MERGED),
            2: Item(id=2, project="demo-public", type="Task", status="Draft",
                    status_since=self.now()),
        }
        prs, deploys = collect_github(
            self.gh, {"demo-public": make_project()}, items)
        self.assertEqual(set(prs), {PR_MERGED})
        self.assertTrue(prs[PR_MERGED].merged)
        targets = sorted(d.target for d in deploys)
        self.assertEqual(targets, ["production", "test"])

    def now(self):
        return datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
