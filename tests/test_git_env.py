#!/usr/bin/env python3
"""Tests: every git subprocess runs non-interactive, helper intact."""

import os
import unittest
from unittest import mock

from opl.conductor.spark.worktree import GIT_ENV, git_env


class GitEnvTests(unittest.TestCase):
    def test_prompts_disabled(self):
        self.assertEqual(GIT_ENV["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(GIT_ENV["GCM_INTERACTIVE"], "never")

    def test_credential_helper_not_blanked(self):
        self.assertNotIn("credential.helper", GIT_ENV)
        for value in GIT_ENV.values():
            self.assertNotIn("credential.helper", value)

    def test_extra_merges(self):
        env = git_env({"GIT_CONFIG_COUNT": "1"})
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_worktree_git_passes_env(self):
        from opl.conductor.spark import worktree

        with mock.patch.object(worktree.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="ok\n",
                                         stderr="")
            worktree._git("/tmp/repo", "status")
        _args, kwargs = run.call_args
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(kwargs["env"]["GCM_INTERACTIVE"], "never")

    def test_runner_sh_passes_env_and_extra(self):
        from opl.conductor.spark import runner

        with mock.patch.object(runner.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="ok\n",
                                         stderr="")
            runner._sh(["git", "x", "y"], "/tmp/repo", env={"A": "B"})
        _args, kwargs = run.call_args
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(kwargs["env"]["GCM_INTERACTIVE"], "never")
        self.assertEqual(kwargs["env"]["A"], "B")

    def test_outcomes_pass_env(self):
        import subprocess

        from opl.conductor.spark import outcomes

        with mock.patch.object(subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="",
                                         stderr="")
            outcomes.keep_partial_work("/tmp/repo", 5, 1)
        self.assertTrue(run.call_count >= 1)
        for call in run.call_args_list:
            _args, kwargs = call
            self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")

if __name__ == "__main__":
    unittest.main(verbosity=2)
