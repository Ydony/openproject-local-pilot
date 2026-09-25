#!/usr/bin/env python3
"""Tests for the Spark build runner: outcomes, slots, privacy."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from unittest import mock

from opl.conductor.spark.runner import SparkRunner, _push_env
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
NOW_STR = "2026-09-24T12:00:00Z"


def make_settings(repo_path, command, state_dir, limits=None,
                  base_ref="main", pr_base="main"):
    limits = {"S": 1, "M": 2, "L": 3, "review": 1, "test": 1, "stall": 30,
              **(limits or {})}
    return Settings(
        openproject=OpenProject("http://127.0.0.1:8080", "OPL_TOKEN_ADMIN", "admin"),
        tokens={"spark": "OPL_TOKEN_SPARK"},
        github_token_env="OPL_GITHUB_TOKEN",
        users_email_domain="example.invalid",
        conductor=Conductor(False, 60, state_dir),
        runner=Runner(tuple(command), 2, limits),
        projects=(
            SettingsProject("demo", "Demo", "example-owner/demo", "Public",
                            False, "", None, None, repo_path, base_ref,
                            pr_base),
            SettingsProject("priv", "Priv", "example-owner/priv", "Private",
                            False, "", None, None, repo_path, base_ref,
                            pr_base),
        ),
    )


def make_world(*tasks):
    projects = {
        "demo": Project(key="demo", op_id=1, repo="example-owner/demo",
                        visibility="Public", has_test_env=False,
                        test_signal=None, prod_signal=None),
        "priv": Project(key="priv", op_id=2, repo="example-owner/priv",
                        visibility="Private", has_test_env=False,
                        test_signal=None, prod_signal=None),
    }
    items = {
        1: Item(id=1, project="demo", type="Epic", status="Open",
                 status_since="2026-09-20T12:00:00Z"),
        2: Item(id=2, project="demo", type="Feature", status="Approved",
                 status_since="2026-09-20T12:00:00Z", parent_id=1),
        11: Item(id=11, project="priv", type="Epic", status="Open",
                  status_since="2026-09-20T12:00:00Z"),
        12: Item(id=12, project="priv", type="Feature", status="Approved",
                  status_since="2026-09-20T12:00:00Z", parent_id=11),
    }
    for task in tasks:
        items[task.id] = task
    return World(now=NOW_STR, projects=projects, items=items,
                 pull_requests={})


def ready_task(iid, project="demo", size="S"):
    return Item(id=iid, project=project, type="Task", status="Ready",
                status_since="2026-09-20T12:00:00Z", parent_id=2,
                assignee="spark", reviewer="claude", size=size, risk="Low",
                lock_version=3)


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


def mark(world, iid, status):
    """Return a world with one item's status replaced (like a fresh collect
    after the conductor moved it). Prevents re-starting concluded tasks."""
    import dataclasses

    items = dict(world.items)
    items[iid] = dataclasses.replace(items[iid], status=status)
    return dataclasses.replace(world, items=items)


class RunnerHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opl-runner-")
        self.origin = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", self.origin],
                       check=True, timeout=60)
        self.repo = os.path.join(self.tmp, "repo")
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
        point_origin(self.repo, self.origin, "example-owner/demo")
        self.server = FakeServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        base = self.server.base_url
        self.server.add("GET", "/api/v3/statuses", body={"_embedded": {"elements": [
            {"id": 31, "name": "In progress"}, {"id": 32, "name": "In review"},
            {"id": 33, "name": "Blocked"}, {"id": 34, "name": "Ready"},
            {"id": 35, "name": "Approved"}, {"id": 36, "name": "Building"},
            {"id": 37, "name": "Proposed"}]}})
        self.server.add("GET", "/api/v3/types", body={"_embedded": {"elements": [
            {"id": 13, "name": "Task"}, {"id": 12, "name": "Feature"}]}})
        self.server.add("GET", "/repos/example-owner/demo",
                        body={"private": False})
        self.server.add("GET", "/api/v3/work_packages/schemas/1-13", body={
            "customField50": {"type": "String", "name": "PR link"}})
        self.server.add("GET", "/api/v3/work_packages/schemas/2-13", body={
            "customField50": {"type": "String", "name": "PR link"}})
        self.patches = []
        self.posts = []
        # Work-package versions tracked live: PATCHes must carry the fresh
        # version or get a 409, proving the runner re-fetches every time.
        self.wp_versions = {5: 3, 6: 1, 7: 1, 2: 1}
        self.wp_subjects = {
            5: ("Build thing", "Do it well"),
            6: ("Build other", ""),
            7: ("Build slow", ""),
            2: ("Feature", "Why: x"),
        }

        def get_wp(method, path, query, body, headers):
            wid = int(path.rsplit("/", 1)[-1])
            subject, desc = self.wp_subjects[wid]
            # TH.18 fresh reads parse HAL links; statuses mirror the
            # worlds these tests build (task Ready, feature Approved).
            status = {5: 34, 6: 34, 7: 34, 2: 35}.get(wid, 31)
            project = 2 if wid in (11, 12) else 1
            wtype = 12 if wid in (2, 12) else 13
            parent = {5: 2, 6: 2, 7: 2}.get(wid)
            links = {"status": {"href": "/api/v3/statuses/%d" % status},
                     "type": {"href": "/api/v3/types/%d" % wtype},
                     "project": {"href": "%s/api/v3/projects/%d"
                                          % (self.server.base_url, project)}}
            if parent is not None:
                links["parent"] = {
                    "href": "%s/api/v3/work_packages/%d"
                            % (self.server.base_url, parent)}
            if wtype == 13:
                links["assignee"] = {"href": "/api/v3/users/4", "title": "Spark"}
            return 200, {"id": wid, "subject": subject, "description": desc,
                         "lockVersion": self.wp_versions[wid],
                         "_links": links}

        def patch_wp(method, path, query, body, headers):
            self.patches.append((path, body))
            wid = int(path.rsplit("/", 1)[-1])
            if body.get("lockVersion") != self.wp_versions.get(wid):
                return 409, {"_type": "Error", "message": "lock version mismatch"}
            self.wp_versions[wid] += 1
            return 200, {}

        def post_any(method, path, query, body, headers):
            self.posts.append((path, body))
            if path == "/api/v3/work_packages/activities":
                return 201, {}
            return 200, {}

        self.server.add("PATCH", "/api/v3/work_packages/5", handler=patch_wp)
        self.server.add("PATCH", "/api/v3/work_packages/6", handler=patch_wp)
        self.server.add("PATCH", "/api/v3/work_packages/7", handler=patch_wp)
        self.server.add("POST", "/api/v3/work_packages/5/activities", handler=post_any)
        self.server.add("POST", "/api/v3/work_packages/6/activities", handler=post_any)
        self.server.add("POST", "/api/v3/work_packages/7/activities", handler=post_any)
        for wid in (5, 6, 7, 2):
            self.server.add("GET", "/api/v3/work_packages/%d" % wid, handler=get_wp)
        # Live relations for the spawn-time predecessor check (TH.22).
        self.server.add("GET", "/api/v3/relations",
                        body={"_embedded": {"elements": []}})
        self.server.add("POST", "/repos/example-owner/demo/pulls", body={
            "html_url": base + "/example-owner/demo/pull/9", "number": 9})
        # No open PR yet: the lookup answers an empty list (a failed
        # lookup raises since TH.20).
        self.server.add("GET", "/repos/example-owner/demo/pulls", body=[])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _runner(self, worker, slots_max=2, limits=None, base_ref="main",
                pr_base="main"):
        from opl.conductor.spark.supervisor import Slots

        settings = make_settings(
            self.repo, [sys.executable, os.path.join(FAKES, worker)],
            os.path.join(self.tmp, "state"), limits, base_ref, pr_base)
        op = Client(self.server.base_url, "spark-token")
        gh = GitHub("gh-token", self.server.base_url)
        return SparkRunner(settings, None, op, gh, Slots(slots_max))

    def _branches(self):
        out = subprocess.run(
            ["git", "--git-dir", self.origin, "branch", "-a"],
            capture_output=True, text=True, timeout=60, check=True)
        return out.stdout


class DispatchTests(RunnerHarness):
    """TH.6: revalidate right before every run; Ready alone proves nothing."""

    def _blocked_with(self, world, reason_fragment):
        runner = self._runner("worker_commit.py")
        actions = runner.tick(world)
        settle(runner)
        moves = [b for p, b in self.patches if p == "/api/v3/work_packages/5"]
        self.assertTrue(any("/api/v3/statuses/33" in str(b) for b in moves),
                        "task was not moved to Blocked: %r" % moves)
        comments = [r["body"]["comment"]["raw"] for r in self.server.requests
                    if r["method"] == "POST" and r["path"].endswith("/5/activities")]
        self.assertTrue(any(reason_fragment in c for c in comments), comments)
        packets = os.path.join(self.tmp, "state", "packets")
        self.assertFalse(os.path.isdir(packets) and os.listdir(packets),
                         "a worker was started")
        return actions

    def test_manual_ready_under_unapproved_feature(self):
        world = make_world(ready_task(5))
        world.items[2] = replace(world.items[2], status="Proposed")
        self._blocked_with(world, "not Approved/Building")

    def test_parent_in_another_project(self):
        world = make_world(ready_task(5))
        world.items[2] = replace(world.items[2], project="priv")
        self._blocked_with(world, "same project")

    def test_unmerged_predecessor(self):
        pred = replace(ready_task(6), status="In review")
        task = replace(ready_task(5), predecessors=(6,))
        world = make_world(task, pred)
        self._blocked_with(world, "predecessor 6")

    def test_origin_is_not_the_configured_repo(self):
        subprocess.run(["git", "-C", self.repo, "remote", "set-url", "origin",
                        "https://github.com/someone-else/demo.git"],
                       check=True, timeout=60)
        self._blocked_with(make_world(ready_task(5)), "origin")

    def test_foreign_host_refused_even_with_matching_slug(self):
        # TH.18: the slug matches but the host is not github.com.
        # No worker starts (so no network ever happens).
        subprocess.run(["git", "-C", self.repo, "remote", "set-url", "origin",
                        "https://example.invalid/example-owner/demo.git"],
                       check=True, timeout=60)
        self._blocked_with(make_world(ready_task(5)), "github.com")

    def test_private_repo_refused(self):
        # TH.18: the API says private → fail closed, no worker.
        self.server.add("GET", "/repos/example-owner/demo",
                        body={"private": True})
        self._blocked_with(make_world(ready_task(5)), "not public")

    def test_repo_lookup_failure_refused(self):
        # TH.18: an unreachable repo lookup refuses, fail closed.
        self.server.add("GET", "/repos/example-owner/demo",
                        status=500, body={"message": "boom"})
        self._blocked_with(make_world(ready_task(5)), "verify")

    def test_start_ref_must_resolve_locally(self):
        # TH.11: a start ref that resolves to nothing blocks, no worker.
        runner = self._runner("worker_commit.py", base_ref="no-such-ref")
        runner.tick(make_world(ready_task(5)))
        settle(runner)
        moves = [b for p, b in self.patches if p == "/api/v3/work_packages/5"]
        self.assertTrue(any("/api/v3/statuses/33" in str(b) for b in moves),
                        "task was not moved to Blocked: %r" % moves)
        comments = [r["body"]["comment"]["raw"] for r in self.server.requests
                    if r["method"] == "POST" and r["path"].endswith("/5/activities")]
        self.assertTrue(any("start ref" in c for c in comments), comments)
        packets = os.path.join(self.tmp, "state", "packets")
        self.assertFalse(os.path.isdir(packets) and os.listdir(packets),
                         "a worker was started")

    def test_remote_tracking_pr_base_rejected_at_dispatch(self):
        # TH.11: even if settings validation were bypassed, a remote
        # ref as pr_base never reaches GitHub.
        runner = self._runner("worker_commit.py", pr_base="origin/main")
        runner.tick(make_world(ready_task(5)))
        settle(runner)
        comments = [r["body"]["comment"]["raw"] for r in self.server.requests
                    if r["method"] == "POST" and r["path"].endswith("/5/activities")]
        self.assertTrue(any("pr base" in c for c in comments), comments)
        posts = [r for r in self.server.requests
                 if r["method"] == "POST"
                 and r["path"] == "/repos/example-owner/demo/pulls"]
        self.assertEqual(posts, [])


class IsolationTests(RunnerHarness):
    def test_worker_never_sees_conductor_tokens(self):
        # TH.1: every conductor secret stays out of the worker process, and
        # its home is a fresh per-run folder under the state dir.
        secrets = {"OPL_CANARY_SECRET": "canary", "OPL_TOKEN_SPARK": "spark-t",
                   "OPL_TOKEN_ADMIN": "admin-t", "OPL_GITHUB_TOKEN": "gh-t"}
        runner = self._runner("worker_env_probe.py")
        with mock.patch.dict(os.environ, secrets):
            runner.tick(make_world(ready_task(5)))
            settle(runner)
        with open(os.path.join(self.tmp, "state", "logs", "run-5-1.log"),
                  encoding="utf-8") as fh:
            log = fh.read()
        keys = [l for l in log.splitlines() if l.startswith("ENVKEYS:")][0]
        for name in secrets:
            self.assertNotIn(name, keys.split(":", 1)[1].split(","))
        home = [l for l in log.splitlines() if l.startswith("HOME:")][0]
        self.assertIn(os.path.normcase(os.path.join(self.tmp, "state", "homes")),
                      os.path.normcase(home))


class RedactionTests(RunnerHarness):
    def test_worker_output_never_reaches_comments_or_packets(self):
        # TH.2: a worker that prints a known secret (plain and base64) and
        # fails twice. Neither the retry packet nor any tracker comment may
        # carry it.
        import base64

        secret = "leaky-s3cret-value"
        encoded = base64.b64encode(("x-access-token:" + secret).encode()).decode()
        runner = self._runner("worker_echo_secret.py")
        world = make_world(ready_task(5))
        with mock.patch.dict(os.environ, {"OPL_TOKEN_SPARK": secret}):
            runner.tick(world)
            settle(runner)
            runner.tick(mark(world, 5, "In progress"))
        comments = [r for r in self.server.requests
                    if r["method"] == "POST" and r["path"].endswith("/activities")]
        self.assertTrue(comments, "expected a failure comment")
        for req in comments:
            raw = req["body"]["comment"]["raw"]
            self.assertNotIn(secret, raw)
            self.assertNotIn(encoded, raw)
        packets = os.path.join(self.tmp, "state", "packets")
        for name in os.listdir(packets):
            with open(os.path.join(packets, name), encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn(secret, text, name)
            self.assertNotIn(encoded, text, name)


class PrepFailureTests(RunnerHarness):
    def test_failed_worktree_blocks_instead_of_stranding(self):
        # PR #6 review: if the worktree can't be made, the task must not be
        # left In progress (no build would pick it up again) and the
        # conductor must keep running.
        import opl.conductor.spark.runner as runner_mod

        def broken(*args, **kwargs):
            raise RuntimeError("no space left on device")

        runner = self._runner("worker_ok.py")
        world = make_world(ready_task(5))
        with mock.patch.object(runner_mod, "create_worktree", broken):
            runner.tick(world)
            settle(runner)
            actions = runner.tick(mark(world, 5, "Blocked"))
        self.assertTrue(any("not started" in a for a in actions), actions)
        statuses = [b.get("_links", {}).get("status", {}).get("href", "")
                    for p, b in self.patches if p.endswith("/work_packages/5")]
        # Never moved to In progress; only to Blocked.
        self.assertTrue(statuses)
        self.assertEqual(set(statuses), {"/api/v3/statuses/33"})
        comments = [b["comment"]["raw"] for p, b in self.posts
                    if p.endswith("/work_packages/5/activities")]
        self.assertTrue(any("could not prepare the worktree" in c for c in comments))


class SuccessTests(RunnerHarness):
    def test_success_pushes_pr_moves(self):
        runner = self._runner("worker_commit.py")
        world = make_world(ready_task(5))
        runner.tick(world)
        settle(runner)
        actions = runner.tick(mark(world, 5, "In review"))
        self.assertTrue(actions)
        branches = self._branches()
        self.assertIn("opl/task-T5-", branches)
        pulls = [r for r in self.server.requests
                 if r["method"] == "POST" and r["path"] == "/repos/example-owner/demo/pulls"]
        self.assertEqual(len(pulls), 1)
        self.assertTrue(pulls[0]["body"]["head"].startswith("opl/task-T5-"))
        self.assertEqual(pulls[0]["body"]["base"], "main")
        body = pulls[0]["body"]["body"]
        self.assertIn("/work_packages/5", body)
        self.assertIn("Feature", body)
        pr_url = self.server.base_url + "/example-owner/demo/pull/9"
        link_patches = [b for p, b in self.patches
                        if "customField50" in b]
        self.assertEqual(len(link_patches), 1)
        self.assertEqual(link_patches[0]["customField50"], pr_url)
        self.assertIn("/api/v3/statuses/32",
                      link_patches[0]["_links"]["status"]["href"])
        versions = [b["lockVersion"] for p, b in self.patches
                    if p == "/api/v3/work_packages/5"]
        self.assertEqual(versions, [3, 4])
        with open(os.path.join(self.tmp, "state", "packets", "task-5-1.md"),
                  encoding="utf-8") as fh:
            packet = fh.read()
        self.assertIn("Build thing", packet)
        self.assertIn("Do it well", packet)
        self.assertIn("Why: x", packet)

    def test_rerun_reuses_open_pr(self):        # TH.11: a re-run after a partial success (push done, link lost)
        # reuses the open PR from the same head instead of opening another.
        runner = self._runner("worker_commit.py")
        world = make_world(ready_task(5))
        runner.tick(world)
        settle(runner)
        existing = self.server.base_url + "/example-owner/demo/pull/9"

        def list_pulls(method, path, query, body, headers):
            return 200, [{"html_url": existing, "number": 9,
                          "head": {"ref": "opl/task-T5-x"},
                          "base": {"ref": "main"}}]

        self.server.add("GET", "/repos/example-owner/demo/pulls",
                        handler=list_pulls)
        actions = runner.tick(mark(world, 5, "In review"))
        posts = [r for r in self.server.requests
                 if r["method"] == "POST"
                 and r["path"] == "/repos/example-owner/demo/pulls"]
        self.assertEqual(posts, [])
        link_patches = [b for p, b in self.patches
                        if "customField50" in b]
        self.assertEqual(len(link_patches), 1)
        self.assertEqual(link_patches[0]["customField50"], existing)
        self.assertTrue(any("reusing open PR" in a for a in actions),
                        actions)

    def test_create_422_falls_back_to_relookup(self):
        # TH.18: the PR was opened between lookup and create (422) —
        # look it up again from the head and use it instead of raising.
        existing = self.server.base_url + "/example-owner/demo/pull/9"

        def list_pulls(method, path, query, body, headers):
            return 200, [{"html_url": existing, "number": 9,
                          "head": {"ref": "opl/task-T5-x"},
                          "base": {"ref": "main"}}]

        self.server.add("GET", "/repos/example-owner/demo/pulls",
                        handler=list_pulls)

        def create_422(method, path, query, body, headers):
            return 422, {"message": "A pull request already exists"}

        self.server.add("POST", "/repos/example-owner/demo/pulls",
                        handler=create_422)
        runner = self._runner("worker_commit.py")
        world = make_world(ready_task(5))
        runner.tick(world)
        settle(runner)
        actions = runner.tick(mark(world, 5, "In review"))
        link_patches = [b for p, b in self.patches
                        if "customField50" in b]
        self.assertEqual(len(link_patches), 1)
        self.assertEqual(link_patches[0]["customField50"], existing)
        self.assertTrue(any("reusing open PR" in a for a in actions),
                        actions)

    def test_stale_parent_gives_no_worker(self):
        # TH.18: dispatch passed on the collected world, but the live
        # re-read finds the feature back at Proposed — no worker starts,
        # no packet or worktree, task Blocked with the reason.
        self.server.add("GET", "/api/v3/work_packages/2", body={
            "id": 2, "subject": "Feature", "description": "Why: x",
            "lockVersion": 1,
            "_links": {
                "status": {"href": "/api/v3/statuses/37"},
                "type": {"href": "/api/v3/types/12"},
                "project": {"href": "%s/api/v3/projects/1"
                                      % self.server.base_url}}})
        runner = self._runner("worker_commit.py")
        world = make_world(ready_task(5))
        runner.tick(world)
        settle(runner)
        actions = runner.tick(world)
        packets = os.path.join(self.tmp, "state", "packets")
        self.assertFalse(os.path.isdir(packets) and os.listdir(packets),
                         "a worker was started")
        self.assertFalse(os.path.isdir(
            os.path.join(self.tmp, "state", "worktrees")))
        blocked = [b for p, b in self.patches
                   if b.get("_links", {}).get("status", {}).get("href", "")
                   .endswith("/33")]
        self.assertTrue(blocked)
        comments = [r["body"]["comment"]["raw"] for r in self.server.requests
                    if r["method"] == "POST"
                    and r["path"].endswith("/5/activities")]
        self.assertTrue(any("Not started (build run)" in c for c in comments),
                        comments)
        self.assertTrue(any("not started" in a for a in actions),
                        actions)

    def test_all_runs_share_one_spark_data_dir(self):
        # TH.18 item 5: every Spark run logs OpenCode usage into the same
        # data dir while HOME stays per run.
        runner = self._runner("worker_commit.py")
        env = runner._worker_env(os.path.join(self.tmp, "state", "logs",
                                              "run-5-1.log"))
        data = os.path.join(self.tmp, "state", "spark-data")
        self.assertEqual(env["XDG_DATA_HOME"], data)
        self.assertTrue(os.path.isdir(data))
        self.assertNotEqual(env["HOME"], data)
        self.assertTrue(env["HOME"].startswith(
            os.path.join(self.tmp, "state", "homes")))
        other = runner._worker_env(os.path.join(self.tmp, "state", "logs",
                                                "run-5-2.log"))
        self.assertEqual(other["XDG_DATA_HOME"], data)
        self.assertNotEqual(other["HOME"], env["HOME"])


class FailTests(RunnerHarness):
    def test_failed_retries_once_then_blocks(self):
        runner = self._runner("worker_fail.py")
        world = make_world(ready_task(5))
        runner.tick(world)
        settle(runner)
        actions = runner.tick(mark(world, 5, "Blocked"))
        blocked = [b for p, b in self.patches
                   if b.get("_links", {}).get("status", {}).get("href", "").endswith("/33")]
        self.assertTrue(blocked)
        comments = [b for p, b in self.posts if p.endswith("/activities")]
        self.assertTrue(comments)
        self.assertIn("failed twice", comments[-1]["comment"]["raw"])
        logs = []
        for root, _dirs, files in os.walk(os.path.join(self.tmp, "state", "logs")):
            logs.extend(f for f in files if f.startswith("run-5-"))
        self.assertEqual(sorted(logs), ["run-5-1.log", "run-5-2.log"])
        self.assertTrue(any("failed twice" in a for a in actions))

    def test_uncommitted_counts_as_failed(self):
        runner = self._runner("worker_dirty.py")
        world = make_world(ready_task(5))
        runner.tick(world)
        settle(runner)
        runner.tick(mark(world, 5, "Blocked"))
        blocked = [b for p, b in self.patches
                   if b.get("_links", {}).get("status", {}).get("href", "").endswith("/33")]
        self.assertTrue(blocked)
        pulls = [r for r in self.server.requests
                 if r["method"] == "POST" and r["path"].endswith("/pulls")]
        self.assertEqual(pulls, [])

    def test_missing_result_line_counts_as_failed(self):
        runner = self._runner("worker_noresult.py")
        world = make_world(ready_task(5))
        runner.tick(world)
        settle(runner)
        runner.tick(mark(world, 5, "Blocked"))
        blocked = [b for p, b in self.patches
                   if b.get("_links", {}).get("status", {}).get("href", "").endswith("/33")]
        self.assertTrue(blocked)
        comments = [b for p, b in self.posts if p.endswith("/activities")]
        self.assertTrue(any("missing OPL-RESULT line" in b["comment"]["raw"]
                            for b in comments))

    def test_failed_result_line_counts_as_failed(self):
        runner = self._runner("worker_failedline.py")
        world = make_world(ready_task(5))
        runner.tick(world)
        settle(runner)
        runner.tick(mark(world, 5, "Blocked"))
        blocked = [b for p, b in self.patches
                   if b.get("_links", {}).get("status", {}).get("href", "").endswith("/33")]
        self.assertTrue(blocked)
        comments = [b for p, b in self.posts if p.endswith("/activities")]
        self.assertTrue(any("kaboom" in b["comment"]["raw"] for b in comments))

    def test_stale_snapshot_version_is_refreshed(self):
        # Someone else moved the task: the snapshot says 3, the server is at 99.
        self.wp_versions[5] = 99
        runner = self._runner("worker_commit.py")
        world = make_world(ready_task(5))
        runner.tick(world)
        settle(runner)
        actions = runner.tick(mark(world, 5, "In review"))
        self.assertTrue(actions)
        versions = [b["lockVersion"] for p, b in self.patches
                    if p == "/api/v3/work_packages/5"]
        self.assertEqual(versions[0], 99)


class TimeoutTests(RunnerHarness):
    def test_timeout_returns_fast_then_blocks(self):
        runner = self._runner(
            "worker_hang.py",
            limits={"S": 0.05, "M": 0.05, "L": 0.05, "review": 1, "test": 1,
                    "stall": 30})
        world = make_world(ready_task(5))
        started = time.monotonic()
        actions = runner.tick(world)
        elapsed = time.monotonic() - started
        # The 3s worker is still running: a blocking tick could not return.
        self.assertLess(elapsed, 2.5)
        self.assertEqual(actions, [])
        self.assertTrue(runner._active)
        time.sleep(max(0.0, 5.0 - elapsed))
        actions = runner.tick(mark(world, 5, "Blocked"))
        blocked = [b for p, b in self.patches
                   if b.get("_links", {}).get("status", {}).get("href", "").endswith("/33")]
        self.assertEqual(len(blocked), 1)
        comments = [b for p, b in self.posts if p.endswith("/activities")]
        self.assertTrue(any("timeout after" in b["comment"]["raw"] for b in comments))
        self.assertTrue(any("timeout" in a for a in actions))

    def test_timeout_commits_partial_work_and_names_branch(self):
        runner = self._runner(
            "worker_dirty_hang.py",
            limits={"S": 0.05, "M": 0.05, "L": 0.05, "review": 1, "test": 1,
                    "stall": 30})
        world = make_world(ready_task(5))
        runner.tick(world)
        time.sleep(5.0)
        actions = runner.tick(mark(world, 5, "Blocked"))
        comments = [b for p, b in self.posts if p.endswith("/activities")]
        self.assertEqual(len(comments), 1)
        raw = comments[0]["comment"]["raw"]
        self.assertIn("timeout after", raw)
        branch = [seg for seg in raw.split() if seg.startswith("opl/task-T5-")]
        self.assertEqual(len(branch), 1)
        wt_root = os.path.join(self.tmp, "state", "worktrees")
        leaves = []
        for root, dirs, _files in os.walk(wt_root):
            if ".git" in dirs or ".git" in _files:
                leaves.append(root)
                dirs[:] = []
        self.assertEqual(len(leaves), 1)
        log = subprocess.run(
            ["git", "-C", leaves[0], "log", "--oneline"],
            capture_output=True, text=True, timeout=60, check=True)
        self.assertIn("WIP: partial work, run 5-1", log.stdout)
        self.assertTrue(any("timeout" in a for a in actions))


class GateTests(RunnerHarness):
    def test_private_never_started(self):
        runner = self._runner("worker_commit.py")
        world = make_world(ready_task(6, project="priv"))
        actions = runner.tick(world)
        self.assertEqual(actions, [])
        pulls = [r for r in self.server.requests
                 if r["method"] == "POST" and r["path"].endswith("/pulls")]
        self.assertEqual(pulls, [])

    def test_slot_cap_respected(self):
        runner = self._runner("worker_commit.py", slots_max=1)
        world = make_world(ready_task(5), ready_task(6))
        runner.tick(world)
        settle(runner)
        packets = os.listdir(os.path.join(self.tmp, "state", "packets"))
        self.assertEqual(packets, ["task-5-1.md"])
        runner.tick(mark(world, 5, "In review"))
        settle(runner)
        marked = mark(world, 5, "In review")
        runner.tick(mark(marked, 6, "In review"))
        started = sorted(
            b["customField50"] for p, b in self.patches if "customField50" in b)
        self.assertEqual(len(started), 2)

    def test_push_env_token_handling(self):
        from opl.conductor.spark.runner import _push_env

        token = "s3cr3t-token-xyz"
        env = _push_env("https://github.com/o/r", token)
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["GIT_CONFIG_KEY_0"],
                         "http.https://github.com/.extraheader")
        value = env["GIT_CONFIG_VALUE_0"]
        self.assertNotIn(token, value)
        import base64

        encoded = value.split("basic ")[1]
        self.assertEqual(base64.b64decode(encoded).decode("ascii"),
                         "x-access-token:" + token)
        self.assertEqual(_push_env("/tmp/origin.git", token), {})
        self.assertEqual(_push_env("https://github.com/o/r", None), {})

    def test_failing_github_push_leaks_no_secret(self):
        import base64
        from unittest import mock

        # A local bare repo that rejects every push, mapped over the
        # github.com URL: the push fails fast with no network and no
        # credential prompt, while the URL still exercises the token path.
        bare = os.path.join(self.tmp, "reject.git")
        subprocess.run(["git", "init", "-q", "--bare", bare],
                       check=True, timeout=60)
        hook = os.path.join(bare, "hooks", "pre-receive")
        with open(hook, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("#!/bin/sh\nexit 1\n")
        os.chmod(hook, 0o755)
        # Empty global config (isolation from the machine's credential
        # helpers); the repo keeps its configured github.com origin (TH.6
        # checks it) and only its redirect moves to the rejecting repo.
        cfg = os.path.join(self.tmp, "gitconfig")
        with open(cfg, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("")
        subprocess.run(
            ["git", "-C", self.repo, "config", "--unset-all",
             "url.%s.insteadOf" % self.origin.replace("\\", "/")],
            check=True, timeout=60)
        point_origin(self.repo, bare, "example-owner/demo")
        runner = self._runner("worker_commit.py")
        world = make_world(ready_task(5))
        runner.tick(world)
        settle(runner)
        token = "gh-token"
        encoded = base64.b64encode(
            ("x-access-token:" + token).encode("ascii")).decode("ascii")
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": cfg,
                                             "GIT_CONFIG_NOSYSTEM": "1",
                                             "GIT_TERMINAL_PROMPT": "0",
                                             "GCM_INTERACTIVE": "never"}):
            with self.assertRaises(RuntimeError) as ctx:
                runner.tick(mark(world, 5, "In review"))
        message = str(ctx.exception)
        self.assertNotIn(token, message)
        self.assertNotIn(encoded, message)
        pushed = subprocess.run(
            ["git", "--git-dir", bare, "branch", "--list", "opl/*"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(pushed.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
