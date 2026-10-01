#!/usr/bin/env python3
"""Issue #32: fix runs must start from the PR's current head.

Live sequence: Spark built the PR; another contributor pushed to the PR
branch; reviewer requested changes on the new head. The fix run reused the
stale local branch, never saw the new commit, reported DONE with no change,
and failed as "uncommitted work".

Synthetic only: local bare repo as origin, FakeServer for APIs.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from opl.conductor.state import Item, Project, PullRequest, World
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


class FixHeadHarness(unittest.TestCase):
    def setUp(self):
        # A prior module (e2e) shuts the conductor down, closing the
        # process-wide stop gate; reopen it, as a new runner does, or
        # run_worker spawns nothing (see test_opencode_adapter.py).
        from opl.conductor.spark.supervisor import reset_stop_gate
        reset_stop_gate()
        self.tmp = tempfile.mkdtemp(prefix="opl-fixhead-")
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
        self.bare = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", self.bare],
                       check=True, timeout=60)
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0",
                   GCM_INTERACTIVE="never",
                   GIT_CONFIG_NOSYSTEM="1",
                   GIT_CONFIG_GLOBAL="/dev/null",
                   GIT_CONFIG_SYSTEM="/dev/null")
        self.env = env
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
        self.server.add("GET", "/api/v3/work_packages/5/activities", body={
            "_embedded": {"elements": []}})
        # PR endpoints read the live bare repo head (like op_world GitHubWorld).
        def get_pull(method, path, query, body, headers):
            out = subprocess.run(
                ["git", "--git-dir", self.bare, "rev-parse",
                 "refs/heads/feature-branch"],
                capture_output=True, text=True, timeout=60)
            sha = out.stdout.strip() if out.returncode == 0 else ""
            return 200, {"head": {"sha": sha, "ref": "feature-branch",
                                  "repo": {"full_name": "example-owner/demo"}},
                         "base": {"ref": "main",
                                  "repo": {"full_name": "example-owner/demo"}}}
        self.server.add("GET", "/repos/example-owner/demo/pulls/9",
                        handler=get_pull)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _runner(self, worker="worker_fix_done.py"):
        from opl.conductor.spark.supervisor import Slots
        settings = make_settings(self.repo, os.path.join(self.tmp, "state"),
                                 worker)
        op = Client(self.server.base_url, "spark-token")
        gh = GitHub("gh-token", self.server.base_url)
        from opl.conductor.spark.runner import SparkRunner
        return SparkRunner(settings, None, op, gh, Slots(2))

    def _world(self, head_sha):
        projects = {
            "demo": Project(key="demo", op_id=1, repo="example-owner/demo",
                            visibility="Public", has_test_env=False,
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
        prs = {"https://github.com/example-owner/demo/pull/9": PullRequest(
            url="https://github.com/example-owner/demo/pull/9",
            merged=False, merged_at=None, checks_green=True,
            head_sha=head_sha, head_repo="example-owner/demo",
            base_repo="example-owner/demo")}
        return World(now="2026-09-24T12:00:00Z", projects=projects,
                     items=items, pull_requests=prs)

    def _bare_sha(self, branch):
        out = subprocess.run(
            ["git", "--git-dir", self.bare, "rev-parse",
             "refs/heads/%s" % branch],
            capture_output=True, text=True, timeout=60)
        return out.stdout.strip()

    def _bare_file(self, branch, name):
        out = subprocess.run(
            ["git", "--git-dir", self.bare, "show", "%s:%s" % (branch, name)],
            capture_output=True, text=True, timeout=60)
        return out.stdout if out.returncode == 0 else None


class FixHeadTests(FixHeadHarness):
    def test_fix_starts_from_current_pr_head(self):
        # External contributor pushes to the PR branch after review.
        subprocess.run(["git", "-C", self.repo, "checkout", "-q",
                        "feature-branch"], check=True, timeout=60)
        with open(os.path.join(self.repo, "external.txt"), "w") as fh:
            fh.write("external change\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"],
                       check=True, timeout=60)
        subprocess.run(["git", "-C", self.repo, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-qm", "external"],
                       check=True, timeout=60)
        subprocess.run(
            ["git", "-C", self.repo, "-c", "credential.helper=",
             "push", "origin", "feature-branch"],
            check=True, timeout=60, env=self.env)
        new_head = self._bare_sha("feature-branch")
        self.assertTrue(new_head)
        # Conductor's local branch is stale (still at the old commit).
        old = subprocess.run(
            ["git", "-C", self.repo, "rev-parse", "HEAD~1"],
            capture_output=True, text=True, timeout=60).stdout.strip()
        subprocess.run(["git", "-C", self.repo, "reset", "--hard", "-q", old],
                       check=True, timeout=60)
        stale = subprocess.run(
            ["git", "-C", self.repo, "rev-parse", "feature-branch"],
            capture_output=True, text=True, timeout=60).stdout.strip()
        self.assertNotEqual(stale, new_head)
        subprocess.run(["git", "-C", self.repo, "checkout", "-q", "main"],
                       check=True, timeout=60)

        runner = self._runner()
        world = self._world(new_head)
        runner.tick(world)
        settle(runner)
        import dataclasses
        items = dict(world.items)
        items[5] = dataclasses.replace(items[5], status="In review",
                                       review_result=None)
        world2 = dataclasses.replace(world, items=items)
        actions = runner.tick(world2)
        self.assertTrue(any("fix task 5" in a for a in actions),
                        "fix run should succeed, got %r" % (actions,))
        # The fix commit lands on top of the external push.
        self.assertEqual(self._bare_file("feature-branch", "external.txt"),
                         "external change\n")
        self.assertEqual(self._bare_file("feature-branch", "fix.txt"),
                         "addressed review feedback\n")
        log = subprocess.run(
            ["git", "--git-dir", self.bare, "log", "--format=%H %P",
             "feature-branch"],
            capture_output=True, text=True, timeout=60).stdout.strip().splitlines()
        self.assertGreaterEqual(len(log), 2)
        top_sha, top_parents = log[0].split()[0], log[0].split()[1:]
        self.assertEqual(top_parents, [new_head])


class FixDivergedTests(FixHeadHarness):
    def test_diverged_local_branch_blocks_with_reason(self):
        head = self._bare_sha("feature-branch")
        # Local branch moves ahead with a commit the PR does not have.
        subprocess.run(["git", "-C", self.repo, "checkout", "-q",
                        "feature-branch"], check=True, timeout=60)
        with open(os.path.join(self.repo, "local-only.txt"), "w") as fh:
            fh.write("local\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"],
                       check=True, timeout=60)
        subprocess.run(["git", "-C", self.repo, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-qm", "local only"],
                       check=True, timeout=60)
        subprocess.run(["git", "-C", self.repo, "checkout", "-q", "main"],
                       check=True, timeout=60)
        runner = self._runner()
        world = self._world(head)
        runner.tick(world)
        settle(runner)
        import dataclasses
        items = dict(world.items)
        items[5] = dataclasses.replace(items[5], status="Blocked")
        world2 = dataclasses.replace(world, items=items)
        actions = runner.tick(world2)
        self.assertTrue(any("not started" in a for a in actions),
                        "diverged fix must block, got %r" % (actions,))
        blocked = [b for p, b in self.patches
                   if b.get("_links", {}).get("status", {}).get("href", "")
                   .endswith("/33")]
        self.assertEqual(len(blocked), 1)
        comments = [b for p, b in self.posts if p.endswith("/activities")]
        self.assertTrue(any("has commits not in PR head" in b["comment"]["raw"]
                            for b in comments),
                        "diverged reason must name the cause, got %r"
                        % ([b["comment"]["raw"] for b in comments],))
        # The PR branch on the origin is untouched.
        self.assertIsNone(self._bare_file("feature-branch", "local-only.txt"))
        self.assertIsNone(self._bare_file("feature-branch", "fix.txt"))


class FixStaleBranchTests(FixHeadHarness):
    def test_already_pushed_local_tip_is_stale_not_diverged(self):
        # The PR was rebased on GitHub after Spark pushed its build commit:
        # the local branch still points at that pushed commit. Recorded as
        # pushed in runs.jsonl, it is stale, and the fix runs from the PR
        # head instead of blocking for a human decision.
        head = self._bare_sha("feature-branch")
        subprocess.run(["git", "-C", self.repo, "checkout", "-q",
                        "feature-branch"], check=True, timeout=60)
        with open(os.path.join(self.repo, "pre-rebase.txt"), "w") as fh:
            fh.write("old\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"],
                       check=True, timeout=60)
        subprocess.run(["git", "-C", self.repo, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-qm", "pushed before"],
                       check=True, timeout=60)
        tip = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=True,
                             timeout=60).stdout.strip()
        subprocess.run(["git", "-C", self.repo, "checkout", "-q", "main"],
                       check=True, timeout=60)
        state = os.path.join(self.tmp, "state")
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "runs.jsonl"), "a", encoding="utf-8") as fh:
            fh.write('{"task": 5, "kind": "build", "outcome": "success", '
                     '"commit": "%s"}\n' % tip)
        runner = self._runner()
        world = self._world(head)
        actions = runner.tick(world)
        settle(runner)
        actions += runner.tick(world)
        self.assertFalse(any("not started" in a for a in actions),
                         "stale branch must not block, got %r" % (actions,))
        self.assertEqual(self._bare_file("feature-branch", "fix.txt"),
                         "addressed review feedback\n")


class FixMessageTests(FixHeadHarness):
    def _run_fix_worker(self, worker):
        head = self._bare_sha("feature-branch")
        runner = self._runner(worker)
        world = self._world(head)
        runner.tick(world)
        settle(runner)
        import dataclasses
        items = dict(world.items)
        items[5] = dataclasses.replace(items[5], status="Blocked")
        world2 = dataclasses.replace(world, items=items)
        actions = runner.tick(world2)
        return actions

    def test_done_without_commit_says_so(self):
        actions = self._run_fix_worker("worker_ok.py")
        self.assertTrue(any("Blocked" in a for a in actions))
        comments = [b for p, b in self.posts if p.endswith("/activities")]
        self.assertTrue(any("worker made no new commit" in b["comment"]["raw"]
                            for b in comments),
                        "got %r" % ([b["comment"]["raw"] for b in comments],))
        self.assertFalse(any("uncommitted work" in b["comment"]["raw"]
                             for b in comments))

    def test_done_with_dirty_tree_says_so(self):
        actions = self._run_fix_worker("worker_dirty.py")
        self.assertTrue(any("Blocked" in a for a in actions))
        comments = [b for p, b in self.posts if p.endswith("/activities")]
        self.assertTrue(any("uncommitted changes left" in b["comment"]["raw"]
                            for b in comments),
                        "got %r" % ([b["comment"]["raw"] for b in comments],))
        self.assertFalse(any("uncommitted work" in b["comment"]["raw"]
                             for b in comments))


if __name__ == "__main__":
    unittest.main(verbosity=2)
