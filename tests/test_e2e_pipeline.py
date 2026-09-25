#!/usr/bin/env python3
"""End to end: one task from approved feature to merged PR, real code only.

The real `opl-conductor` main loop (`--once --live`, one call per cycle)
with the real rules, engine, collector, Spark runner and git, against a
stateful fake OpenProject (roles, workflows and a journal from
config/pm-model.toml) and a fake GitHub backed by a local bare repo.
Checks the path Draft -> Ready -> In progress -> In review -> reviewed
(Pass bound to the head SHA) -> merged at that SHA -> Merged, who made
each move, and that no role ever attempted a forbidden transition.

What it does NOT prove (Codex review of TH.23): live OpenProject
permissions and localisation (journal details are English, access checks
are selective), real GitHub merging, rulesets or failing CI, fork
handling, real model behaviour (the worker is scripted), or costs (ccusage
is faked). The fakes follow v17 shapes where the pipeline depends on them:
plain schemas without allowed values, options only on forms.
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import opl.conductor.__main__ as conductor_main
from opl.github import GitHub
from opl.model import load as load_model
from tests.fakes.ccusage_fake import no_real_ccusage
from tests.fakes.http_fake import FakeServer
from tests.fakes.op_world import GitHubWorld, OpenProjectWorld, Router
from tests.gitfixture import point_origin

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "tests", "fakes", "worker_e2e.py")

TOKENS = {"claude": "tok-claude-e2e", "codex": "tok-codex-e2e",
          "spark": "tok-spark-e2e", "conductor": "tok-conductor-e2e",
          "owner": "tok-owner-e2e"}

CONFIG = """
[openproject]
url = "%(url)s"
admin_token_env = "OPL_TOKEN_ADMIN"
owner_login = "owner"

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
live = true
interval_seconds = 60
state_dir = "%(state)s"

