"""TH.14 offline canary tests: fake sudo only, never a privileged command."""

import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout

from opl import sandbox_check
from tests.test_static import sh_path

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests" / "fakebin" / "sudo"
BASH = shutil.which("bash")


@unittest.skipUnless(BASH, "bash needed for offline fake")
class SandboxCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opl-canary-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worktree = self.root / "worktree"
        self.config = self.root / "owner-config"
        self.worktree.mkdir()
        self.config.mkdir()
        (self.config / "opl.toml").write_text("# synthetic\n", encoding="utf-8")

    def run_check(self, fake_env=None, **targets):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            self.assertEqual(argv[:6], ["sudo", "-n", "-H", "-u",
                                       "synthetic-worker", "--"])
            self.assertEqual(kwargs["timeout"], 20)
            self.assertEqual(set(kwargs["env"]), {"PATH", "LANG"})
            # Always execute the checked-in fake via Bash, never resolve sudo.
            kwargs["env"] = dict(PATH=os.environ.get("PATH", ""), **(fake_env or {}))
            return subprocess.run([BASH, sh_path(str(FAKE)), *argv[1:]], **kwargs)

        out = io.StringIO()
        with redirect_stdout(out):
            rc = sandbox_check.check("synthetic-worker", self.worktree,
                 targets.get("outside", self.root), targets.get("config", self.config),
                 run=fake_run, owner_uid=12345)
        return rc, out.getvalue(), calls

    def test_all_denials_pass_and_cleanup(self):
        rc, text, calls = self.run_check()
        self.assertEqual(rc, 0, text)
        self.assertEqual(text.count("PASS:"), 5)
        self.assertEqual(len(calls), 5)
        self.assertEqual(list(self.root.glob(".opl-canary-*")), [])

    def test_each_forbidden_access_fails(self):
        for kind in ("private-read", "outside-write", "owner-config"):
            with self.subTest(kind=kind):
                rc, text, calls = self.run_check({"FAKE_ALLOW": kind})
                self.assertEqual(rc, 1)
                self.assertEqual(text.count("FAIL:"), 1)
                self.assertEqual(len(calls), 5)

    def test_same_owner_or_root_identity_is_failure(self):
        for uid in ("0", "12345", "not-a-uid"):
            with self.subTest(uid=uid):
                rc, text, calls = self.run_check({"FAKE_WORKER_UID": uid})
                self.assertEqual(rc, 1)
                self.assertNotIn("PASS:", text)
                self.assertEqual(len(calls), 1)

    def test_sudo_failure_is_not_permission_denial(self):
        rc, text, _ = self.run_check({"FAKE_SUDO_FAILURE": "1"})
        self.assertEqual(rc, 1)
        self.assertNotIn("PASS:", text)

    def test_broken_interpreter_or_positive_control_is_failure(self):
        for env in ({"FAKE_PROBE_ERROR": "1"}, {"FAKE_INSIDE_DENIED": "1"}):
            with self.subTest(env=env):
                rc, text, calls = self.run_check(env)
                self.assertEqual(rc, 1)
                self.assertEqual(len(calls), 2)
                self.assertEqual(text.count("FAIL:"), 4)

    def test_missing_config_and_inside_target_fail_before_sudo(self):
        for args in ({"config": self.root / "missing"}, {"outside": self.worktree}):
            with self.subTest(args=args):
                rc, text, calls = self.run_check(**args)
                self.assertEqual(rc, 1)
                self.assertEqual(calls, [])
                self.assertNotIn("PASS:", text)

    def test_fake_rejects_unexpected_arguments(self):
        proc = subprocess.run([BASH, sh_path(str(FAKE)), "-u", "root", "id"],
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 97)

    def test_timeout_is_failure_without_error_contents(self):
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired("synthetic", 20)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = sandbox_check.check("synthetic-worker", self.worktree, self.root,
                                     self.config, run=timeout, owner_uid=12345)
        self.assertEqual(rc, 1)
        self.assertNotIn(str(self.root), out.getvalue())
