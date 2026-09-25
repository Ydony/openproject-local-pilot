#!/usr/bin/env python3
"""Tests: required checks, merge SHA, PR head record (TH.8)."""

import unittest

from opl.github import GitHub
from tests.fakes.http_fake import FakeServer

OWNER = "example-owner"
REPO = "demo"
PR = "https://github.com/%s/%s/pull/8" % (OWNER, REPO)


class ChecksHarness(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.gh = GitHub("gh-token", base_url=self.server.base_url)

    def _pr(self, sha="ccc", repo_full=None):
        self.server.add(
            "GET", "/repos/%s/%s/pulls/8" % (OWNER, REPO),
            body={"merged": False, "merged_at": None,
                  "head": {"sha": sha, "ref": "feature-branch",
                           "repo": {"full_name": repo_full or
                                    "%s/%s" % (OWNER, REPO)}}})

    def _protection(self, contexts):
        self.server.add(
            "GET", "/repos/%s/%s/branches/main/protection" % (OWNER, REPO),
            body={"required_status_checks": {"contexts": contexts}})

    def _runs(self, sha, runs):
        def pages(method, path, query, body, headers):
            return 200, {"check_runs": runs, "total_count": len(runs)}

        self.server.add(
            "GET", "/repos/%s/%s/commits/%s/check-runs" % (OWNER, REPO, sha),
            handler=pages)

    def _status(self, sha, state, statuses=()):
        """`statuses`: contexts (taking the combined `state`) or
        (context, state) pairs, as GitHub gives each status its own."""
        pairs = [c if isinstance(c, tuple) else (c, state) for c in statuses]
        self.server.add(
            "GET", "/repos/%s/%s/commits/%s/status" % (OWNER, REPO, sha),
            body={"state": state, "statuses": [
                {"context": c, "state": st} for c, st in pairs]})

    def _protection_checks(self, checks):
        self.server.add(
            "GET", "/repos/%s/%s/branches/main/protection" % (OWNER, REPO),
            body={"required_status_checks": {
                "contexts": [c for c, _ in checks],
                "checks": [{"context": c, "app_id": a} for c, a in checks]}})


class ActionsOnlyTests(ChecksHarness):
    def test_actions_only_green_without_statuses(self):
        # No legacy statuses at all (404): green check runs are enough.
        self._pr()
        self._runs("ccc", [{"name": "ci", "conclusion": "success"}])
        pr = self.gh.pull_request(PR)
        self.assertTrue(pr.checks_green)

    def test_failing_required_check_blocks(self):
        self._pr()
        self._protection(["ci"])
        self._runs("ccc", [{"name": "ci", "conclusion": "failure"},
                           {"name": "lint", "conclusion": "success"}])
        self._status("ccc", "failure", ["ci", "lint"])
        pr = self.gh.pull_request(PR)
        self.assertFalse(pr.checks_green)

    def test_required_pass_with_noisy_extras(self):
        self._pr()
        self._protection(["lint"])
        self._runs("ccc", [{"name": "lint", "conclusion": "success"},
                           {"name": "flaky", "conclusion": "failure"}])
        self._status("ccc", "failure", [("lint", "success"), ("flaky", "failure")])
        pr = self.gh.pull_request(PR)
        self.assertTrue(pr.checks_green)

    def test_zero_statuses_is_not_pending(self):
        self._pr()
        self._runs("ccc", [{"name": "ci", "conclusion": "success"}])
        self._status("ccc", "pending", [])
        pr = self.gh.pull_request(PR)
        self.assertTrue(pr.checks_green)

    def test_check_runs_paginated(self):
        self._pr()
        first = [{"name": "r%d" % i, "conclusion": "success"}
                 for i in range(100)]
        second = [{"name": "r100", "conclusion": "failure"}]

        def pages(method, path, query, body, headers):
            if "page=2" in query:
                return 200, {"check_runs": second, "total_count": 101}
            return 200, {"check_runs": first, "total_count": 101}

        self.server.add(
            "GET", "/repos/%s/%s/commits/ccc/check-runs" % (OWNER, REPO),
            handler=pages)
        self._status("ccc", "success", [])
        pr = self.gh.pull_request(PR)
        self.assertFalse(pr.checks_green)

    def test_nothing_at_all_is_not_green(self):
        self._pr()
        self._runs("ccc", [])
        self._status("ccc", "pending", [])
        pr = self.gh.pull_request(PR)
        self.assertFalse(pr.checks_green)


class EvidenceTests(ChecksHarness):
    """Codex TH.RE E1: green needs evidence, and nothing outvotes a failure."""

    def test_failing_legacy_status_is_not_outvoted_without_protection(self):
        self._pr()
        self._runs("ccc", [{"name": "ci", "conclusion": "success"}])
        self._status("ccc", "failure", [("legacy-ci", "failure")])
        self.assertFalse(self.gh.pull_request(PR).checks_green)

    def test_empty_required_list_needs_some_evidence(self):
        self._pr()
        self._protection([])
        self._runs("ccc", [])
        self._status("ccc", "pending", [])
        self.assertFalse(self.gh.pull_request(PR).checks_green)

    def test_empty_required_list_falls_back_to_every_signal(self):
        self._pr()
        self._protection([])
        self._runs("ccc", [{"name": "ci", "conclusion": "success"},
                           {"name": "lint", "conclusion": "failure"}])
        self.assertFalse(self.gh.pull_request(PR).checks_green)

    def test_required_check_missing_is_not_green(self):
        self._pr()
        self._protection(["ci"])
        self._runs("ccc", [{"name": "other", "conclusion": "success"}])
        self.assertFalse(self.gh.pull_request(PR).checks_green)

    def test_required_check_from_wrong_app_does_not_count(self):
        self._pr()
        self._protection_checks([("ci", 15368)])
        self._runs("ccc", [{"name": "ci", "conclusion": "success",
                            "app": {"id": 999}}])
        self.assertFalse(self.gh.pull_request(PR).checks_green)
        self._runs("ccc", [{"name": "ci", "conclusion": "success",
                            "app": {"id": 15368}}])
        self.assertTrue(self.gh.pull_request(PR).checks_green)

    def test_required_context_failing_as_status_blocks(self):
        self._pr()
        self._protection(["ci"])
        self._runs("ccc", [{"name": "ci", "conclusion": "success"}])
        self._status("ccc", "failure", [("ci", "failure")])
        self.assertFalse(self.gh.pull_request(PR).checks_green)

    def test_in_progress_run_is_not_green(self):
        self._pr()
        self._runs("ccc", [{"name": "ci", "status": "in_progress",
                            "conclusion": None}])
        self.assertFalse(self.gh.pull_request(PR).checks_green)


class StatusPagingTests(ChecksHarness):
    """TH.23 (Codex F6): legacy statuses are read across every page."""

    def _paged_status(self, pages, total):
        def handler(method, path, query, body, headers):
            page = int(dict(p.split("=") for p in query.split("&")).get("page", "1"))
            batch = pages[page - 1] if page <= len(pages) else []
            return 200, {"state": "pending", "total_count": total,
                         "statuses": [{"context": c, "state": st}
                                      for c, st in batch]}

        self.server.add("GET", "/repos/%s/%s/commits/ccc/status" % (OWNER, REPO),
                        handler=handler)

    def test_failing_context_on_a_later_page_blocks(self):
        self._pr()
        self._runs("ccc", [{"name": "ci", "conclusion": "success"}])
        first = [("ctx-%d" % i, "success") for i in range(100)]
        self._paged_status([first, [("late", "failure")]], 101)
        self.assertFalse(self.gh.pull_request(PR).checks_green)

    def test_required_context_on_a_later_page_is_found(self):
        self._pr()
        self._protection(["late"])
        first = [("ctx-%d" % i, "success") for i in range(100)]
        self._paged_status([first, [("late", "success")]], 101)
        self.assertTrue(self.gh.pull_request(PR).checks_green)

    def test_short_read_is_not_green(self):
        # GitHub says 150 statuses but stops after 100: incomplete.
        self._pr()
        self._runs("ccc", [{"name": "ci", "conclusion": "success"}])
        first = [("ctx-%d" % i, "success") for i in range(100)]
        self._paged_status([first, []], 150)
        self.assertFalse(self.gh.pull_request(PR).checks_green)


class MergeShaTests(ChecksHarness):
    def test_merge_sends_expected_sha(self):
        self.server.add(
            "PUT", "/repos/%s/%s/pulls/8/merge" % (OWNER, REPO),
            body={"merged": True})
        self.assertTrue(self.gh.merge(PR, sha="ccc"))
        (call,) = [r for r in self.server.requests if r["method"] == "PUT"]
        self.assertEqual(call["body"],
                         {"merge_method": "squash", "sha": "ccc"})

    def test_merge_without_sha_omits_it(self):
        self.server.add(
            "PUT", "/repos/%s/%s/pulls/8/merge" % (OWNER, REPO),
            body={"merged": True})
        self.assertTrue(self.gh.merge(PR))
        (call,) = [r for r in self.server.requests if r["method"] == "PUT"]
        self.assertEqual(call["body"], {"merge_method": "squash"})

    def test_repo_private_true_and_false(self):
        self.server.add("GET", "/repos/%s/%s" % (OWNER, REPO),
                        body={"private": True})
        self.assertTrue(self.gh.repo_private(OWNER, REPO))
        self.server.add("GET", "/repos/%s/%s" % (OWNER, REPO),
                        body={"private": False})
        self.assertFalse(self.gh.repo_private(OWNER, REPO))

    def test_repo_private_missing_field_counts_as_private(self):
        for body in ({}, {"private": None}, {"private": "false"}):
            with self.subTest(body=body):
                self.server.add("GET", "/repos/%s/%s" % (OWNER, REPO), body=body)
                self.assertTrue(self.gh.repo_private(OWNER, REPO))

    def test_repo_private_lookup_failure_raises(self):
        from opl.openproject import ApiError

        self.server.add("GET", "/repos/%s/%s" % (OWNER, REPO),
                        status=500, body={"message": "boom"})
        with self.assertRaises(ApiError):
            self.gh.repo_private(OWNER, REPO)

    def test_moved_head_rejected(self):
        # The head moved between check and merge: GitHub refuses (422)
        # and the client surfaces the failure instead of merging blindly.
        from opl.openproject import ApiError

        def reject(method, path, query, body, headers):
            self.assertEqual(body.get("sha"), "old-sha")
            return 422, {"message": "Head was modified"}

        self.server.add("PUT", "/repos/%s/%s/pulls/8/merge" % (OWNER, REPO),
                        handler=reject)
        with self.assertRaises(ApiError):
            self.gh.merge(PR, sha="old-sha")


class HeadRecordTests(ChecksHarness):
    def test_head_sha_and_repo_kept(self):
        self._pr(sha="ccc", repo_full="fork-owner/demo")
        self._runs("ccc", [{"name": "ci", "conclusion": "success"}])
        self._status("ccc", "success", ["ci"])
        pr = self.gh.pull_request(PR)
        self.assertEqual(pr.head_sha, "ccc")
        self.assertEqual(pr.head_repo, "fork-owner/demo")

    def test_same_repo_head_when_no_fork(self):
        self._pr(sha="ccc")
        self._runs("ccc", [{"name": "ci", "conclusion": "success"}])
        self._status("ccc", "success", ["ci"])
        pr = self.gh.pull_request(PR)
        self.assertEqual(pr.head_repo, "%s/%s" % (OWNER, REPO))


if __name__ == "__main__":
    unittest.main(verbosity=2)