[runner]
command = [%(python)s, %(worker)s, "{packet}"]
max_parallel = 2
worker_env = ["OPL_E2E_REVIEW"]

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
local_repo = "%(repo)s"
base_ref = "main"
pr_base = "main"
"""


def git(*args, cwd=None, env=None):
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                         text=True, timeout=120, env=env)
    if out.returncode != 0:
        raise AssertionError("git %s failed: %s" % (args, out.stderr))
    return out.stdout.strip()


def toml_str(path):
    return '"%s"' % path.replace("\\", "/")


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opl-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.git_env = dict(os.environ, GIT_TERMINAL_PROMPT="0",
                            GCM_INTERACTIVE="never",
                            GIT_CONFIG_GLOBAL=os.path.join(self.tmp, "gitconfig"),
                            GIT_AUTHOR_NAME="owner", GIT_AUTHOR_EMAIL="o@example.invalid",
                            GIT_COMMITTER_NAME="owner",
                            GIT_COMMITTER_EMAIL="o@example.invalid")
        open(self.git_env["GIT_CONFIG_GLOBAL"], "w").close()
        self.bare = os.path.join(self.tmp, "origin.git")
        self.repo = os.path.join(self.tmp, "demo")
        git("init", "-q", "--bare", "-b", "main", self.bare, env=self.git_env)
        git("init", "-q", "-b", "main", self.repo, env=self.git_env)
        with open(os.path.join(self.repo, "README.md"), "w") as fh:
            fh.write("demo\n")
        git("add", "README.md", cwd=self.repo, env=self.git_env)
        git("commit", "-qm", "init", cwd=self.repo, env=self.git_env)
        point_origin(self.repo, self.bare, "example-owner/demo", env=self.git_env)
        git("push", "-q", "origin", "main", cwd=self.repo, env=self.git_env)
        git("fetch", "-q", "origin", cwd=self.repo, env=self.git_env)

        self.server = FakeServer()
        self.server.handlers = Router()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        model = load_model(os.path.join(ROOT, "config", "pm-model.toml"))
        self.op = OpenProjectWorld(model, TOKENS)
        self.op.install(self.server.handlers)
        self.gh = GitHubWorld(self.bare)
        self.gh.install(self.server.handlers)

        self.state = os.path.join(self.tmp, "state")
        cfg = os.path.join(self.tmp, "cfg")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "opl.toml"), "w", encoding="utf-8") as fh:
            fh.write(CONFIG % {"url": self.server.base_url,
                               "state": self.state.replace("\\", "/"),
                               "python": toml_str(sys.executable),
                               "worker": toml_str(WORKER),
                               "repo": self.repo.replace("\\", "/")})
        self.env = {"OPL_CONFIG_DIR": cfg,
                    "OPL_TOKEN_CLAUDE": TOKENS["claude"],
                    "OPL_TOKEN_CODEX": TOKENS["codex"],
                    "OPL_TOKEN_SPARK": TOKENS["spark"],
                    "OPL_TOKEN_CONDUCTOR": TOKENS["conductor"],
                    "OPL_GITHUB_TOKEN": "gh-e2e-token",
                    "GIT_CONFIG_GLOBAL": self.git_env["GIT_CONFIG_GLOBAL"],
                    "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}

        # An approved feature with one Low-risk Spark task, reviewed by
        # Spark (allowed for Low risk in a Public project).
        op = self.op
        op.create(10, "Registration", "Epic", "Open")
        op.create(11, "Sign-up form", "Feature", "Proposed", parent=10)
        op.act("owner", 11, status="Approved")
        op.create(12, "Build the form", "Task", "Draft", parent=11,
                  author="claude", assignee="spark",
                  **{"Reviewer": op.uid["spark"], "Size": "S", "Risk": "Low"})

    def cycle(self):
        base = self.server.base_url
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env), no_real_ccusage(), \
                mock.patch.object(conductor_main, "GitHub",
                                  lambda token: GitHub(token, base_url=base)), \
                redirect_stdout(out), redirect_stderr(err):
            os.environ.pop("OPL_TOKEN_ADMIN", None)
            rc = conductor_main.main(["--once", "--live"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual(self.op.rejected, [], "forbidden transition attempted")
        return out.getvalue(), err.getvalue()

    def moves(self, wid):
        return [(who, d) for who, details, _ in self.op.history(wid)
                for d in details if d.startswith("Status ")]

    def comments(self, wid, who=None):
        return [c for w, _, c in self.op.history(wid) if c and (who is None or w == who)]

    def run_until(self, predicate, limit=8):
        for _ in range(limit):
            if predicate():
                return
            self.cycle()
        self.assertTrue(predicate(), "pipeline stalled: %s"
                        % self.op.history(12))

    def test_task_flows_from_draft_to_merged(self):
        self.cycle()
        self.assertEqual(self.op.status_of(12), "Ready")
        self.run_until(lambda: self.op.status_of(12) == "Merged")

        self.assertEqual(self.moves(12), [
            ("conductor", "Status changed from Draft to Ready"),
            ("spark", "Status changed from Ready to In progress"),
            ("spark", "Status changed from In progress to In review"),
            ("conductor", "Status changed from In review to Merged"),
        ])
        # The feature moved on when work started.
        self.assertIn(("conductor", "Status changed from Approved to Building"),
                      self.moves(11))
        # One PR, merged exactly at the SHA Spark's review recorded.
        (number, merged_sha), = self.gh.merges
        reviewed = [c for c in self.comments(12, "spark") if "reviewed: " in c]
        self.assertEqual(len(reviewed), 1)
        self.assertIn("reviewed: %s" % merged_sha, reviewed[0])
        self.assertEqual(self.op.wps[12]["values"]["Review result"], "Pass")
        self.assertEqual(self.op.wps[12]["values"]["PR link"], self.gh.url(number))
        self.assertEqual(self.op.conflicts, [])
        # List fields (Review result, Action, Models) were resolved through
        # the edit form: the plain schema carries no allowed values.
        self.assertIn(12, self.op.forms)
        # Nobody used an admin token; only the conductor and Spark wrote.
        writers = {self.op.who(r["headers"]) for r in self.server.requests
                   if r["method"] in ("PATCH", "POST") and r["path"].startswith("/api/v3")}
        self.assertEqual(writers, {self.op.uid["conductor"], self.op.uid["spark"]})

    def test_a_push_after_review_forces_a_fresh_review(self):
        self.run_until(lambda: self.op.wps[12]["values"].get("Review result") == "Pass")
        first = self.gh.head_sha(self.gh.pulls[9]["head"])
        # Someone pushes to the PR branch after the review.
        tree = git("--git-dir", self.bare, "rev-parse", first + "^{tree}",
                   env=self.git_env)
        moved = git("--git-dir", self.bare, "commit-tree", tree, "-p", first,
                    "-m", "late push", env=self.git_env)
        git("--git-dir", self.bare, "update-ref",
            "refs/heads/" + self.gh.pulls[9]["head"], moved, env=self.git_env)

        self.cycle()
        self.assertEqual(self.gh.merges, [])
        self.assertIsNone(self.op.wps[12]["values"].get("Review result"))
        self.assertTrue(any("Re-review" in c for c in self.comments(12, "conductor")))

        self.run_until(lambda: self.op.status_of(12) == "Merged")
        self.assertEqual(self.gh.merges, [(9, moved)])
        reviewed = [c for c in self.comments(12, "spark") if "reviewed: " in c]
        self.assertEqual([c.split("reviewed: ")[1].strip() for c in reviewed],
                         [first, moved])

    def test_changes_requested_then_fixed_then_merged(self):
        self.run_until(lambda: self.op.status_of(12) == "In review")
        self.env["OPL_E2E_REVIEW"] = "changes"
        self.cycle()
        self.assertEqual(self.op.status_of(12), "In progress")
        self.assertEqual(self.op.wps[12]["values"]["Review result"],
                         "Changes requested")
        self.assertTrue(any("add a test" in c for c in self.comments(12, "spark")))
        del self.env["OPL_E2E_REVIEW"]
        self.run_until(lambda: self.op.status_of(12) == "Merged")
        # One PR; the fix landed on its branch; the merge is at the head
        # the second (passing) review recorded.
        self.assertEqual(len(self.gh.pulls), 1)
        (number, merged_sha), = self.gh.merges
        reviewed = [c for c in self.comments(12, "spark") if "reviewed: " in c]
        self.assertEqual(len(reviewed), 1)
        self.assertIn(merged_sha, reviewed[0])
        log = git("--git-dir", self.bare, "log", "--format=%s", merged_sha,
                  env=self.git_env)
        self.assertEqual(len([m for m in log.splitlines()
                              if m.startswith("spark: ")]), 2)

    # -- TH.22 (Codex F4): authority re-read immediately before a spawn --
    def during_preparation(self, target, change):
        """Run `change` right after `target` (a runner preparation step)
        returns, i.e. after the pre-run check but before the worker."""
        import opl.conductor.spark.runner as runner_mod

        real = getattr(runner_mod, target)

        def wrapped(*args, **kwargs):
            result = real(*args, **kwargs)
            change()
            return result

        return mock.patch.object(runner_mod, target, wrapped)

    def assert_not_started(self, kind, reason):
        self.assertEqual(self.op.status_of(12), "Blocked")
        self.assertTrue(any("Not started (%s run)" % kind in c and reason in c
                            for c in self.comments(12, "spark")),
                        self.comments(12))

    def pushed_branches(self):
        return git("--git-dir", self.bare, "branch", "--list", "opl/*",
                   env=self.git_env)

    def test_feature_unapproved_during_build_preparation(self):
        self.cycle()                                    # Draft -> Ready
        with self.during_preparation(
                "create_worktree",
                lambda: self.op.act("owner", 11, status="Proposed")):
            self.cycle()
        self.assert_not_started("build", "feature is Proposed")
        self.assertEqual(self.pushed_branches(), "")    # no worker ran

    def test_predecessor_added_during_build_preparation(self):
        self.op.create(13, "Schema first", "Task", "In progress", parent=11,
                       author="claude", assignee="claude",
                       **{"Reviewer": self.op.uid["codex"], "Size": "S",
                          "Risk": "Low"})
        self.cycle()
        self.assertEqual(self.op.status_of(12), "Ready")
        with self.during_preparation(
                "create_worktree",
                lambda: self.op.relations.append((13, 12))):
            self.cycle()
        self.assert_not_started("build", "predecessor #13 is not Merged")
        self.assertEqual(self.pushed_branches(), "")

    def test_reviewer_changed_during_review_preparation(self):
        self.run_until(lambda: self.op.status_of(12) == "In review")
        with self.during_preparation(
                "checkout_pr_head",
                lambda: self.op.act("owner", 12,
                                    **{"Reviewer": self.op.uid["codex"]})):
            self.cycle()
        self.assert_not_started("review", "changed while preparing: reviewer")
        self.assertIsNone(self.op.wps[12]["values"].get("Review result"))

    def review_trees(self):
        root = os.path.join(self.state, "worktrees")
        found = []
        for dirpath, dirnames, _files in os.walk(root):
            found += [d for d in dirnames if d.startswith("review-")]
        return found

    def test_pr_link_swapped_during_review_preparation(self):
        # Codex final2 #2: another PR of the same repo passes every
        # identity check, so which PR is reviewed must itself be part of
        # the authorization. #3: the refused run leaves no scratch tree.
        self.run_until(lambda: self.op.status_of(12) == "In review")
        other = self.gh.url(77)
        with self.during_preparation(
                "checkout_pr_head",
                lambda: self.op.act("claude", 12, **{"PR link": other})):
            self.cycle()
        self.assert_not_started("review", "PR link changed")
        self.assertIsNone(self.op.wps[12]["values"].get("Review result"))
        self.assertEqual(self.review_trees(), [])

    def test_packet_is_built_from_the_guards_own_read(self):
        # Codex final2 #1: the worker's prompt describes exactly the state
        # the spawn guard authorized; a change after the guard's read must
        # not reach the packet through a second fetch.
        import opl.conductor.spark.runner as runner_mod

        self.cycle()
        real = runner_mod.SparkRunner._guard_spawn

        def guard_then_edit(runner, record, item, world):
            elements = real(runner, record, item, world)
            self.op.wps[12]["subject"] = "EDITED AFTER THE GUARD"
            return elements

        with mock.patch.object(runner_mod.SparkRunner, "_guard_spawn",
                               guard_then_edit):
            self.cycle()
        packets = os.path.join(self.state, "packets")
        text = ""
        for name in os.listdir(packets):
            if name.startswith("task-12-"):
                with open(os.path.join(packets, name), encoding="utf-8") as fh:
                    text += fh.read()
        self.assertIn("Build the form", text)
        self.assertNotIn("EDITED AFTER THE GUARD", text)

    # The negative halves of the three TH.22 scenarios, in-tree: with the
    # spawn guard switched off, each change made during preparation goes
    # unnoticed and a worker runs. So the guard is what stops them.
    def no_guard(self):
        import opl.conductor.spark.runner as runner_mod

        return mock.patch.object(runner_mod.SparkRunner, "_guard_spawn",
                                 lambda runner, record, item, world: None)

    def test_without_the_guard_an_unapproval_would_not_stop_the_build(self):
        with self.no_guard():
            self.cycle()
            with self.during_preparation(
                    "create_worktree",
                    lambda: self.op.act("owner", 11, status="Proposed")):
                self.cycle()
        self.assertIn("opl/task-T12-", self.pushed_branches())

    def test_without_the_guard_a_new_predecessor_would_not_stop_the_build(self):
        self.op.create(13, "Schema first", "Task", "In progress", parent=11,
                       author="claude", assignee="claude",
                       **{"Reviewer": self.op.uid["codex"], "Size": "S",
                          "Risk": "Low"})
        with self.no_guard():
            self.cycle()
            with self.during_preparation(
                    "create_worktree",
                    lambda: self.op.relations.append((13, 12))):
                self.cycle()
        self.assertIn("opl/task-T12-", self.pushed_branches())

    def test_without_the_guard_a_new_reviewer_would_not_stop_the_review(self):
        self.run_until(lambda: self.op.status_of(12) == "In review")
        with self.no_guard(), self.during_preparation(
                "checkout_pr_head",
                lambda: self.op.act("owner", 12,
                                    **{"Reviewer": self.op.uid["codex"]})):
            self.cycle()
        self.assertEqual(self.op.wps[12]["values"].get("Review result"), "Pass")

    def test_pass_from_someone_else_blocks_instead_of_merging(self):
        self.run_until(lambda: self.op.status_of(12) == "In review")
        # Codex (not the task's reviewer) ticks Pass before Spark's review.
        self.op.act("codex", 12, **{"Review result": "Pass"})
        self.cycle()
        self.assertEqual(self.op.status_of(12), "Blocked")
        self.assertEqual(self.gh.merges, [])
        self.assertTrue(any("Unauthorised approval" in c
                            for c in self.comments(12, "conductor")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
