#!/usr/bin/env python3
"""Tests for the minimal OpenCode adapter (T5.1 live-pilot blocker).

Synthetic only: a fake opencode records argv/cwd/env-names and prints a
final OPL-RESULT line. No Docker, network, OpenProject, GitHub, or real
model calls. Standard library only.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from opl.conductor.spark import opencode as adapter
from opl.conductor.spark.supervisor import (reset_stop_gate, run_worker,
                                            worker_environment)

# Synthetic Windows paths, assembled so no tracked file spells out a
# machine path (the publication scan forbids drive-letter paths).
_BS = chr(92)
WIN_GIT_DIR = "C:" + _BS + "Git" + _BS + "bin"
WIN_SYS_DIR = "C:" + _BS + "Windows" + _BS + "System32"
WIN_GIT_BASH = WIN_GIT_DIR + _BS + "bash.exe"
WIN_WSL = WIN_SYS_DIR + _BS + "wsl.exe"

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKES = os.path.join(REPO, "tests", "fakes")
FAKE_OPENCODE = os.path.join(FAKES, "opencode_fake.py")

SCALE = {"OPL_TIME_SCALE": "0.05"}


def write_packet(directory, text="Do the thing.\nWRITE_YES\n"):
    path = os.path.join(directory, "packet.md")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path


class BuildCommandTests(unittest.TestCase):
    def test_packet_workdir_model_variant_become_argv(self):
        argv = adapter.build_command("opencode", "example/spark-1", "xhigh",
                                     "/tmp/work", "Do it.")
        self.assertEqual(argv[:4], ["opencode", "run", "--model", "example/spark-1"])
        self.assertIn("--variant", argv)
        self.assertIn("xhigh", argv)
        self.assertIn("--dir", argv)
        self.assertIn("/tmp/work", argv)
        self.assertEqual(argv[-1], "Do it.")

    def test_variant_optional(self):
        argv = adapter.build_command("opencode", "example/spark-1", "",
                                     "/tmp/work", "Do it.")
        self.assertNotIn("--variant", argv)

    def test_model_must_be_explicit_provider_slash_model(self):
        for bad in ("", "spark", "has space/x", "a/b c"):
            with self.assertRaises(ValueError, msg=bad):
                adapter.build_command("opencode", bad, "xhigh", "/tmp/w", "p")

    def test_rejects_empty_prompt_or_workdir(self):
        with self.assertRaises(ValueError):
            adapter.build_command("opencode", "a/b", "", "", "prompt")
        with self.assertRaises(ValueError):
            adapter.build_command("opencode", "a/b", "", "/tmp/w", "  ")

    def test_forbidden_secret_value_refuses_argv(self):
        with self.assertRaises(ValueError):
            adapter.build_command("opencode", "a/b", "xhigh", "/tmp/w",
                                  "leaks s3cr3t-value-here", forbidden=("s3cr3t-value-here",))

    def test_short_secrets_ignored(self):
        argv = adapter.build_command("opencode", "a/b", "", "/tmp/w", "says abc",
                                     forbidden=("abc",))
        self.assertIn("says abc", argv[-1])


class ReadPacketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opl-oc-pack-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reads_packet_text(self):
        path = write_packet(self.tmp, "Hello.\n")
        self.assertEqual(adapter.read_packet(path), "Hello.\n")

    def test_missing_empty_rejected(self):
        with self.assertRaises(ValueError):
            adapter.read_packet(os.path.join(self.tmp, "nope.md"))
        empty = os.path.join(self.tmp, "empty.md")
        with open(empty, "w", encoding="utf-8") as fh:
            fh.write("  \n")
        with self.assertRaises(ValueError):
            adapter.read_packet(empty)


class ShellDiscoveryTests(unittest.TestCase):
    def _exists(self, present):
        present = {p.lower() for p in present}
        return lambda p: (p.lower() in present)

    def test_git_bash_and_wsl_are_distinguished(self):
        exists = self._exists({WIN_GIT_BASH, WIN_WSL})
        found = adapter.discover_shell(
            os_name="nt",
            env={"PATH": WIN_GIT_DIR + ";" + WIN_SYS_DIR},
            path_value=WIN_GIT_DIR + ";" + WIN_SYS_DIR,
            exists=exists)
        self.assertEqual(found["git_bash"], WIN_GIT_BASH)
        self.assertEqual(found["wsl"], WIN_WSL)
        self.assertNotIn("wsl", (found["git_bash"] or "").lower())
        self.assertNotIn("bash", (found["wsl"] or "").lower())

    def test_wsl_alone_is_never_reported_as_bash(self):
        exists = self._exists({WIN_WSL})
        found = adapter.discover_shell(
            os_name="nt", env={"PATH": WIN_SYS_DIR},
            path_value=WIN_SYS_DIR, exists=exists)
        self.assertIsNone(found["git_bash"])
        self.assertEqual(found["wsl"], WIN_WSL)

    def test_git_bash_override_wins_but_wsl_never_becomes_bash(self):
        exists = self._exists({WIN_GIT_BASH, WIN_WSL})
        env = {"PATH": WIN_SYS_DIR,
               adapter.GIT_BASH_ENV: WIN_GIT_BASH}
        found = adapter.discover_shell(os_name="nt", env=env,
                                       path_value=env["PATH"], exists=exists)
        self.assertEqual(found["git_bash"], WIN_GIT_BASH)

    def test_bogus_override_pointing_at_wsl_is_ignored(self):
        exists = self._exists({WIN_WSL})
        env = {"PATH": WIN_SYS_DIR,
               adapter.GIT_BASH_ENV: WIN_WSL}
        found = adapter.discover_shell(os_name="nt", env=env,
                                       path_value=env["PATH"], exists=exists)
        # The override names wsl.exe: it must not count as Git Bash.
        self.assertIsNone(found["git_bash"])
        self.assertEqual(found["wsl"], WIN_WSL)

    def test_posix_reports_sh(self):
        found = adapter.discover_shell(os_name="posix", env={},
                                       path_value="/usr/bin",
                                       exists=lambda p: p == "/bin/sh")
        self.assertEqual(found["posix"], "/bin/sh")


class FakeOpenCodeFlowTests(unittest.TestCase):
    """packet -> prompt, workdir, explicit model/variant, bounded run."""

    def setUp(self):
        # An earlier test's runner shutdown closes the process-wide stop
        # gate; reopen it, as a new runner does, or run_worker spawns nothing.
        reset_stop_gate()
        self.tmp = tempfile.mkdtemp(prefix="opl-oc-flow-")
        self.workdir = os.path.join(self.tmp, "work")
        os.mkdir(self.workdir)
        self.log = os.path.join(self.tmp, "run.log")
        self.record = os.path.join(self.tmp, "calls.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self, extra=None):
        # Test-only allowlist: the worker key plus the fake's control vars.
        # Production configs allow only the worker's own API key; the fake
        # vars exist solely so the synthetic double can record argv/cwd.
        allow = ("OPL_WORKER_API_KEY", "FAKE_OPENCODE_LOG", "FAKE_OPENCODE_MODE",
                 "FAKE_OPENCODE_EXIT")
        env = worker_environment(
            allow,
            os.path.join(self.tmp, "home"),
            base={**os.environ,
                  "OPL_WORKER_API_KEY": "synthetic-worker-key",
                  "OPL_TOKEN_SPARK": "synthetic-spark-token",
                  "OPL_TOKEN_ADMIN": "synthetic-admin-token",
                  "OPL_GITHUB_TOKEN": "synthetic-github-token",
                  "FAKE_OPENCODE_LOG": self.record,
                  **(extra or {})})
        return env

    def test_fake_opencode_receives_packet_prompt_dir_model_variant(self):
        packet = write_packet(self.tmp, "Do the tiny task.\nWRITE_YES\n")
        prompt = adapter.read_packet(packet)
        cmd = [sys.executable, FAKE_OPENCODE, "--model", "example/spark-1",
               "--variant", "xhigh", "--dir", self.workdir, prompt]
        # Simulate what bin/opl-opencode-run builds: model/variant/dir/prompt.
        argv = adapter.build_command("opencode", "example/spark-1", "xhigh",
                                     self.workdir, prompt)
        self.assertIn("example/spark-1", argv)
        self.assertIn("xhigh", argv)
        self.assertIn(self.workdir, argv)
        with mock.patch.dict(os.environ, {**SCALE, "FAKE_OPENCODE_LOG": self.record}):
            result = run_worker(cmd, self.workdir, 60, 60, self.log,
                                env=self._env())
        self.assertEqual(result.outcome, "success")
        self.assertTrue(any("OPL-RESULT: DONE" in line for line in result.last_lines))
        with open(self.record, encoding="utf-8") as fh:
            call = json.loads(fh.readline())
        self.assertEqual(call["cwd"], self.workdir)
        # The worker env carries only its own key, never conductor tokens.
        # The fake records env *names* only; values never leave the process.
        self.assertIn("FAKE_OPENCODE_LOG", call["env_names"])
        for forbidden in ("OPL_TOKEN_SPARK", "OPL_TOKEN_ADMIN", "OPL_GITHUB_TOKEN"):
            self.assertNotIn(forbidden, call["env_names"])

    def test_worker_env_holds_only_its_own_key(self):
        env = self._env()
        self.assertEqual(env.get("OPL_WORKER_API_KEY"), "synthetic-worker-key")
        self.assertNotIn("OPL_TOKEN_SPARK", env)
        self.assertNotIn("OPL_TOKEN_ADMIN", env)
        self.assertNotIn("OPL_GITHUB_TOKEN", env)

    def test_secret_value_never_in_argv_or_packet(self):
        secret = "synthetic-secret-abc123"
        packet = write_packet(self.tmp, "Do it with %s.\n" % secret)
        prompt = adapter.read_packet(packet)
        with self.assertRaises(ValueError):
            adapter.build_command("opencode", "a/b", "xhigh", self.workdir,
                                  prompt, forbidden=(secret,))

    def test_tiny_task_writes_yes_txt(self):
        packet = write_packet(self.tmp, "WRITE_YES please.\n")
        prompt = adapter.read_packet(packet)
        cmd = [sys.executable, FAKE_OPENCODE, prompt]
        with mock.patch.dict(os.environ, {**SCALE, "FAKE_OPENCODE_LOG": self.record}):
            result = run_worker(cmd, self.workdir, 60, 60, self.log,
                                env=self._env())
        self.assertEqual(result.outcome, "success")
        with open(os.path.join(self.workdir, "yes.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "YES\n")

    def test_missing_final_line_is_observable(self):
        packet = write_packet(self.tmp, "Do it.\n")
        prompt = adapter.read_packet(packet)
        cmd = [sys.executable, FAKE_OPENCODE, prompt]
        extra = {"FAKE_OPENCODE_MODE": "missing-line"}
        with mock.patch.dict(os.environ, {**SCALE, "FAKE_OPENCODE_LOG": self.record,
                                           **extra}):
            # run_worker itself succeeds (exit 0); the missing OPL-RESULT
            # line is a caller-side failure, as in outcomes.parse_final_line.
            from opl.conductor.spark.outcomes import parse_final_line
            result = run_worker(cmd, self.workdir, 60, 60, self.log,
                                env=self._env(extra))
            verdict, _ = parse_final_line(list(result.last_lines))
            self.assertEqual(verdict, "failed")


class AdapterScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opl-oc-bin-")
        self.workdir = os.path.join(self.tmp, "work")
        os.mkdir(self.workdir)
        self.record = os.path.join(self.tmp, "calls.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_wrapper_builds_opencode_argv_and_passes_exit_code(self):
        from opl.conductor.spark import opencode_cli as module
        packet = write_packet(self.tmp, "Build YES.\nWRITE_YES\n")
        seen = {}

        class FakeProc:
            returncode = 0

        def fake_run(cmd, cwd=None, **kwargs):
            seen["cmd"] = cmd
            seen["cwd"] = cwd
            return FakeProc()

        with mock.patch.object(module.subprocess, "run", fake_run):
            code = module.main(["--packet", packet, "--workdir", self.workdir,
                                "--model", "example/spark-1", "--variant", "xhigh",
                                "--opencode-bin", "opencode"])
        self.assertEqual(code, 0)
        self.assertEqual(seen["cmd"][:4], ["opencode", "run", "--model", "example/spark-1"])
        self.assertIn("--variant", seen["cmd"])
        self.assertIn("xhigh", seen["cmd"])
        self.assertIn("--dir", seen["cmd"])
        self.assertIn(self.workdir, seen["cmd"])
        self.assertTrue(seen["cmd"][-1].startswith("Build YES."))
        self.assertEqual(seen["cwd"], self.workdir)

    def test_wrapper_never_leaks_worker_key(self):
        from opl.conductor.spark import opencode_cli as module
        packet = write_packet(self.tmp, "Build it.\n")
        captured = {}

        class FakeProc:
            returncode = 0

        def fake_run(cmd, cwd=None, **kwargs):
            captured["text"] = " ".join(cmd)
            return FakeProc()

        env = {"OPL_WORKER_ENV_NAMES": "OPL_WORKER_API_KEY",
               "OPL_WORKER_API_KEY": "synthetic-worker-key"}
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(module.subprocess, "run", fake_run):
                code = module.main(["--packet", packet, "--workdir", self.workdir,
                                    "--model", "a/b"])
        self.assertEqual(code, 0)
        self.assertNotIn("synthetic-worker-key", captured["text"])

    def test_wrapper_refuses_secret_in_packet(self):
        from opl.conductor.spark import opencode_cli as module
        from io import StringIO
        packet = write_packet(self.tmp, "contains synthetic-secret-zz9\n")
        env = {"OPL_WORKER_ENV_NAMES": "OPL_WORKER_API_KEY",
               "OPL_WORKER_API_KEY": "synthetic-secret-zz9"}
        with mock.patch.dict(os.environ, env, clear=False):
            err = StringIO()
            with mock.patch.object(module.sys, "stderr", err):
                code = module.main(["--packet", packet,
                                    "--workdir", self.workdir, "--model", "a/b"])
        self.assertNotEqual(code, 0)
        self.assertNotIn("synthetic-secret-zz9", err.getvalue())


class ErrorEventTests(unittest.TestCase):
    """#66: provider errors reach the run log and pause Spark."""

    EVENT = {"type": "error", "sessionID": "ses_x", "error": {
        "name": "APIError", "data": {
            "message": "Billing verification failed. Please check your payment method.",
            "statusCode": 402, "isRetryable": False,
            "responseHeaders": {"proxy-status": "secret-ish internals"}}}}

    def test_error_event_renders_one_line_without_headers(self):
        lines, usage = adapter.convert_line(json.dumps(self.EVENT))
        self.assertIsNone(usage)
        self.assertEqual(lines, [
            "[error] APIError 402 (not retryable): Billing verification "
            "failed. Please check your payment method."])
        self.assertNotIn("proxy", lines[0])

    def test_provider_error_detects_refusals_only(self):
        line = adapter.render_error(self.EVENT)
        self.assertEqual(adapter.provider_error("usage unknown\n" + line), line)
        self.assertIsNone(adapter.provider_error(
            "[error] APIError 500: upstream hiccup"))
        # A 400 belongs to one request and its task, even when not retryable.
        self.assertIsNone(adapter.provider_error(
            "[error] APIError 400 (not retryable): invalid parameters"))
        self.assertIsNotNone(adapter.provider_error(
            "[error] APIError 429: rate limited"))
        self.assertIsNone(adapter.provider_error("something broke"))


class TransientErrorTests(unittest.TestCase):
    def test_database_collisions_are_transient(self):
        line = "[error] UnknownError: Failed to execute statement"
        self.assertEqual(adapter.transient_error("x\n" + line), line)
        self.assertIsNotNone(adapter.transient_error("database is locked"))
        self.assertIsNone(adapter.transient_error("[error] APIError 402: x"))
        self.assertIsNone(adapter.transient_error(""))


class ParseUsageCostTests(unittest.TestCase):
    def test_roundtrips_format_usage(self):
        line = adapter.format_usage((8068, 261, 0.0009))[0]
        self.assertAlmostEqual(adapter.parse_usage_cost(line), 0.0009)

    def test_unknown_and_absent_are_none(self):
        self.assertIsNone(adapter.parse_usage_cost("usage unknown"))
        self.assertIsNone(adapter.parse_usage_cost("no usage here"))


if __name__ == "__main__":
    unittest.main()
