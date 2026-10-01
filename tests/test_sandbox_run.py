#!/usr/bin/env python3
"""Tests for the sandbox adapter (issue #24, T5.1 S5).

Synthetic only: a fake launcher implements the clone/run/export modes over
a local bare repo, and a fake sudo (in-process here, plus the pass-through
branch of tests/fakebin/sudo) drops its options and runs the command. No
Docker, network, OpenProject, GitHub, real sudo, or model calls.
Standard library only.
"""

import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from opl.conductor.spark import sandbox_run
from tests.gitfixture import point_origin
from tests.test_static import sh_path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_LAUNCHER = os.path.join(REPO, "tests", "fakes", "sandbox_launcher_fake.py")
FAKE_SUDO = os.path.join(REPO, "tests", "fakebin", "sudo")
BASH = shutil.which("bash")

KEY = "synthetic-worker-key-abc123"
SLUG = "example-owner/demo-public"


def git(*args, cwd=None, env=None):
    merged = dict(os.environ)
    merged["GIT_TERMINAL_PROMPT"] = "0"
    merged["GCM_INTERACTIVE"] = "never"
    if env:
        merged.update(env)
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, timeout=60, env=merged)
    if proc.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args),
                                                  proc.stderr.strip()[:200]))
    return proc.stdout.strip()


