#!/usr/bin/env python3
"""Static checks of the reference launcher sandbox/opl-spark-launch
(issue #45). The functional test needs the worker account and is run by the
owner (docs/SANDBOX.md); these checks pin its security-relevant rules."""

import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "sandbox", "opl-spark-launch")


def text():
    with open(PATH, "rb") as fh:
        return fh.read().decode("utf-8")


class LauncherReferenceTests(unittest.TestCase):
    def test_lf_only_with_bash_shebang_and_strict_mode(self):
        body = text()
        self.assertNotIn("\r", body, "the launcher must stay LF-only")
        self.assertTrue(body.startswith("#!/bin/bash\n"))
        self.assertIn("set -euo pipefail", body)

    def test_gitattributes_keeps_the_directory_lf(self):
        with open(os.path.join(ROOT, ".gitattributes"), encoding="utf-8") as fh:
            self.assertIn("sandbox/* text eol=lf", fh.read())

    def test_runtime_arguments_are_validated_and_never_worker_writable(self):
        body = text()
        self.assertIn(r"^([a-z][a-z0-9]*)@([0-9]+(\.[0-9]+){0,2})$", body)
        self.assertIn('RUNTIMES=/opt/opl-runtimes', body)
        self.assertIn(".opl-installed", body)
        self.assertIn('!= "opl-worker"', body)          # not worker-owned
        self.assertIn("-perm /022", body)               # not group/world writable
        self.assertIn("[ ! -L", body)                   # no symlinked runtime

    def test_setup_is_bounded_validated_and_fails_the_run(self):
        body = text()
        self.assertIn("-le 4096", body)
        self.assertIn("^[A-Za-z0-9+/]+={0,2}$", body)
        self.assertIn('timeout "$SETUP_TIMEOUT"', body)
        self.assertIn("exit 4", body)
        # The setup runs before the model, never after it.
        self.assertLess(body.index('bash -c "$setup"'),
                        body.index('setsid "$OPENCODE"'))

    def test_environment_is_still_rebuilt_from_nothing(self):
        body = text()
        self.assertGreaterEqual(body.count('env -i "${clean_env[@]}"'), 4)
        self.assertNotIn("sudo ", body.replace("# ", "").split("set -euo")[1])

    def test_version_mode_advertises_the_feature(self):
        self.assertIn("opl-spark-launch 3 runtimes setup probe", text())

    def test_probe_mode_is_model_free_and_cleans_up(self):
        body = text()
        probe = body.split("  probe)")[1].split("  export)")[0]
        self.assertIn("probe-*", probe)
        self.assertIn("rm -rf", probe)
        self.assertIn("--depth 1", probe)
        self.assertNotIn("OPENCODE", probe)
        self.assertNotIn("OPL_WORKER_API_KEY", probe)
        self.assertIn("exit 4", probe)

    def test_bash_accepts_the_syntax(self):
        if os.name == "nt":
            self.skipTest("no reliable bash on Windows; the WSL run covers it")
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("no bash")
        proc = subprocess.run([bash, "-n", PATH], capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
