#!/usr/bin/env python3
"""Tests for review runs and test runs: packets, moves, idempotency."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from opl.conductor.spark.runner import SparkRunner
from opl.conductor.state import Item, Project, World
from opl.github import GitHub
from opl.openproject import Client
from opl.settings import Conductor, OpenProject
from opl.settings import Project as SettingsProject
from opl.settings import Runner, Settings
from tests.fakes.http_fake import FakeServer
from tests.gitfixture import point_origin

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKES = os.path.join(REPO, "tests", "fakes")


def make_settings(repo_path, state_dir, worker="worker_ok.py"):
    return Settings(
        openproject=OpenProject("http://127.0.0.1:8080", "OPL_TOKEN_ADMIN", "admin"),
        tokens={"spark": "OPL_TOKEN_SPARK"},
        github_token_env="OPL_GITHUB_TOKEN",
        users_email_domain="example.invalid",
        conductor=Conductor(False, 60, state_dir),
        runner=Runner((sys.executable, os.path.join(FAKES, worker)), 2,
                      {"S": 1, "M": 2, "L": 3, "review": 1, "test": 1, "stall": 30}),
        projects=(
            SettingsProject("demo", "Demo", "example-owner/demo", "Public",
                            True, "http://127.0.0.1:3001", None, None,
                            repo_path, "main"),
        ),
    )


def review_world():
    projects = {
        "demo": Project(key="demo", op_id=1, repo="example-owner/demo",
                        visibility="Public", has_test_env=True,
                        test_signal=None, prod_signal=None),
    }
    items = {
        1: Item(id=1, project="demo", type="Epic", status="Open",
                 status_since="2026-09-20T12:00:00Z"),
        2: Item(id=2, project="demo", type="Feature", status="In test",
                 status_since="2026-09-20T12:00:00Z", parent_id=1,
                 test_result=None),
        5: Item(id=5, project="demo", type="Task", status="In review",
                 status_since="2026-09-20T12:00:00Z", parent_id=2,
                 assignee="codex", reviewer="spark", size="S", risk="Low",
                 pr_url="https://github.com/example-owner/demo/pull/9",
                 review_result=None),
    }
    return World(now="2026-09-24T12:00:00Z", projects=projects, items=items,
                 pull_requests={})


def review_only_world():
    """Review candidate only: the feature is not In test."""
    world = review_world()
    import dataclasses

    items = dict(world.items)
    # A task in review belongs to a feature being built (not In test).
    items[2] = dataclasses.replace(items[2], status="Building")
    return dataclasses.replace(world, items=items)


def test_only_world():
    """Test candidate only: the task is not awaiting review."""
    world = review_world()
    import dataclasses

    items = dict(world.items)
    items[5] = dataclasses.replace(items[5], status="In progress")
    return dataclasses.replace(world, items=items)


def settle(runner, timeout=120):
    """Wait until all background runs finish (records stay for reap)."""
    deadline = time.monotonic() + timeout
    while True:
        with runner._lock:
            pending = [r for r in runner._active.values()
                       if not r["done"].is_set()]
        if not pending:
            return
        assert time.monotonic() < deadline, "runs did not finish"
        time.sleep(0.2)


def mark(world, iid, **changes):
    """Return a world with one item replaced (fresh collect after conclude)."""
    import dataclasses

    items = dict(world.items)
    items[iid] = dataclasses.replace(items[iid], **changes)
    return dataclasses.replace(world, items=items)


class ReviewHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opl-rev-")
        self.repo = os.path.join(self.tmp, "repo")
        os.mkdir(self.repo)
        subprocess.run(["git", "init", "-q", "-b", "main", self.repo],
                       check=True, timeout=60)
        subprocess.run(["git", "-C", self.repo, "config", "user.email", "t@t"],
                       check=True, timeout=60)
        subprocess.run(["git", "-C", self.repo, "config", "user.name", "t"],
                       check=True, timeout=60)
        with open(os.path.join(self.repo, "README.md"), "w") as fh:
            fh.write("base\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True, timeout=60)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "base"],
                       check=True, timeout=60)
        subprocess.run(
            ["git", "-C", self.repo, "checkout", "-qb", "feature-branch"], check=True,
            timeout=60)
        with open(os.path.join(self.repo, "work.txt"), "w") as fh:
            fh.write("work\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True, timeout=60)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "work"],
                       check=True, timeout=60)
        subprocess.run(["git", "-C", self.repo, "checkout", "-q", "main"],
                       check=True, timeout=60)
        self.bare = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", self.bare],
                       check=True, timeout=60)
        point_origin(self.repo, self.bare, "example-owner/demo")
        subprocess.run(["git", "-C", self.repo, "push", "-q", "origin",
                        "main", "feature-branch"], check=True, timeout=60,
                       env=dict(os.environ, GIT_TERMINAL_PROMPT="0",
                                GCM_INTERACTIVE="never"))
        out = subprocess.run(["git", "-C", self.repo, "rev-parse",
                              "feature-branch"],
                             capture_output=True, text=True, timeout=60,
                             check=True)
        self.head_sha = out.stdout.strip()
        self.server = FakeServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        base = self.server.base_url
        self.server.add("GET", "/api/v3/statuses", body={"_embedded": {"elements": [
            {"id": 31, "name": "In progress"}, {"id": 32, "name": "In review"},
            {"id": 33, "name": "Blocked"}, {"id": 34, "name": "Approved"},
            {"id": 35, "name": "Proposed"}, {"id": 21, "name": "Open"},
            {"id": 22, "name": "In test"}]}})
        self.server.add("GET", "/repos/example-owner/demo",
                        body={"private": False})
        self.server.add("GET", "/api/v3/types", body={"_embedded": {"elements": [
            {"id": 13, "name": "Task"}, {"id": 12, "name": "Feature"},
            {"id": 11, "name": "Epic"}]}})
        self.server.add("GET", "/api/v3/work_packages/schemas/1-13", body={
            "customField60": {"name": "Review result", "location": "_links",
                              "_embedded": {"allowedValues": [
                                  {"id": 70, "value": "Pass"},
                                  {"id": 71, "value": "Changes requested"}]}},
            "customField62": {"type": "Link", "name": "PR link"}})
        # The task's live PR link (TH.22: part of the run's authorization);
        # tests that plan a run with another link set this to match.
        self.live_pr_link = "https://github.com/example-owner/demo/pull/9"
        self.server.add("GET", "/api/v3/work_packages/schemas/1-12", body={
            "customField61": {"name": "Test result", "location": "_links",
                              "_embedded": {"allowedValues": [
                                  {"id": 80, "value": "Pass"},
                                  {"id": 81, "value": "Fail"}]}}})
        self.patches = []

        def patch_wp(method, path, query, body, headers):
            self.patches.append((path, body))
            return 200, {}

        self.server.add("PATCH", "/api/v3/work_packages/5", handler=patch_wp)
        self.server.add("PATCH", "/api/v3/work_packages/2", handler=patch_wp)
        self.posts = []

        def post_any(method, path, query, body, headers):
            self.posts.append((path, body))
            return 201, {}

        self.server.add("POST", "/api/v3/work_packages/5/activities", handler=post_any)
        self.server.add("POST", "/api/v3/work_packages/2/activities", handler=post_any)
        self.server.add("GET", "/api/v3/work_packages/1", body={
            "id": 1, "subject": "Epic", "description": "",
            "lockVersion": 1,
            "_links": {
                "status": {"href": "/api/v3/statuses/21"},
                "type": {"href": "/api/v3/types/11"},
                "project": {"href": "%s/api/v3/projects/1"
                                      % self.server.base_url}}})
        self.server.add("GET", "/api/v3/work_packages/5", handler=lambda *a: (200, {
            "id": 5, "subject": "Build thing", "description": "Do it",
            "lockVersion": 1, "customField62": self.live_pr_link,
            "_links": {
                "status": {"href": "/api/v3/statuses/32"},
                "type": {"href": "/api/v3/types/13"},
                "project": {"href": "%s/api/v3/projects/1"
                                      % self.server.base_url},
                "parent": {"href": "%s/api/v3/work_packages/2"
                                     % self.server.base_url}}}))
        self.server.add("GET", "/api/v3/work_packages/2", body={
            "id": 2, "subject": "Feature", "description": "Why: x",
            "lockVersion": 1,
            "_links": {
                "status": {"href": "/api/v3/statuses/34"},
                "type": {"href": "/api/v3/types/12"},
                "project": {"href": "%s/api/v3/projects/1"
                                      % self.server.base_url}}})
        self.server.add("GET", "/repos/example-owner/demo/pulls/9", body={
            "merged": False, "merged_at": None,
            "head": {"sha": self.head_sha, "ref": "feature-branch",
                     "repo": {"full_name": "example-owner/demo"}},
            "base": {"ref": "main", "repo": {"full_name": "example-owner/demo"}}})
        self.server.add(
            "GET", "/repos/example-owner/demo/commits/%s/check-runs"
            % self.head_sha,
            body={"check_runs": [{"name": "ci", "conclusion": "success"}],
                  "total_count": 1})
        self.server.add(
            "GET", "/repos/example-owner/demo/commits/%s/status"
            % self.head_sha,
            body={"state": "success", "statuses": [{"context": "ci"}]})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _runner(self, worker="worker_ok.py"):
        from opl.conductor.spark.supervisor import Slots

        settings = make_settings(self.repo, os.path.join(self.tmp, "state"), worker)
        op = Client(self.server.base_url, "spark-token")
        gh = GitHub("gh-token", self.server.base_url)
        return SparkRunner(settings, None, op, gh, Slots(2))

    def _review_packets(self):
        packets = []
        for root, _dirs, files in os.walk(os.path.join(self.tmp, "state", "packets")):
            for name in sorted(files):
                if name.startswith("review-"):
                    with open(os.path.join(root, name), encoding="utf-8") as fh:
                        packets.append(fh.read())
        return packets


class ReviewPassTests(ReviewHarness):
    def test_pass_sets_result(self):
        runner = self._runner("worker_review_pass.py")
        world = review_only_world()
        runner.tick(world)
        settle(runner)
        actions = runner.tick(mark(world, 5, review_result="Pass"))
        self.assertTrue(any("review task 5: Pass" in a for a in actions))
        review_patches = [b for p, b in self.patches
                          if p == "/api/v3/work_packages/5"]
        self.assertEqual(len(review_patches), 1)
        self.assertEqual(review_patches[0]["_links"]["customField60"],
                         {"href": "/api/v3/custom_options/70"})
        # TH.5: the reviewed SHA is recorded, and before the Pass lands.
        writes = [(r["method"], r["path"], r["body"]) for r in self.server.requests
                  if r["method"] in ("POST", "PATCH")
                  and r["path"].startswith("/api/v3/work_packages/5")]
        first_post = next(i for i, w in enumerate(writes) if w[0] == "POST")
        first_patch = next(i for i, w in enumerate(writes) if w[0] == "PATCH")
        self.assertLess(first_post, first_patch)
        self.assertIn("\nreviewed: %s" % self.head_sha,
                      writes[first_post][2]["comment"]["raw"])

    def _never_reviewed(self, world):
        """A foreign PR link: enforce flags it, and the runner starts
        nothing (no PR read, no fetch, no tree, no packet)."""
        from opl.conductor.rules.enforce import violations

        self.assertIn("is not a pull request of", violations(world)[5])
        self.state = os.path.join(self.tmp, "state")
        runner = self._runner("worker_review_pass.py")
        runner.tick(world)
        settle(runner)
        runner.tick(world)
        self.assertEqual([r for r in self.server.requests
                          if "/pulls/" in r["path"]], [])
        worktrees = os.path.join(self.state, "worktrees")
        self.assertFalse(os.path.isdir(worktrees) and os.listdir(worktrees))
        packets = os.path.join(self.state, "packets")
        self.assertFalse(os.path.isdir(packets)
                         and any(n.startswith("review-") for n in os.listdir(packets)))

    def _review_is_refused(self, world, reason):
        """No fetch, no worktree, no packet, no worker; Blocked with why."""
        self.state = os.path.join(self.tmp, "state")
        runner = self._runner("worker_review_pass.py")
        runner.tick(world)
        settle(runner)
        actions = runner.tick(world)
        self.assertTrue(any("not started" in a for a in actions), actions)
        worktrees = os.path.join(self.state, "worktrees")
        self.assertFalse(os.path.isdir(worktrees)
                         and any("review" in d for d in os.listdir(worktrees)))
        packets = os.path.join(self.state, "packets")
        self.assertFalse(os.path.isdir(packets)
                         and any(n.startswith("review-") for n in os.listdir(packets)))
        self.assertIn({"href": "/api/v3/statuses/33"},
                      [b.get("_links", {}).get("status") for p, b in self.patches
                       if p == "/api/v3/work_packages/5"])
        comments = [b["comment"]["raw"] for p, b in self.posts
                    if p == "/api/v3/work_packages/5/activities"]
        self.assertTrue(any("Not started (review run)" in c and reason in c
                            for c in comments), comments)

    def test_pr_link_to_another_repo_is_never_read_or_fetched(self):
        # TH.23 (Codex F1): the PR link is an editable field. A link to a
        # different (possibly private) repo must never be read with the
        # GitHub token, fetched, or shown to Spark.
        # Since the PR #6 review the conductor's enforce rule already
        # blocks such a task, so the runner never picks it up at all.
        self.live_pr_link = "https://github.com/other-owner/secret/pull/3"
        world = mark(review_only_world(), 5, pr_url=self.live_pr_link)
        self._never_reviewed(world)
        self.assertEqual([r for r in self.server.requests
                          if "other-owner" in r["path"]], [])

    def test_pr_link_on_another_host_is_refused(self):
        self.live_pr_link = "https://github.example.invalid/example-owner/demo/pull/9"
        world = mark(review_only_world(), 5, pr_url=self.live_pr_link)
        self._never_reviewed(world)

    SCHEMA_WITHOUT_PR_LINK = {
        "customField60": {"name": "Review result", "location": "_links",
                          "_embedded": {"allowedValues": [
                              {"id": 70, "value": "Pass"},
                              {"id": 71, "value": "Changes requested"}]}}}

    def test_review_refused_when_the_type_has_no_pr_link_field(self):
        # Codex final3 D1: an unreadable PR link is not "unchanged".
        self.server.add("GET", "/api/v3/work_packages/schemas/1-13",
                        body=self.SCHEMA_WITHOUT_PR_LINK)
        self._review_is_refused(review_only_world(), "has no PR link field")

    def test_review_refused_when_the_task_has_no_pr_link(self):
        self.live_pr_link = None
        self._review_is_refused(review_only_world(), "has no PR link")

    def test_review_refused_when_the_field_vanishes_before_spawn(self):
        import opl.conductor.spark.runner as runner_mod

        real = runner_mod.checkout_pr_head

        def checkout_then_drop_field(*args, **kwargs):
            tree = real(*args, **kwargs)
            self.server.add("GET", "/api/v3/work_packages/schemas/1-13",
                            body=self.SCHEMA_WITHOUT_PR_LINK)
            return tree

        with mock.patch.object(runner_mod, "checkout_pr_head",
                               checkout_then_drop_field):
            self._review_is_refused(review_only_world(), "has no PR link field")

    def test_fork_head_is_refused(self):
        self.server.add("GET", "/repos/example-owner/demo/pulls/9", body={
            "merged": False, "merged_at": None,
            "head": {"sha": self.head_sha, "ref": "feature-branch",
                     "repo": {"full_name": "someone/demo-fork"}},
            "base": {"ref": "main", "repo": {"full_name": "example-owner/demo"}}})
        self._review_is_refused(review_only_world(), "fork or unknown source")

    def test_unknown_head_or_base_repo_is_refused(self):
        self.server.add("GET", "/repos/example-owner/demo/pulls/9", body={
            "merged": False, "merged_at": None,
            "head": {"sha": self.head_sha, "ref": "feature-branch", "repo": None},
            "base": {"ref": "main"}})
        self._review_is_refused(review_only_world(), "PR base repo is 'unknown'")

    def test_result_resets_the_attempt_count(self):
        # TH.16: only runs that end with no result count. One failure
        # then a concluded result leaves the count at zero.
        from opl.conductor.spark.runner import read_attempts

        def attempts():
            return read_attempts(os.path.join(self.tmp, "state"))

        runner = self._runner()
        world = review_only_world()
        runner.tick(world)
        settle(runner)
        runner.tick(world)  # concludes failure 1 (count 1), starts retry
        settle(runner)
        self.assertEqual(attempts(), {("review", 5): 1})
        runner._conclude_check({"kind": "review-changes",
                                "item": world.items[5],
                                "notes": "n", "to_status": 31}, world)
        self.assertEqual(attempts(), {})

    def test_ceiling_rearms_after_a_human_move(self):
        # TH.21: blocked at the ceiling, then a person moves the task:
        # status_since is newer than the ceiling hit, so it runs again.
        import dataclasses
        import datetime
        import json

        from opl.conductor.spark.runner import read_attempts

        state = os.path.join(self.tmp, "state")
        runner = self._runner()
        world = review_only_world()
        runner.tick(world)
        settle(runner)
        runner.tick(world)
        settle(runner)
        actions = runner.tick(world)
        self.assertTrue(any("needs lead" in a for a in actions))
        with open(os.path.join(state, "attempts.json"),
                  encoding="utf-8") as fh:
            hit = json.load(fh)["ceiling:review:5"]["at"]
        moved = (datetime.datetime.fromisoformat(hit)
                 + datetime.timedelta(hours=1)).isoformat()
        items = dict(world.items)
        items[5] = dataclasses.replace(items[5], status_since=moved)
        world = dataclasses.replace(world, items=items)
        runner.tick(world)
        settle(runner)
        actions = runner.tick(world)
        self.assertTrue(any("failed" in a for a in actions))
        self.assertFalse(any("needs lead" in a for a in actions))
        self.assertEqual(read_attempts(state), {("review", 5): 1})

    def test_ceiling_stays_without_a_move(self):
        # TH.21: same status_since as the ceiling hit — still blocked,
        # no new run.
        from opl.conductor.spark.runner import read_attempts

        runner = self._runner()
        world = review_only_world()
        runner.tick(world)
        settle(runner)
        runner.tick(world)
        settle(runner)
        runner.tick(world)
        settle(runner)
        before = read_attempts(os.path.join(self.tmp, "state"))
        actions = runner.tick(world)
        self.assertTrue(any("needs lead" in a for a in actions))
        self.assertEqual(read_attempts(os.path.join(self.tmp, "state")),
                         before)

    def test_missing_final_line_counts_as_failed(self):
        # worker_ok completes but prints no OPL-REVIEW line.
        runner = self._runner()
        world = review_only_world()
        runner.tick(world)
        settle(runner)
        actions = runner.tick(mark(world, 5, review_result="Pass"))
        self.assertTrue(any("failed" in a for a in actions))
        self.assertEqual(
            [b for p, b in self.patches if p == "/api/v3/work_packages/5"], [])

    def test_ceiling_blocks_after_one_retry_and_survives_restart(self):
        # TH.10: a review that keeps failing gets one retry, then
        # Blocked with "needs lead" — even for a fresh runner process.
        # attempts.json proves exactly two failed runs and no third.
        from opl.conductor.spark.runner import read_attempts

        def attempts():
            return read_attempts(os.path.join(self.tmp, "state"))

        world = review_only_world()
        runner = self._runner()
        runner.tick(world)
        settle(runner)
        runner.tick(world)  # concludes failure 1, starts the retry
        settle(runner)
        actions = runner.tick(world)  # concludes failure 2 → ceiling
        blocked = [b for p, b in self.patches
                   if b.get("_links", {}).get("status", {}).get("href", "")
                   .endswith("/33")]
        self.assertEqual(len(blocked), 1)
        comments = [b for p, b in self.posts if p.endswith("/activities")]
        self.assertTrue(any("needs lead" in b["comment"]["raw"]
                            for b in comments))
        self.assertTrue(any("needs lead" in a for a in actions))
        self.assertEqual(attempts(), {("review", 5): 2})
        # A restarted conductor reads the same ceiling: no third run.
        fresh = self._runner()
        fresh.tick(review_only_world())
        settle(fresh)
        fresh.tick(review_only_world())
        self.assertEqual(attempts(), {("review", 5): 2})
        blocked = [b for p, b in self.patches
                   if b.get("_links", {}).get("status", {}).get("href", "")
                   .endswith("/33")]
        self.assertTrue(len(blocked) >= 1)

    def test_corrupt_attempts_file_does_not_stop_runs(self):
        state = os.path.join(self.tmp, "state")
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "attempts.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("not json{")
        runner = self._runner("worker_review_pass.py")
        world = review_only_world()
        runner.tick(world)
        settle(runner)
        actions = runner.tick(mark(world, 5, review_result="Pass"))
        self.assertTrue(any("review task 5: Pass" in a for a in actions))

    def test_failed_review_keeps_its_worktree_and_says_where(self):
        # TH.4: a crashed review run keeps its scratch tree as evidence and
        # the failure comment points at it; a clean pass removes its tree.
        runner = self._runner("worker_fail.py")
        world = review_only_world()
        runner.tick(world)
        settle(runner)
        runner.tick(mark(world, 5, review_result="Pass"))
        wt_root = os.path.join(self.tmp, "state", "worktrees")
        kept = [d for _r, dirs, _f in os.walk(wt_root) for d in dirs
                if d.startswith("review-5-")]
        self.assertTrue(kept, "failed review tree was removed")
        comments = [r["body"]["comment"]["raw"] for r in self.server.requests
                    if r["method"] == "POST" and r["path"].endswith("/activities")]
        self.assertTrue(any("kept worktree at" in c for c in comments), comments)

    def test_clean_pass_removes_its_worktree(self):
        runner = self._runner("worker_review_pass.py")
        world = review_only_world()
        runner.tick(world)
        settle(runner)
        wt_root = os.path.join(self.tmp, "state", "worktrees")
        left = [d for _r, dirs, _f in os.walk(wt_root) for d in dirs
                if d.startswith("review-5-")]
        self.assertEqual(left, [])

    def test_private_project_never_reviewed(self):
        import dataclasses

        runner = self._runner("worker_review_pass.py")
        world = review_only_world()
        projects = dict(world.projects)
        projects["demo"] = dataclasses.replace(projects["demo"],
                                               visibility="Private")
        world = dataclasses.replace(world, items=dict(world.items),
                                    projects=projects)
        runner.tick(world)
        settle(runner)
        actions = runner.tick(world)
        self.assertEqual(actions, [])
        self.assertEqual(self.patches, [])
        self.assertEqual(self.posts, [])


class ReviewCheckoutTests(ReviewHarness):
    def test_failed_fetch_aborts_without_fallback(self):
        # TH.8: the PR head moved away (or never existed here) — the run
        # fails with the fetch error; no stale branch, no packet, no work.
        self.server.add("GET", "/repos/example-owner/demo/pulls/9", body={
            "merged": False, "merged_at": None,
            "head": {"sha": "0" * 40, "ref": "feature-branch",
                     "repo": {"full_name": "example-owner/demo"}},
            "base": {"ref": "main", "repo": {"full_name": "example-owner/demo"}}})
        runner = self._runner("worker_review_pass.py")
        world = review_only_world()
        runner.tick(world)
        settle(runner)
        actions = runner.tick(mark(world, 5, review_result="Pass"))
        self.assertTrue(any("failed" in a for a in actions))
        comments = [b for p, b in self.posts if p.endswith("/activities")]
        self.assertTrue(any("could not fetch" in b["comment"]["raw"]
                            for b in comments),
                        [b["comment"]["raw"] for b in comments])
        self.assertEqual(
            [b for p, b in self.patches if p == "/api/v3/work_packages/5"],
            [])
        wt_root = os.path.join(self.tmp, "state", "worktrees")
        leaves = []
        if os.path.isdir(wt_root):
            for _root, dirs, _files in os.walk(wt_root):
                leaves.extend(d for d in dirs if d.startswith("review-5-"))
        self.assertEqual(leaves, [])
        packets = os.path.join(self.tmp, "state", "packets")
        self.assertFalse(os.path.isdir(packets) and os.listdir(packets))


class ReviewChangesTests(ReviewHarness):
    def test_changes_comments_moves_reviews(self):
        runner = self._runner("worker_review_changes.py")
        world = review_only_world()
        runner.tick(world)
        settle(runner)
        actions = runner.tick(mark(world, 5, status="In progress",
                                   review_result="Changes requested"))
        self.assertTrue(any("changes requested" in a for a in actions))
        bodies = [b for p, b in self.patches if p == "/api/v3/work_packages/5"]
        self.assertEqual(len(bodies), 1)
        self.assertEqual(bodies[0]["_links"]["customField60"],
                         {"href": "/api/v3/custom_options/71"})
        self.assertIn("/api/v3/statuses/31", bodies[0]["_links"]["status"]["href"])
        comments = [b for p, b in self.posts if p.endswith("/activities")]
        self.assertEqual(len(comments), 1)
        self.assertIn("needs tests", comments[0]["comment"]["raw"])


class TestRunTests(ReviewHarness):
    def _feature_in_test(self):
        # The fresh re-read (TH.18) sees the live snapshot: In test
        # under its Epic, mirroring test_only_world().
        self.server.add("GET", "/api/v3/work_packages/2", body={
            "id": 2, "subject": "Feature", "description": "Why: x",
            "lockVersion": 1,
            "_links": {
                "status": {"href": "/api/v3/statuses/22"},
                "type": {"href": "/api/v3/types/12"},
                "project": {"href": "%s/api/v3/projects/1"
                                      % self.server.base_url},
                "parent": {"href": "%s/api/v3/work_packages/1"
                                     % self.server.base_url}}})

    def test_test_run_posts_and_sets(self):
        self._feature_in_test()
        runner = self._runner("worker_test_pass.py")
        world = test_only_world()
        runner.tick(world)
        settle(runner)
        actions = runner.tick(mark(world, 2, test_result="Pass"))
        self.assertTrue(any("test feature 2: Pass" in a for a in actions))
        bodies = [b for p, b in self.patches if p == "/api/v3/work_packages/2"]
        self.assertEqual(len(bodies), 1)
        self.assertEqual(bodies[0]["_links"]["customField61"],
                         {"href": "/api/v3/custom_options/80"})
        comments = [b for p, b in self.posts if p.endswith("/activities")]
        summaries = [b["comment"]["raw"] for b in comments]
        self.assertIn("all green", summaries)

    def test_test_ceiling_comments_without_status_move(self):
        # TH.10: features have no Blocked status, so a twice-failed test
        # run ends in a "needs lead" comment, never a status move.
        self._feature_in_test()
        runner = self._runner()
        world = test_only_world()
        runner.tick(world)
        settle(runner)
        runner.tick(world)
        settle(runner)
        actions = runner.tick(world)
        self.assertEqual(
            [b for p, b in self.patches if p == "/api/v3/work_packages/2"],
            [])
        comments = [b for p, b in self.posts if p.endswith("/activities")]
        self.assertTrue(any("needs lead" in b["comment"]["raw"]
                            for b in comments))
        self.assertTrue(any("needs lead" in a for a in actions))

    def test_ref_deleted_on_removal_and_kept_on_keep(self):
        # TH.16: the isolated fetch ref goes away with a removed tree
        # and stays with a tree kept as evidence.
        import subprocess

        def refs():
            out = subprocess.run(
                ["git", "-C", self.repo, "for-each-ref", "refs/opl"],
                capture_output=True, text=True, timeout=60, check=True)
            return out.stdout.strip()

        runner = self._runner("worker_review_pass.py")
        world = review_only_world()
        runner.tick(world)
        settle(runner)
        runner.tick(mark(world, 5, review_result="Pass"))
        self.assertEqual(refs(), "")
        failing = self._runner("worker_fail.py")
        world2 = review_only_world()
        failing.tick(world2)
        settle(failing)
        failing.tick(mark(world2, 5, review_result="Pass"))
        self.assertTrue(refs())

    def test_feature_ceiling_comments_only_once(self):
        # TH.16: the "needs lead" comment goes out once; later ticks
        # stay quiet (the flag persists in attempts.json).
        self._feature_in_test()
        runner = self._runner()
        world = test_only_world()
        for _ in range(5):
            runner.tick(world)
            settle(runner)
        notes = [b for p, b in self.posts if p.endswith("/activities")]
        self.assertEqual(
            sum("needs lead" in b["comment"]["raw"] for b in notes), 1)

    def test_failed_ceiling_notice_is_retried_not_forgotten(self):
        # TH.22 (Codex F7): if posting the "needs lead" comment fails, the
        # notice is not recorded as sent; a later tick posts it (once).
        from opl.conductor.spark.runner import SparkRunner

        self._feature_in_test()
        delivered, failures = [], []

        def flaky(method, path, query, body, headers):
            raw = (body or {}).get("comment", {}).get("raw", "")
            if "needs lead" in raw and not failures:
                failures.append(raw)
                return 500, {"_type": "Error", "message": "temporarily down"}
            if "needs lead" in raw:
                delivered.append(raw)
            self.posts.append((path, body))
            return 201, {}

        self.server.add("POST", "/api/v3/work_packages/2/activities",
                        handler=flaky)
        runner = self._runner()
        world = test_only_world()
        for _ in range(6):
            runner.tick(world)
            settle(runner)
        self.assertEqual(len(failures), 1)
        self.assertEqual(len(delivered), 1)
        self.assertTrue(runner._ceiling_entry("test", 2).get("notified"))
        self.assertIsInstance(runner, SparkRunner)

    def test_feature_rearms_after_a_move(self):
        # TH.21: after the single ceiling comment, a status change
        # (newer status_since) re-arms the feature for a fresh pair.
        import dataclasses
        import datetime
        import json

        from opl.conductor.spark.runner import read_attempts

        state = os.path.join(self.tmp, "state")
        runner = self._runner()
        world = test_only_world()
        self._feature_in_test()
        for _ in range(3):
            runner.tick(world)
            settle(runner)
        notes = [b for p, b in self.posts if p.endswith("/activities")]
        self.assertEqual(
            sum("needs lead" in b["comment"]["raw"] for b in notes), 1)
        with open(os.path.join(state, "attempts.json"),
                  encoding="utf-8") as fh:
            hit = json.load(fh)["ceiling:test:2"]["at"]
        moved = (datetime.datetime.fromisoformat(hit)
                 + datetime.timedelta(hours=1)).isoformat()
        items = dict(world.items)
        items[2] = dataclasses.replace(items[2], status_since=moved)
        world = dataclasses.replace(world, items=items)
        runner.tick(world)
        settle(runner)
        runner.tick(world)
        self.assertEqual(read_attempts(state), {("test", 2): 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