class SandboxFixture(unittest.TestCase):
    """Local bare repo + workdir clone with a GitHub origin (offline)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opl-sandbox-")
        self.bare = os.path.join(self.tmp, "upstream.git")
        git("init", "-q", "--bare", "-b", "main", self.bare)
        seed = os.path.join(self.tmp, "seed")
        git("init", "-q", "-b", "main", seed)
        git("-C", seed, "config", "user.email", "t@t")
        git("-C", seed, "config", "user.name", "t")
        with open(os.path.join(seed, "base.txt"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write("base\n")
        git("-C", seed, "add", "-A")
        git("-C", seed, "commit", "-qm", "base")
        self.base = git("-C", seed, "rev-parse", "HEAD")
        git("-C", seed, "push", "-q", self.bare, "main:main")
        shutil.rmtree(seed, ignore_errors=True)
        self.workdir = os.path.join(self.tmp, "work")
        git("clone", "-q", self.bare, self.workdir)
        git("-C", self.workdir, "config", "user.email", "t@t")
        git("-C", self.workdir, "config", "user.name", "t")
        point_origin(self.workdir, self.bare, SLUG)
        self.packet = os.path.join(self.tmp, "packet.md")
        with open(self.packet, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("Do the tiny task.\nWRITE_YES\n")
        self.roots = os.path.join(self.tmp, "worktrees")
        os.mkdir(self.roots)
        self.calls = []
        self.key_env = {"OPL_WORKER_API_KEY": KEY}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- the fake sudo: drops its options and runs the command ------------

    def fake_spawn(self, argv, **kwargs):
        """Emulate `sudo -n -u <worker> [--preserve-env=N] -- <cmd...>`.

        Records the full sudo argv (for the preserve-env assertions),
        then runs the launcher command for real.
        """
        self.calls.append(list(argv))
        self.assertEqual(argv[0], "sudo")
        self.assertEqual(argv[1:3], ["-n", "-u"])
        rest = argv[3:]
        self.assertTrue(rest, "sudo argv has no worker")
        worker = rest[0]
        self.assertEqual(worker, "opl-worker")
        rest = rest[1:]
        while rest and rest[0].startswith("--preserve-env="):
            rest = rest[1:]
        self.assertTrue(rest and rest[0] == "--",
                        "sudo argv missing --: %r" % (argv,))
        cmd = list(rest[1:])
        self.assertTrue(cmd, "sudo argv has no command")
        if cmd[0].endswith(".py"):
            cmd = [sys.executable] + cmd
        env = dict(kwargs.pop("env", None) or os.environ)
        return subprocess.Popen(cmd, env=env, **kwargs)

    def launcher_env(self, extra=None):
        env = dict(os.environ)
        env.update(self.key_env)
        env["FAKE_WORKTREES_ROOT"] = self.roots
        env["FAKE_UPSTREAM_BARE"] = self.bare
        env["FAKE_LAUNCHER_LOG"] = os.path.join(self.tmp, "launcher.jsonl")
        env["OPL_SANDBOX_STATE_DIR"] = os.path.join(self.tmp, "state")
        if extra:
            env.update(extra)
        return env

    def run_adapter(self, extra_env=None, **over):
        params = dict(packet_path=self.packet, workdir=self.workdir,
                      model="example/spark-1", variant="xhigh",
                      launcher=FAKE_LAUNCHER, worker="opl-worker",
                      worktrees_root=self.roots, sudo_bin="sudo",
                      spawn=self.fake_spawn)
        params.update(over)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.launcher_env(extra_env),
                             clear=False):
            with redirect_stdout(out), redirect_stderr(err):
                code = sandbox_run.run_sandbox(**params)
        return code, out.getvalue(), err.getvalue()


class ParseTests(unittest.TestCase):
    def test_github_forms_accepted(self):
        for url in ("https://github.com/Owner/Name.git",
                    "https://github.com/owner/name",
                    "git@github.com:owner/name.git",
                    "ssh://git@github.com/owner/name.git"):
            self.assertEqual(sandbox_run.parse_origin_slug(url),
                             "owner/name", url)

    def test_local_and_foreign_origins_refused(self):
        for url in ("/srv/git/demo.git", "file:///srv/git/d.git",
                    "https://gitlab.com/owner/name.git",
                    "https://github.com.evil.com/owner/name.git",
                    "https://github.com/just-owner",
                    "", "not-a-url"):
            with self.assertRaises(ValueError, msg=url):
                sandbox_run.parse_origin_slug(url)

    def test_run_id_shape(self):
        stamp = sandbox_run.make_run_id("my worktree!")
        self.assertRegex(stamp, sandbox_run.RUN_ID_RE)
        self.assertIn("my-worktree", stamp)
        other = sandbox_run.make_run_id("my worktree!")
        self.assertNotEqual(stamp, other)
        long_id = sandbox_run.make_run_id("x" * 200)
        self.assertTrue(sandbox_run.RUN_ID_RE.match(long_id))
        self.assertLessEqual(len(long_id), 100)

    def test_sudo_argv_shapes(self):
        clone = sandbox_run.sudo_argv("sudo", "opl-worker", "/L",
                                      ["clone", "--run", "R"])
        self.assertEqual(clone[:5], ["sudo", "-n", "-u", "opl-worker", "--"])
        self.assertNotIn("--preserve-env=OPL_WORKER_API_KEY", clone)
        run = sandbox_run.sudo_argv("sudo", "opl-worker", "/L", ["run"],
                                    preserve_key=True)
        self.assertIn("--preserve-env=OPL_WORKER_API_KEY", run)
        self.assertNotIn(KEY, " ".join(run))

    def test_convert_line(self):
        lines, usage = sandbox_run.convert_line(
            '{"type": "text", "text": "hi\\nOPL-RESULT: DONE x"}')
        self.assertEqual(lines, ["hi", "OPL-RESULT: DONE x"])
        self.assertIsNone(usage)
        lines, _ = sandbox_run.convert_line(
            '{"type": "tool_use", "title": "edit a.txt"}')
        self.assertEqual(lines, ["[tool] edit a.txt"])
        lines, _ = sandbox_run.convert_line(
            '{"type": "tool", "name": "bash"}')
        self.assertEqual(lines, ["[tool] bash"])
        # Unknown shapes are silent; raw lines survive verbatim.
        self.assertEqual(sandbox_run.convert_line('{"type": "ping"}'),
                         (None, None))
        lines, _ = sandbox_run.convert_line("OPL-RESULT: DONE raw-ok")
        self.assertEqual(lines, ["OPL-RESULT: DONE raw-ok"])
        self.assertEqual(sandbox_run.format_usage(None), ["usage unknown"])
        self.assertEqual(len(sandbox_run.format_usage((1, 2, 0.5))), 1)

    def test_shared_parser_with_opencode(self):
        # Issue #27: one shared parser, not two.
        from opl.conductor.spark import opencode as adapter
        for name in ("convert_line", "render_event", "extract_usage",
                     "format_usage", "TOOL_TYPES"):
            self.assertIs(getattr(sandbox_run, name), getattr(adapter, name),
                          name)

    def test_real_opencode_sample_lines(self):
        # Issue #27: the trimmed synthetic run from the report, fed
        # exactly as the launcher emits it (nested under "part").
        sample = [
            '{"type": "step_start", "timestamp": 1790544732582, '
            '"sessionID": "ses_f1b37b5bfffeZoiqUrC4oMVYDw", '
            '"part": {"id": "prt_0e4c86156001dZtWkRkVV0fku0", '
            '"messageID": "msg_0e4c850a4001QOafEd1PxAaSLR", '
            '"sessionID": "ses_f1b37b5bfffeZoiqUrC4oMVYDw", '
            '"snapshot": "b1f0299949f6ff16456fe8b521772e0031920610", '
            '"type": "step-start"}}',
            '{"type": "tool_use", "timestamp": 1790544734701, '
            '"sessionID": "ses_f1b37b5bfffeZoiqUrC4oMVYDw", '
            '"part": {"type": "tool", "tool": "read", '
            '"callID": "call_01a0e4c862d375ef97d2c27e8ef380cf", '
            '"state": {"status": "completed", '
            '"input": {"filePath": "/srv/opl-worktrees/preview-yes-1/repo"}, '
            '"output": "listing...", '
            '"metadata": {"preview": ".git/", "truncated": false}, '
            '"title": "", '
            '"time": {"start": 1790544734656, "end": 1790544734693}}, '
            '"id": "prt_0e4c86929001PUB95BuoHCVYUB", '
            '"sessionID": "ses_f1b37b5bfffeZoiqUrC4oMVYDw", '
            '"messageID": "msg_0e4c850a4001QOafEd1PxAaSLR"}}',
            '{"type": "step_finish", "timestamp": 1790544734886, '
            '"sessionID": "ses_f1b37b5bfffeZoiqUrC4oMVYDw", '
            '"part": {"id": "prt_0e4c86a9a001boJ5yZFYrQ9TZk", '
            '"reason": "tool-calls", '
            '"snapshot": "b1f0299949f6ff16456fe8b521772e0031920610", '
            '"messageID": "msg_0e4c850a4001QOafEd1PxAaSLR", '
            '"sessionID": "ses_f1b37b5bfffeZoiqUrC4oMVYDw", '
            '"type": "step-finish", '
            '"tokens": {"total": 7607, "input": 7374, "output": 77, '
            '"reasoning": 156, "cache": {"write": 0, "read": 0}}, '
            '"cost": 0.000784}}',
            '{"type": "step_start", "timestamp": 1790544736000, '
            '"sessionID": "ses_f1b37b5bfffeZoiqUrC4oMVYDw", '
            '"part": {"id": "prt_0e4c86ef7001JGK9D6sndE6sNN", '
            '"messageID": "msg_0e4c86b17001cbHb4Ln4EGRmk6", '
            '"sessionID": "ses_f1b37b5bfffeZoiqUrC4oMVYDw", '
            '"snapshot": "b1f0299949f6ff16456fe8b521772e0031920610", '
            '"type": "step-start"}}',
            '{"type": "tool_use", "timestamp": 1790544736387, '
            '"sessionID": "ses_f1b37b5bfffeZoiqUrC4oMVYDw", '
            '"part": {"type": "tool", "tool": "write", '
            '"callID": "call_01a0e4c8685170a18be84f1a86c98677", '
            '"state": {"status": "completed", '
            '"input": {"content": "YES\\n", '
            '"filePath": "/srv/opl-worktrees/preview-yes-1/repo/yes.txt"}, '
            '"output": "Wrote file successfully.", '
            '"metadata": {"filepath": '
            '"/srv/opl-worktrees/preview-yes-1/repo/yes.txt"}, '
            '"title": "yes.txt", '
            '"time": {"start": 1790544736352, "end": 1790544736375}}, '
            '"id": "prt_0e4c87028001Om20iF5eQ2GkNg", '
            '"sessionID": "ses_f1b37b5bfffeZoiqUrC4oMVYDw", '
            '"messageID": "msg_0e4c86b17001cbHb4Ln4EGRmk6"}}',
            '{"type": "step_finish", "timestamp": 1790544736934, '
            '"sessionID": "ses_f1b37b5bfffeZoiqUrC4oMVYDw", '
            '"part": {"id": "prt_0e4c87271001N2a3j0ow11XgLq", '
            '"reason": "tool-calls", '
            '"snapshot": "b3f75bf5393bfa9bd6446d03ceb0df4bf3c602ee", '
            '"messageID": "msg_0e4c86b17001cbHb4Ln4EGRmk6", '
            '"sessionID": "ses_f1b37b5bfffeZoiqUrC4oMVYDw", '
            '"type": "step-finish", '
            '"tokens": {"total": 7755, "input": 389, "output": 81, '
            '"reasoning": 4, "cache": {"write": 0, "read": 7281}}, '
            '"cost": 7.0462e-05}}',
            '{"type": "text", "timestamp": 1790544742982, '
            '"sessionID": "ses_f1b37b5bfffeZoiqUrC4oMVYDw", '
            '"part": {"id": "prt_0e4c88a29001pgIHnnk6d2uzua", '
            '"messageID": "msg_0e4c88474001R3DSb5wdPO0pjS", '
            '"sessionID": "ses_f1b37b5bfffeZoiqUrC4oMVYDw", '
            '"type": "text", "text": "OPL-RESULT: DONE", '
            '"time": {"start": 1790544742953, "end": 1790544742962}}}',
        ]
        # step_start lines are silent.
        self.assertEqual(sandbox_run.convert_line(sample[0]), (None, None))
        self.assertEqual(sandbox_run.convert_line(sample[3]), (None, None))
        # Empty state.title falls back to the tool name; set titles show.
        lines, _ = sandbox_run.convert_line(sample[1])
        self.assertEqual(lines, ["[tool] read"])
        lines, _ = sandbox_run.convert_line(sample[4])
        self.assertEqual(lines, ["[tool] yes.txt"])
        # Usage sums across both step_finish events (not "unknown").
        totals = [0, 0, 0.0]
        shown = []
        for line in sample:
            rendered, usage = sandbox_run.convert_line(line)
            if usage is not None:
                totals[0] += usage[0]
                totals[1] += usage[1]
                totals[2] += usage[2]
            if rendered:
                shown.extend(rendered)
        self.assertIn("OPL-RESULT: DONE", shown)
        self.assertEqual(totals[0], 7374 + 389)
        self.assertEqual(totals[1], 77 + 81)
        self.assertAlmostEqual(totals[2], 0.000784 + 7.0462e-05)
        summary = sandbox_run.format_usage(tuple(totals))
        self.assertEqual(len(summary), 1)
        self.assertRegex(summary[0], r"^usage: input=7763 output=158 cost=")
        self.assertNotEqual(summary, ["usage unknown"])


class CloneRunTests(SandboxFixture):
    def test_clone_run_converts_json_and_keeps_final_line(self):
        code, out, _err = self.run_adapter()
        self.assertEqual(code, 0, out)
        self.assertIn("fake worker did the task", out)
        self.assertIn("[tool] edit demo.txt", out)
        self.assertIn("OPL-RESULT: DONE fake-ok", out)
        # Usage summary is one trailing line, inside the last-20 window.
        self.assertRegex(out, r"(?m)^usage: input=10 output=5 cost=")
        self.assertEqual(len(out.strip().splitlines()), 4)
        # The fake really cloned through the launcher (no env preserved on
        # clone or export) and ran with the key name preserved.
        kinds = [c for c in self.calls]
        self.assertEqual(len(kinds), 3)
        self.assertIn("clone", kinds[0])
        self.assertNotIn("--preserve-env=OPL_WORKER_API_KEY", kinds[0])
        self.assertIn("run", kinds[1])
        self.assertIn("--preserve-env=OPL_WORKER_API_KEY", kinds[1])
        self.assertNotIn(KEY, " ".join(kinds[1]))
        self.assertIn("export", kinds[2])
        self.assertNotIn("--preserve-env=OPL_WORKER_API_KEY", kinds[2])
        # Model/variant reach the launcher; packet went on stdin (the fake
        # wrote yes.txt into the worker clone, not the owner's workdir).
        self.assertIn("example/spark-1", kinds[1])
        self.assertIn("xhigh", kinds[1])
        self.assertFalse(os.path.exists(
            os.path.join(self.workdir, "yes.txt")))
        run_ids = [c[c.index("--run") + 1] for c in kinds]
        worker_repo = os.path.join(self.roots, run_ids[0], "repo")
        self.assertTrue(os.path.isfile(os.path.join(worker_repo, "yes.txt")))

    def test_exit_code_is_the_runs(self):
        code, _out, _err = self.run_adapter(
            extra_env={"FAKE_LAUNCHER_EXIT": "5"})
        self.assertEqual(code, 5)

    def test_usage_unknown_without_usage_events(self):
        code, out, _err = self.run_adapter(
            extra_env={"FAKE_LAUNCHER_MODE": "no-usage"})
        self.assertEqual(code, 0)
        self.assertIn("usage unknown", out.strip().splitlines()[-1])

    def test_missing_key_fails_closed_and_unlogged(self):
        out, err = io.StringIO(), io.StringIO()
        env = self.launcher_env()
        env.pop("OPL_WORKER_API_KEY", None)
        with mock.patch.dict(os.environ, env, clear=True):
            with redirect_stdout(out), redirect_stderr(err):
                code = sandbox_run.main(
                    ["--packet", self.packet, "--workdir", self.workdir,
                     "--model", "example/spark-1",
                     "--launcher", FAKE_LAUNCHER,
                     "--worktrees-root", self.roots],
                    spawn=self.fake_spawn)
        self.assertEqual(code, 2)
        self.assertEqual(self.calls, [])
        self.assertNotIn(KEY, out.getvalue() + err.getvalue())

    def test_packet_holding_key_value_is_refused(self):
        with open(self.packet, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("Use %s please.\n" % KEY)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.launcher_env(), clear=False):
            with redirect_stdout(out), redirect_stderr(err):
                code = sandbox_run.main(
                    ["--packet", self.packet, "--workdir", self.workdir,
                     "--model", "example/spark-1",
                     "--launcher", FAKE_LAUNCHER,
                     "--worktrees-root", self.roots],
                    spawn=self.fake_spawn)
        self.assertEqual(code, 2)
        self.assertEqual(self.calls, [])
        self.assertNotIn(KEY, out.getvalue() + err.getvalue())

    def test_bad_model_and_variant_refused(self):
        for model in ("spark", "has space/x"):
            with self.assertRaises(ValueError):
                sandbox_run.run_sandbox(
                    packet_path=self.packet, workdir=self.workdir,
                    model=model, variant="", launcher=FAKE_LAUNCHER,
                    worker="opl-worker", worktrees_root=self.roots,
                    spawn=self.fake_spawn)

    def _run_detached(self, mode=None):
        # Review, test and (since #32) fix worktrees are detached at the PR
        # head on purpose. The worker gets a throwaway branch in its clone.
        git("-C", self.workdir, "checkout", "-q", "--detach", "HEAD")
        head = git("-C", self.workdir, "rev-parse", "HEAD")
        env = dict(self.launcher_env())
        if mode:
            env["FAKE_LAUNCHER_MODE"] = mode
        with mock.patch.dict(os.environ, env, clear=False):
            sandbox_run.run_sandbox(
                packet_path=self.packet, workdir=self.workdir,
                model="example/spark-1", variant="",
                launcher=FAKE_LAUNCHER, worker="opl-worker",
                worktrees_root=self.roots, spawn=self.fake_spawn)
        clone = [c for c in self.calls if "clone" in c][0]
        self.assertTrue(clone[clone.index("--branch") + 1].startswith("opl/detached-"))
        return head

    def test_detached_head_without_commits_stays_put(self):
        head = self._run_detached()
        self.assertEqual(git("-C", self.workdir, "rev-parse", "HEAD"), head)

    def test_detached_head_imports_worker_commit(self):
        # Found live (T5.1 R2): fix runs are detached at the PR head; the
        # worker's fix commit must land on that detached HEAD for the push.
        head = self._run_detached(mode="commit")
        new = git("-C", self.workdir, "rev-parse", "HEAD")
        self.assertNotEqual(new, head)
        self.assertEqual(git("-C", self.workdir, "rev-parse", "HEAD~1"), head)

    def test_non_github_origin_refused(self):
        git("-C", self.workdir, "remote", "set-url", "origin",
            "https://gitlab.com/example-owner/demo-public.git")
        git("-C", self.workdir, "config", "--unset-all",
            "url.%s.insteadOf" % self.bare.replace("\\", "/"))
        with mock.patch.dict(os.environ, self.launcher_env(), clear=False):
            with self.assertRaises(ValueError):
                sandbox_run.run_sandbox(
                    packet_path=self.packet, workdir=self.workdir,
                    model="example/spark-1", variant="",
                    launcher=FAKE_LAUNCHER, worker="opl-worker",
                    worktrees_root=self.roots, spawn=self.fake_spawn)
        self.assertEqual(self.calls, [])


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opl-hb-")
        self.repo = os.path.join(self.tmp, "repo")
        os.mkdir(self.repo)
        self.probe = os.path.join(self.repo, "file.txt")
        with open(self.probe, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("old\n")
        # Old baseline: any real write advances past it for sure.
        os.utime(self.probe, (1000000000, 1000000000))
        self.beat = os.path.join(self.tmp, "work", "beat")
        os.mkdir(os.path.join(self.tmp, "work"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_newest_mtime_ignores_dot_git(self):
        gitdir = os.path.join(self.repo, ".git", "objects")
        os.makedirs(gitdir)
        fresh = os.path.join(gitdir, "fresh")
        with open(fresh, "w") as fh:
            fh.write("x")
        future = 2000000000
        os.utime(fresh, (future, future))
        self.assertEqual(sandbox_run.newest_mtime(self.repo), 1000000000)

    def test_touch_only_on_real_progress(self):
        monitor = sandbox_run.Heartbeat(self.repo, self.beat, 0.05)
        monitor.start()
        try:
            import time
            time.sleep(0.25)
            # Idle repo: no heartbeat, so the runner still stalls.
            self.assertFalse(os.path.exists(self.beat))
            with open(self.probe, "a", encoding="utf-8", newline="\n") as fh:
                fh.write("new\n")
            deadline = time.monotonic() + 5
            while not os.path.exists(self.beat) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(os.path.exists(self.beat))
        finally:
            monitor.stop()


class RuntimeArgsTests(SandboxFixture):
    """#45: provisioned runtimes and the setup reach the launcher's run mode."""

    def launcher_calls(self):
        with open(os.path.join(self.tmp, "launcher.jsonl"),
                  encoding="utf-8") as fh:
            return [json.loads(line)["argv"] for line in fh if line.strip()]

    def test_runtime_and_setup_are_passed_to_run_mode(self):
        code, _out, _err = self.run_adapter(
            runtimes=["node@22.17.0"], setup_b64="bnBtIGNp")
        self.assertEqual(code, 0)
        run_call = next(c for c in self.launcher_calls()
                        if c and c[0] == "run")
        self.assertIn("--runtime", run_call)
        self.assertEqual(run_call[run_call.index("--runtime") + 1],
                         "node@22.17.0")
        self.assertEqual(run_call[run_call.index("--setup-b64") + 1],
                         "bnBtIGNp")

    def test_no_declaration_means_no_extra_launcher_arguments(self):
        # An older launcher must keep working until a project opts in.
        self.run_adapter()
        run_call = next(c for c in self.launcher_calls()
                        if c and c[0] == "run")
        self.assertNotIn("--runtime", run_call)
        self.assertNotIn("--setup-b64", run_call)

    def test_bad_values_are_refused_before_any_launcher_call(self):
        for over in ({"runtimes": ["node@22;rm -rf"]},
                     {"runtimes": ["../node@22"]},
                     {"setup_b64": "not base64!"},
                     {"setup_b64": "A" * 5000}):
            with self.assertRaises(ValueError, msg=over):
                self.run_adapter(**over)
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, "launcher.jsonl")))

    def test_command_line_flags_are_accepted(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.launcher_env(), clear=False):
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = sandbox_run.main([
                    "--packet", self.packet, "--workdir", self.workdir,
                    "--model", "example/spark-1", "--launcher", FAKE_LAUNCHER,
                    "--worktrees-root", self.roots,
                    "--runtime", "node@22.17.0", "--runtime", "node@20.1.0",
                    "--setup-b64", "bnBtIGNp"], spawn=self.fake_spawn)
        self.assertEqual(code, 0)
        run_call = next(c for c in self.launcher_calls()
                        if c and c[0] == "run")
        self.assertEqual(run_call.count("--runtime"), 2)


