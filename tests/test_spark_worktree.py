#!/usr/bin/env python3
"""Tests for worktree create/remove against a temporary local git repo."""

import os
import shutil
import subprocess
import tempfile
import unittest

from opl.conductor.spark.worktree import (
    Worktree,
    checkout_pr_head,
    checkout_worktree,
    create_worktree,
    dispose_worktree,
    remove_worktree,
)

GIT = shutil.which("git")


@unittest.skipUnless(GIT, "git required for worktree tests")
class WorktreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opl-wt-")
        self.repo = os.path.join(self.tmp, "repo")
        os.mkdir(self.repo)
        self.state = os.path.join(self.tmp, "state")
        os.mkdir(self.state)
        subprocess.run([GIT, "init", "-q", "-b", "main", self.repo], check=True, timeout=60)
        subprocess.run([GIT, "-C", self.repo, "config", "user.email", "t@t"],
                       check=True, timeout=60)
        subprocess.run([GIT, "-C", self.repo, "config", "user.name", "t"],
                       check=True, timeout=60)
        with open(os.path.join(self.repo, "README.md"), "w", newline="\n") as fh:
            fh.write("hi\n")
        subprocess.run([GIT, "-C", self.repo, "add", "-A"], check=True, timeout=60)
        subprocess.run([GIT, "-C", self.repo, "commit", "-qm", "init"],
                       check=True, timeout=60)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_and_remove(self):
        worktree = create_worktree(self.repo, "main", "T12", self.state)
        self.assertIsInstance(worktree, Worktree)
        self.assertTrue(worktree.branch.startswith("opl/task-T12-"))
        self.assertTrue(os.path.isfile(os.path.join(worktree.path, "README.md")))
        listed = subprocess.run(
            [GIT, "-C", self.repo, "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=60, check=True)
        posix_path = worktree.path.replace(os.sep, "/")
        self.assertIn(posix_path, listed.stdout)
        self.assertIn(worktree.branch, listed.stdout)
        remove_worktree(worktree)
        listed = subprocess.run(
            [GIT, "-C", self.repo, "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=60, check=True)
        self.assertNotIn(posix_path, listed.stdout)
        self.assertFalse(os.path.exists(worktree.path))

    # -- TH.4: disposal keeps evidence, never forces ---------------------
    def test_dispose_keeps_failed_run(self):
        worktree = checkout_worktree(self.repo, "main", "review-5", self.state)
        kept = dispose_worktree(worktree, self.state, succeeded=False)
        self.assertEqual(kept, worktree.path)
        self.assertTrue(os.path.isdir(worktree.path))

    def test_dispose_keeps_dirty_tree_even_on_success(self):
        worktree = checkout_worktree(self.repo, "main", "test-6", self.state)
        with open(os.path.join(worktree.path, "stray.txt"), "w") as fh:
            fh.write("a worker edited this\n")
        kept = dispose_worktree(worktree, self.state, succeeded=True)
        self.assertEqual(kept, worktree.path)
        self.assertTrue(os.path.isfile(os.path.join(worktree.path, "stray.txt")))

    def test_dispose_removes_clean_success_without_force(self):
        worktree = checkout_worktree(self.repo, "main", "review-7", self.state)
        self.assertIsNone(dispose_worktree(worktree, self.state, succeeded=True))
        self.assertFalse(os.path.exists(worktree.path))

    def test_dispose_refuses_paths_outside_the_state_dir(self):
        outside = Worktree(path=self.repo, branch="main")
        with self.assertRaises(RuntimeError):
            dispose_worktree(outside, self.state, succeeded=True)
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "README.md")))

    # -- TH.8: exact-SHA PR checkout -----------------------------------
    def test_pr_head_checks_out_exact_sha(self):
        sha = subprocess.run(
            [GIT, "-C", self.repo, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=60,
            check=True).stdout.strip()
        worktree = checkout_pr_head(self.repo, self.repo, sha, "review-5",
                                    self.state)
        try:
            got = subprocess.run(
                [GIT, "-C", worktree.path, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=60,
                check=True).stdout.strip()
            self.assertEqual(got, sha)
            self.assertTrue(worktree.branch.startswith("refs/opl/review-5-"))
        finally:
            remove_worktree(worktree)

    def test_pr_head_fetch_failure_aborts(self):
        with self.assertRaises(RuntimeError) as ctx:
            checkout_pr_head(self.repo, self.repo, "0" * 40, "review-5",
                             self.state)
        self.assertIn("could not fetch", str(ctx.exception))
        leaves = []
        for root, dirs, _files in os.walk(os.path.join(self.state,
                                                       "worktrees")):
            leaves.extend(d for d in dirs if d.startswith("review-5-"))
        self.assertEqual(leaves, [])

    def test_pr_head_rejects_non_sha(self):
        with self.assertRaises(RuntimeError):
            checkout_pr_head(self.repo, self.repo, "feature-branch",
                             "review-5", self.state)

    def test_delete_ref_removes_isolated_ref(self):
        from opl.conductor.spark.worktree import delete_ref

        sha = subprocess.run(
            [GIT, "-C", self.repo, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=60,
            check=True).stdout.strip()
        worktree = checkout_pr_head(self.repo, self.repo, sha, "review-5",
                                    self.state)
        try:
            self.assertTrue(delete_ref(self.repo, worktree.branch))
            gone = subprocess.run(
                [GIT, "-C", self.repo, "for-each-ref", "refs/opl"],
                capture_output=True, text=True, timeout=60,
                check=True).stdout.strip()
            self.assertEqual(gone, "")
        finally:
            remove_worktree(worktree)

    def test_delete_ref_missing_is_false(self):
        from opl.conductor.spark.worktree import delete_ref

        self.assertFalse(delete_ref(self.repo, "refs/opl/no-such-ref"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
