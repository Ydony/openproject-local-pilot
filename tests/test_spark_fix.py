#!/usr/bin/env python3
"""Tests for fix-after-review runs: same-branch push, clear, move, guards."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest

from opl.conductor.spark.records import record_run
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


def make_settings(repo_path, state_dir, worker="worker_fix_done.py"):
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
                            False, "", None, None, repo_path, "main"),
        ),
    )


def fix_world(visibility="Public"):
    projects = {
        "demo": Project(key="demo", op_id=1, repo="example-owner/demo",
                        visibility=visibility, has_test_env=False,
                        test_signal=None, prod_signal=None),
    }
    items = {
        1: Item(id=1, project="demo", type="Epic", status="Open",
                 status_since="2026-09-20T12:00:00Z"),
        2: Item(id=2, project="demo", type="Feature", status="Building",
                 status_since="2026-09-20T12:00:00Z", parent_id=1),
        5: Item(id=5, project="demo", type="Task", status="In progress",
                 status_since="2026-09-20T12:00:00Z", parent_id=2,
                 assignee="spark", reviewer="claude", size="S", risk="Low",
                 pr_url="https://github.com/example-owner/demo/pull/9",
                 review_result="Changes requested"),
    }
    return World(now="2026-09-24T12:00:00Z", projects=projects, items=items,
                 pull_requests={})


def settle(runner, timeout=120):
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
    import dataclasses

    items = dict(world.items)
    items[iid] = dataclasses.replace(items[iid], **changes)
    return dataclasses.replace(world, items=items)


JOURNAL = {
    "_embedded": {"elements": [
        {"createdAt": "2026-09-24T10:00:00Z",
         "comment": {"raw": "OPL-REVIEW: CHANGES please rename X"},
         "_links": {"author": {"title": "spark"}}},
        {"createdAt": "2026-09-24T10:05:00Z",
         "comment": {"raw": "Also fix the typo on line 3."},
         "_links": {"author": {"title": "Owner"}}},
    ]},
}


class FixHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opl-fix-")
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
            ["git", "-C", self.repo, "checkout", "-qb", "feature-branch"],
            check=True, timeout=60)
        with open(os.path.join(self.repo, "work.txt"), "w") as fh:
            fh.write("work\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True, timeout=60)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "work"],
                       check=True, timeout=60)
        subprocess.run(["git", "-C", self.repo, "checkout", "-q", "main"],
                       check=True, timeout=60)
        # Local bare repo as origin: no network, no token, no prompts.
        self.bare = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", self.bare],
                       check=True, timeout=60)
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0",
                   GCM_INTERACTIVE="never",
                   GIT_CONFIG_NOSYSTEM="1",
                   GIT_CONFIG_GLOBAL="/dev/null",
                   GIT_CONFIG_SYSTEM="/dev/null")
        point_origin(self.repo, self.bare, "example-owner/demo", env=env)
        subprocess.run(
            ["git", "-C", self.repo, "-c", "credential.helper=",
             "push", "origin", "main", "feature-branch"],
            check=True, timeout=60, env=env)
        self.server = FakeServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.server.add("GET", "/api/v3/statuses", body={"_embedded": {"elements": [
            {"id": 31, "name": "In progress"}, {"id": 32, "name": "In review"},
            {"id": 33, "name": "Blocked"}, {"id": 34, "name": "Approved"},
            {"id": 35, "name": "Proposed"}]}})
        self.server.add("GET", "/repos/example-owner/demo",
                        body={"private": False})
        self.server.add("GET", "/api/v3/types", body={"_embedded": {"elements": [
            {"id": 13, "name": "Task"}, {"id": 12, "name": "Feature"}]}})
        self.server.add("GET", "/api/v3/work_packages/schemas/1-13", body={
            "customField60": {"name": "Review result", "location": "_links",
                              "_embedded": {"allowedValues": [
                                  {"id": 70, "value": "Pass"},
                                  {"id": 71, "value": "Changes requested"}]}},
            "customField62": {"type": "Link", "name": "PR link"}})
        self.patches = []
        self.posts = []

        def patch_wp(method, path, query, body, headers):
            self.patches.append((path, body))
            return 200, {}

        def post_any(method, path, query, body, headers):
            self.posts.append((path, body))
            return 201, {}

        self.server.add("PATCH", "/api/v3/work_packages/5", handler=patch_wp)
        self.server.add("POST", "/api/v3/work_packages/5/activities", handler=post_any)
        self.server.add("GET", "/api/v3/work_packages/5", body={
            "id": 5, "subject": "Build thing", "description": "Do it",
            "lockVersion": 1,
            "customField62": "https://github.com/example-owner/demo/pull/9",
            "_links": {
                "status": {"href": "/api/v3/statuses/31"},
                "type": {"href": "/api/v3/types/13"},
                "project": {"href": "%s/api/v3/projects/1"
                                      % self.server.base_url},
                "parent": {"href": "%s/api/v3/work_packages/2"
                                     % self.server.base_url},
                "assignee": {"href": "/api/v3/users/4", "title": "Spark"}}})
        # Live relations for the spawn-time predecessor check (TH.22).
        self.server.add("GET", "/api/v3/relations",
                        body={"_embedded": {"elements": []}})
        self.server.add("GET", "/api/v3/work_packages/2", body={
            "id": 2, "subject": "Feature", "description": "Why: x",
            "lockVersion": 1,
            "_links": {
                "status": {"href": "/api/v3/statuses/34"},
                "type": {"href": "/api/v3/types/12"},
                "project": {"href": "%s/api/v3/projects/1"
                                      % self.server.base_url}}})
        self.server.add("GET", "/api/v3/work_packages/5/activities", body=JOURNAL)
        self.server.add("GET", "/repos/example-owner/demo/pulls/9", body={
            "head": {"ref": "feature-branch"}})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _runner(self, worker="worker_fix_done.py"):
        from opl.conductor.spark.supervisor import Slots

        settings = make_settings(self.repo, os.path.join(self.tmp, "state"),
                                 worker)
        op = Client(self.server.base_url, "spark-token")
        gh = GitHub("gh-token", self.server.base_url)
        return SparkRunner(settings, None, op, gh, Slots(2))

    def _fix_packet(self):
        path = os.path.join(self.tmp, "state", "packets", "fix-5.md")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def _bare_file(self, branch, name):
        out = subprocess.run(
            ["git", "--git-dir", self.bare, "show", "%s:%s" % (branch, name)],
            capture_output=True, text=True, timeout=60)
        return out.stdout if out.returncode == 0 else None


class FixSuccessTests(FixHarness):
    def test_same_branch_pushed_cleared_and_moved(self):
        runner = self._runner()
        world = fix_world()
        runner.tick(world)
        settle(runner)
        actions = runner.tick(mark(world, 5, status="In review",
                                   review_result=None))
        self.assertTrue(any("fix task 5" in a for a in actions))
        # Same PR branch updated on the origin, not a new opl/ branch.
        self.assertEqual(self._bare_file("feature-branch", "fix.txt"),
                         "addressed review feedback\n")
        out = subprocess.run(
            ["git", "--git-dir", self.bare, "branch", "--list", "opl/*"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "")
        bodies = [b for p, b in self.patches if p == "/api/v3/work_packages/5"]
        self.assertEqual(len(bodies), 1)
        self.assertIn("/api/v3/statuses/32",
                      bodies[0]["_links"]["status"]["href"])
        self.assertEqual(bodies[0]["_links"]["customField60"],
                         {"href": None})

    def test_packet_carries_reviewer_notes(self):
        runner = self._runner()
        world = fix_world()
        runner.tick(world)
        settle(runner)
        runner.tick(mark(world, 5, status="In review", review_result=None))
        packet = self._fix_packet()
        self.assertIn("typo on line 3", packet)
        self.assertNotIn("OPL-REVIEW: CHANGES", packet)

    def test_missing_final_line_blocks(self):
        runner = self._runner("worker_ok.py")
        world = fix_world()
        runner.tick(world)
        settle(runner)
        actions = runner.tick(mark(world, 5, status="Blocked"))
        blocked = [b for p, b in self.patches
                   if b.get("_links", {}).get("status", {}).get("href", "")
                   .endswith("/33")]
        self.assertEqual(len(blocked), 1)
        self.assertTrue(any("Blocked" in a for a in actions))

    def test_rejected_fix_push_blocked_push_failed(self):
        # Issue #49 (fix path): a rejected HEAD:branch push Blocks with
        # the reason, records push-failed and keeps the worktree.
        hook = os.path.join(self.bare, "hooks", "pre-receive")
        with open(hook, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("#!/bin/sh\necho 'remote rejected: refusing to allow "
                     "a Personal Access Token to create or update workflow "
                     ".github/workflows/ci.yml without workflow scope' >&2\n"
                     "exit 1\n")
        os.chmod(hook, 0o755)
        runner = self._runner()
        world = fix_world()
        runner.tick(world)
        settle(runner)
        actions = runner.tick(mark(world, 5, status="In review",
                                   review_result=None))
        self.assertTrue(any("push failed" in a for a in actions), actions)
        blocked = [b for p, b in self.patches
                   if b.get("_links", {}).get("status", {}).get("href", "")
                   .endswith("/33")]
        self.assertTrue(blocked, "fix was not moved to Blocked")
        comments = [b for p, b in self.posts if p.endswith("/activities")]
        self.assertTrue(any("Push failed" in b["comment"]["raw"]
                            for b in comments), comments)
        self.assertTrue(any("Workflows" in b["comment"]["raw"]
                            for b in comments), comments)
        from opl.conductor.spark.records import read_runs
        rows = read_runs(os.path.join(self.tmp, "state"))
        self.assertTrue(any(r.get("outcome") == "push-failed"
                            for r in rows), rows)
        self.assertFalse(any(r.get("outcome") == "fix-success"
                             for r in rows), rows)


class FixGuardTests(FixHarness):
    def test_third_round_blocked(self):
        state_dir = os.path.join(self.tmp, "state")
        for _ in range(2):
            record_run(state_dir, task=5, kind="review", size="S",
                       started="2026-09-24T10:00:00+00:00",
                       ended="2026-09-24T10:01:00+00:00",
                       duration_s=60.0, outcome="review-changes", cost_usd=0.0)
        runner = self._runner()
        world = fix_world()
        actions = runner.tick(world)
        settle(runner)
        actions += runner.tick(mark(world, 5, status="Blocked"))
        blocked = [b for p, b in self.patches
                   if b.get("_links", {}).get("status", {}).get("href", "")
                   .endswith("/33")]
        self.assertEqual(len(blocked), 1)
        comments = [b for p, b in self.posts if p.endswith("/activities")]
        self.assertTrue(any("review loop: 2 rounds" in b["comment"]["raw"]
                            for b in comments))
        self.assertFalse(os.path.exists(os.path.join(state_dir, "packets")))
        self.assertTrue(any("review loop" in a for a in actions))

    def test_private_never_picked(self):
        runner = self._runner()
        world = fix_world(visibility="Private")
        runner.tick(world)
        settle(runner)
        actions = runner.tick(world)
        self.assertEqual(actions, [])
        self.assertEqual(self.patches, [])
        self.assertEqual(self.posts, [])


class ReworkHandoffTests(FixHarness):
    """Issue #61: In review + Changes requested must return to Spark.

    An external reviewer (Claude/Codex) sets Review result without moving
    the task, so it never becomes the In-progress fix candidate. The
    runner hands it back to In progress (same PR, reviewer notes become
    the fix packet); after two rework rounds it Blocks like the fix path.
    """

    def _in_review_changes(self, **over):
        base = {"status": "In review"}
        base.update(over)
        return mark(fix_world(), 5, **base)

    def test_in_review_changes_moves_to_in_progress(self):
        runner = self._runner()
        world = self._in_review_changes()
        actions = runner.tick(world)
        settle(runner)
        self.assertTrue(any("rework" in a for a in actions), actions)
        moved = [b for p, b in self.patches
                 if b.get("_links", {}).get("status", {}).get("href", "")
                 .endswith("/31")]
        self.assertEqual(len(moved), 1)
        # No worker spawned for the handoff itself.
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, "state", "packets", "fix-5.md")))
        self.assertEqual(runner.pending(), 0)

    def test_handoff_feeds_fix_on_next_tick(self):
        runner = self._runner()
        world = self._in_review_changes()
        runner.tick(world)
        settle(runner)
        # Fresh collect after the handoff: In progress + Changes requested.
        progressed = self._in_review_changes(status="In progress")
        runner.tick(progressed)
        settle(runner)
        actions = runner.tick(mark(progressed, 5, status="In review",
                                   review_result=None))
        self.assertTrue(any("fix task 5" in a for a in actions), actions)
        self.assertEqual(self._bare_file("feature-branch", "fix.txt"),
                         "addressed review feedback\n")

    def test_two_reviews_from_in_review_block(self):
        state_dir = os.path.join(self.tmp, "state")
        for _ in range(2):
            record_run(state_dir, task=5, kind="review", size="S",
                       started="2026-09-24T10:00:00+00:00",
                       ended="2026-09-24T10:01:00+00:00",
                       duration_s=60.0, outcome="review-changes", cost_usd=0.0)
        runner = self._runner()
        actions = runner.tick(self._in_review_changes())
        settle(runner)
        blocked = [b for p, b in self.patches
                   if b.get("_links", {}).get("status", {}).get("href", "")
                   .endswith("/33")]
        self.assertEqual(len(blocked), 1)
        self.assertTrue(any("review loop" in a for a in actions))

    def test_two_fixes_from_in_review_block(self):
        state_dir = os.path.join(self.tmp, "state")
        for _ in range(2):
            record_run(state_dir, task=5, kind="fix", size="S",
                       started="2026-09-24T10:00:00+00:00",
                       ended="2026-09-24T10:01:00+00:00",
                       duration_s=60.0, outcome="fix-success", cost_usd=0.0)
        runner = self._runner()
        actions = runner.tick(self._in_review_changes())
        settle(runner)
        blocked = [b for p, b in self.patches
                   if b.get("_links", {}).get("status", {}).get("href", "")
                   .endswith("/33")]
        self.assertEqual(len(blocked), 1)
        self.assertTrue(any("review loop" in a for a in actions))
        self.assertFalse(os.path.exists(
            os.path.join(state_dir, "packets", "fix-5.md")))

    def test_two_fixes_block_in_progress_too(self):
        state_dir = os.path.join(self.tmp, "state")
        for _ in range(2):
            record_run(state_dir, task=5, kind="fix", size="S",
                       started="2026-09-24T10:00:00+00:00",
                       ended="2026-09-24T10:01:00+00:00",
                       duration_s=60.0, outcome="fix-success", cost_usd=0.0)
        runner = self._runner()
        actions = runner.tick(fix_world())
        settle(runner)
        blocked = [b for p, b in self.patches
                   if b.get("_links", {}).get("status", {}).get("href", "")
                   .endswith("/33")]
        self.assertEqual(len(blocked), 1)
        self.assertTrue(any("review loop" in a for a in actions))

    def test_private_missing_pr_wrong_assignee_never_moved(self):
        for world in (
                mark(fix_world(visibility="Private"), 5, status="In review"),
                mark(fix_world(), 5, status="In review", pr_url=None),
                mark(fix_world(), 5, status="In review", assignee="claude"),
                mark(fix_world(), 5, status="In review",
                     review_result="Pass")):
            runner = self._runner()
            self.patches.clear()
            self.posts.clear()
            actions = runner.tick(world)
            settle(runner)
            actions += runner.tick(world)
            self.assertEqual(actions, [], world.items[5])
            self.assertEqual(self.patches, [])
            self.assertEqual(self.posts, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ReviewerNotesTests(unittest.TestCase):
    """#61: the rework packet carries the newest reviewer comment."""

    def _notes(self, elements):
        class _Op:
            def get(self, path):
                return {"_embedded": {"elements": elements}}

        runner = SparkRunner.__new__(SparkRunner)
        runner.op = _Op()
        return runner._reviewer_notes(
            types.SimpleNamespace(id=5))

    @staticmethod
    def _entry(raw, author):
        return {"comment": {"raw": raw},
                "_links": {"author": {"title": author}}}

    def test_newest_reviewer_comment_wins(self):
        notes = self._notes([self._entry("old review", "Claude"),
                             self._entry("new review", "Claude"),
                             self._entry("OPL-REVIEW: CHANGES x", "spark")])
        self.assertEqual(notes, "new review")

    def test_later_conductor_note_is_appended(self):
        notes = self._notes([self._entry("fix the test", "Claude"),
                             self._entry("Merge conflict: rebase", "Conductor")])
        self.assertEqual(notes, "fix the test\n\nMerge conflict: rebase")

    def test_only_spark_comments_fall_back_to_latest(self):
        notes = self._notes([self._entry("first", "spark"),
                             self._entry("second", "spark")])
        self.assertEqual(notes, "second")