class ExcludeImportTests(SandboxFixture):
    def exclude_lines(self):
        rel = git("-C", self.workdir, "rev-parse", "--git-path",
                  "info/exclude")
        path = rel if os.path.isabs(rel) else os.path.join(self.workdir, rel)
        with open(path, encoding="utf-8") as fh:
            return fh.read().splitlines()

    def launcher_run_ids(self):
        with open(os.path.join(self.tmp, "launcher.jsonl"),
                  encoding="utf-8") as fh:
            calls = [json.loads(line)["argv"] for line in fh if line.strip()]
        ids = []
        for argv in calls:
            if argv and argv[0] == "clone" and "--run" in argv:
                ids.append(argv[argv.index("--run") + 1])
        return ids

    def test_exclude_added_once_and_heartbeat_removed(self):
        for _ in range(2):
            code, _out, _err = self.run_adapter()
            self.assertEqual(code, 0)
            self.assertFalse(os.path.exists(
                os.path.join(self.workdir, sandbox_run.HEARTBEAT_NAME)))
        lines = self.exclude_lines()
        self.assertEqual(lines.count("/" + sandbox_run.HEARTBEAT_NAME), 1)

    def test_commit_mode_imports_and_fast_forwards(self):
        code, out, _err = self.run_adapter(
            extra_env={"FAKE_LAUNCHER_MODE": "commit"})
        self.assertEqual(code, 0, out)
        self.assertIn("OPL-RESULT: DONE fake-ok", out)
        self.assertTrue(os.path.isfile(
            os.path.join(self.workdir, "worker.txt")))
        log = git("-C", self.workdir, "log", "--oneline")
        self.assertIn("worker: do the tiny task", log)
        self.assertNotEqual(
            git("-C", self.workdir, "rev-parse", "HEAD"), self.base)
        # The bundle temp file is gone from the private state dir.
        leftovers = []
        for _dir, _subs, files in os.walk(os.path.join(self.tmp, "state")):
            leftovers.extend(files)
        self.assertEqual(leftovers, [])

    def test_no_new_commits_imports_nothing(self):
        code, _out, _err = self.run_adapter()
        self.assertEqual(code, 0)
        self.assertEqual(
            git("-C", self.workdir, "rev-parse", "HEAD"), self.base)
        self.assertEqual(
            git("-C", self.workdir, "log", "--oneline").strip().splitlines(),
            git("-C", self.workdir, "log", "--oneline").strip().splitlines()[:1])

    def test_owner_runs_no_git_inside_worker_repo(self):
        self.run_adapter(extra_env={"FAKE_LAUNCHER_MODE": "commit"})
        [run_id] = self.launcher_run_ids()
        worker_repo = os.path.join(self.roots, run_id, "repo")
        log = git("-C", worker_repo, "log", "--oneline")
        # Exactly base + the worker's own commit: nothing owner-side added.
        self.assertEqual(len(log.strip().splitlines()), 2)

    def _diverge_workdir(self):
        """Worker commit exported; the owner worktree then holds a different
        commit on the same base, like a PR whose head moved or was rebased."""
        self.run_adapter(extra_env={"FAKE_LAUNCHER_MODE": "commit"})
        [run_id] = self.launcher_run_ids()
        git("-C", self.workdir, "reset", "-q", "--hard", self.base)
        with open(os.path.join(self.workdir, "other.txt"), "w") as fh:
            fh.write("other\n")
        git("-C", self.workdir, "add", "-A")
        git("-C", self.workdir, "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-qm", "diverged")
        return run_id

    def _import(self, run_id, **extra):
        with mock.patch.dict(os.environ, self.launcher_env(), clear=False):
            return sandbox_run.export_and_import(
                sudo_bin="sudo", worker="opl-worker", launcher=FAKE_LAUNCHER,
                run_id=run_id, base=self.base, branch="main",
                workdir=self.workdir, spawn=self.fake_spawn, **extra)

    def test_diverged_bundle_is_refused_by_default(self):
        run_id = self._diverge_workdir()
        with self.assertRaises(ValueError) as ctx:
            self._import(run_id)
        self.assertIn("not a fast-forward", str(ctx.exception))

    def test_fix_runs_accept_a_rewritten_branch(self):
        # #74: a rebase is not a fast-forward of the old head; for fix runs
        # the worktree is reset to the worker's tip instead of refusing.
        run_id = self._diverge_workdir()
        self.assertEqual(self._import(run_id, allow_rewrite=True), "rewritten")
        self.assertTrue(os.path.isfile(
            os.path.join(self.workdir, "worker.txt")))
        self.assertFalse(os.path.exists(
            os.path.join(self.workdir, "other.txt")))

    def test_terminated_signal_skips_the_import(self):
        self.run_adapter(extra_env={"FAKE_LAUNCHER_MODE": "commit"})
        [run_id] = self.launcher_run_ids()
        git("-C", self.workdir, "reset", "-q", "--hard", self.base)
        sandbox_run._terminated = True
        try:
            with mock.patch.dict(os.environ, self.launcher_env(),
                                 clear=False):
                with self.assertRaises(SystemExit) as ctx:
                    sandbox_run.export_and_import(
                        sudo_bin="sudo", worker="opl-worker",
                        launcher=FAKE_LAUNCHER, run_id=run_id, base=self.base,
                        branch="main", workdir=self.workdir,
                        spawn=self.fake_spawn)
        finally:
            sandbox_run._terminated = False
        self.assertEqual(ctx.exception.code, 143)
        # Export ran (and found the commit), the import never did.
        self.assertTrue(any("export" in c for c in self.calls))
        self.assertEqual(
            git("-C", self.workdir, "rev-parse", "HEAD"), self.base)


class SignalTests(unittest.TestCase):
    def test_sigterm_forwards_and_exits_143(self):
        seen = {}

        class FakeChild:
            def terminate(self):
                seen["terminated"] = True

            def send_signal(self, signum):
                seen["signal"] = signum

            def wait(self, timeout=None):
                seen["waited"] = timeout
                return 0

        sandbox_run._child = FakeChild()
        try:
            with self.assertRaises(SystemExit) as ctx:
                sandbox_run._forward(signal.SIGTERM, None)
        finally:
            sandbox_run._child = None
        self.assertEqual(ctx.exception.code, 143)
        self.assertTrue(seen.get("terminated"))
        self.assertIn("waited", seen)

    def test_sigint_forwards_sigint(self):
        seen = {}

        class FakeChild:
            def terminate(self):
                seen["terminated"] = True

            def send_signal(self, signum):
                seen["signal"] = signum

            def wait(self, timeout=None):
                return 0

        sandbox_run._child = FakeChild()
        try:
            with self.assertRaises(SystemExit) as ctx:
                sandbox_run._forward(signal.SIGINT, None)
        finally:
            sandbox_run._child = None
        self.assertEqual(ctx.exception.code, 143)
        self.assertEqual(seen.get("signal"), signal.SIGINT)
        self.assertNotIn("terminated", seen)


@unittest.skipUnless(BASH, "bash needed for the fakebin sudo check")
class FakeSudoTests(unittest.TestCase):
    def run_fake(self, *args, env_extra=None):
        env = dict(os.environ)
        if env_extra:
            env.update(env_extra)
        return subprocess.run([BASH, sh_path(FAKE_SUDO), *args],
                              capture_output=True, text=True, timeout=30,
                              env=env)

    def test_sandbox_shape_drops_options_and_runs(self):
        proc = self.run_fake("-n", "-u", "opl-worker", "--",
                             "echo", "sandbox-hi")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("sandbox-hi", proc.stdout)

    def test_preserve_env_option_is_dropped(self):
        proc = self.run_fake("-n", "-u", "opl-worker",
                             "--preserve-env=OPL_WORKER_API_KEY", "--",
                             "echo", "kept-hi")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("kept-hi", proc.stdout)

    def test_missing_separator_is_rejected(self):
        proc = self.run_fake("-n", "-u", "opl-worker", "echo", "hi")
        self.assertEqual(proc.returncode, 97)

    def test_canary_shape_still_strict(self):
        proc = self.run_fake("-u", "root", "id")
        self.assertEqual(proc.returncode, 97)


if __name__ == "__main__":
    unittest.main()
