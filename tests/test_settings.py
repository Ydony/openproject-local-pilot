#!/usr/bin/env python3
"""Tests for opl.settings: external config, tokens, redaction, validation.

Run: python -m unittest discover -s tests   (needs Python 3.11+ for tomllib)
"""

import os
import tempfile
import unittest
from unittest import mock

from opl.settings import MissingSecret, SettingsError, load, redact

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(REPO, "config", "opl.example.toml")

BASE = """
[openproject]
url = "http://127.0.0.1:8080"
admin_token_env = "OPL_TOKEN_ADMIN"
owner_login = "admin"

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
live = false
interval_seconds = 60
state_dir = "~/.local/state/opl"

[runner]
command = ["example-worker", "--prompt-file", "{packet}"]
max_parallel = 2

[runner.limits_minutes]
S = 20
M = 45
L = 90
review = 15
test = 20
stall = 10

[[project]]
key = "demo-public"
name = "Demo public project"
repo = "example-owner/demo-public"
visibility = "Public"
has_test_env = true
test_url = "http://127.0.0.1:3001"
test_signal = { kind = "workflow", name = "deploy-test" }
prod_signal = { kind = "environment", name = "production" }
"""


def load_text(text):
    tmpd = tempfile.mkdtemp(prefix="opl-settings-")
    path = os.path.join(tmpd, "opl.toml")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    with mock.patch.dict(os.environ, {"OPL_CONFIG_DIR": tmpd}):
        return load()


class ExampleTests(unittest.TestCase):
    def test_example_loads(self):
        from opl.settings import load_file

        settings = load_file(EXAMPLE)
        self.assertEqual(settings.openproject.url, "http://localhost:8080")
        self.assertEqual(settings.openproject.owner_login, "admin")
        self.assertEqual(set(settings.tokens), {"claude", "codex", "spark", "conductor"})
        self.assertEqual(len(settings.projects), 2)
        self.assertEqual(settings.runner.limits_minutes["S"], 20)
        self.assertFalse(settings.conductor.live)


class TokenTests(unittest.TestCase):
    def test_token_returns_env_value(self):
        settings = load_text(BASE)
        with mock.patch.dict(os.environ, {"OPL_TOKEN_SPARK": "sentinel-spark"}):
            self.assertEqual(settings.token("spark"), "sentinel-spark")

    def test_token_admin_and_github(self):
        settings = load_text(BASE)
        env = {"OPL_TOKEN_ADMIN": "sentinel-admin", "OPL_GITHUB_TOKEN": "sentinel-gh"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(settings.token("admin"), "sentinel-admin")
            self.assertEqual(settings.token("github"), "sentinel-gh")

    def test_token_missing_names_var_without_value(self):
        settings = load_text(BASE)
        env = dict(os.environ)
        env.pop("OPL_TOKEN_CODEX", None)
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(MissingSecret) as ctx:
                settings.token("codex")
        message = str(ctx.exception)
        self.assertIn("OPL_TOKEN_CODEX", message)
        self.assertNotIn("s3cr3t", message)

    def test_redact_masks_known_values(self):
        settings = load_text(BASE)
        env = {"OPL_TOKEN_SPARK": "s3cr3t-spark", "OPL_TOKEN_ADMIN": "s3cr3t-admin"}
        with mock.patch.dict(os.environ, env):
            out = redact("spark uses s3cr3t-spark and admin s3cr3t-admin ok", settings)
        self.assertNotIn("s3cr3t-spark", out)
        self.assertNotIn("s3cr3t-admin", out)
        self.assertIn("***", out)
        self.assertIn("ok", out)

    def test_redact_ignores_missing_vars(self):
        settings = load_text(BASE)
        env = dict(os.environ)
        for var in ("OPL_TOKEN_SPARK", "OPL_TOKEN_ADMIN", "OPL_GITHUB_TOKEN",
                    "OPL_TOKEN_CLAUDE", "OPL_TOKEN_CODEX", "OPL_TOKEN_CONDUCTOR"):
            env.pop(var, None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(redact("nothing secret here", settings),
                             "nothing secret here")


class ValidationTests(unittest.TestCase):
    def assert_bad(self, text, fragment):
        with self.assertRaises(SettingsError) as ctx:
            load_text(text)
        self.assertIn(fragment, str(ctx.exception))

    def test_visibility_must_be_public_or_private(self):
        self.assert_bad(BASE.replace('visibility = "Public"', 'visibility = "Weird"'), "Weird")

    def test_test_env_needs_test_signal(self):
        text = BASE.replace('test_signal = { kind = "workflow", name = "deploy-test" }\n', "")
        self.assert_bad(text, "test_signal")

    def test_signal_kind_must_be_known(self):
        self.assert_bad(BASE.replace('kind = "workflow"', 'kind = "pigeon"'), "pigeon")

    def test_runner_limit_missing(self):
        self.assert_bad(BASE.replace("stall = 10\n", ""), "stall")

    def test_worker_env_defaults_empty_and_parses(self):
        self.assertEqual(load_text(BASE).runner.worker_env, ())
        text = BASE.replace("max_parallel = 2\n",
                            'max_parallel = 2\nworker_env = ["OPL_WORKER_KEY"]\n')
        self.assertEqual(load_text(text).runner.worker_env, ("OPL_WORKER_KEY",))

    def test_worker_env_refuses_other_roles_tokens(self):
        # A worker may only get its own key: never the admin, GitHub,
        # conductor or other models' token variables.
        for var in ("OPL_TOKEN_ADMIN", "OPL_GITHUB_TOKEN", "OPL_TOKEN_CONDUCTOR",
                    "OPL_TOKEN_CLAUDE", "OPL_TOKEN_CODEX", "OPL_TOKEN_SPARK"):
            with self.subTest(var=var):
                text = BASE.replace(
                    "max_parallel = 2\n",
                    'max_parallel = 2\nworker_env = ["%s"]\n' % var)
                self.assert_bad(text, var)

    def test_worker_env_must_be_names(self):
        text = BASE.replace("max_parallel = 2\n",
                            "max_parallel = 2\nworker_env = [1]\n")
        self.assert_bad(text, "worker_env")

    def test_permcheck_section_is_optional_but_parsed(self):
        self.assertEqual(load_text(BASE).permcheck, {})
        text = BASE + '\n[permcheck]\nproject = "Sandbox"\nfeature = "Test feature"\n'
        parsed = load_text(text)
        self.assertEqual(parsed.permcheck,
                         {"project": "Sandbox", "feature": "Test feature"})


class ReadinessTests(unittest.TestCase):
    """Issue #42: optional per-project runtime/setup/test (synthetic only)."""

    def assert_bad(self, text, fragment):
        with self.assertRaises(SettingsError) as ctx:
            load_text(text)
        self.assertIn(fragment, str(ctx.exception))

    def test_defaults_are_undeclared(self):
        project = load_text(BASE).projects[0]
        self.assertEqual(project.runtime, ())
        self.assertEqual(project.setup, "")
        self.assertEqual(project.test, "")

    def test_parses_runtime_setup_test(self):
        text = BASE + 'runtime = ["node@22"]\nsetup = "npm ci"\ntest = "npm test"\n'
        project = load_text(text).projects[0]
        self.assertEqual(project.runtime, ("node@22",))
        self.assertEqual(project.setup, "npm ci")
        self.assertEqual(project.test, "npm test")

    def test_unknown_runtime_allowed_but_reported(self):
        from opl.settings import unknown_runtimes

        text = BASE + 'runtime = ["node@22", "cobol@1959"]\n'
        project = load_text(text).projects[0]
        self.assertEqual(project.runtime, ("node@22", "cobol@1959"))
        self.assertEqual(unknown_runtimes(project), ("cobol@1959",))

    def test_known_runtime_not_reported(self):
        from opl.settings import unknown_runtimes

        text = BASE + 'runtime = ["node@22"]\n'
        self.assertEqual(unknown_runtimes(load_text(text).projects[0]), ())

    def test_runtime_must_be_list_of_strings(self):
        self.assert_bad(BASE + 'runtime = "node@22"\n', "runtime")
        self.assert_bad(BASE + 'runtime = [42]\n', "runtime")
        self.assert_bad(BASE + 'runtime = [""]\n', "runtime")
        self.assert_bad(BASE + 'runtime = ["  "]\n', "runtime")

    def test_setup_and_test_must_be_strings(self):
        self.assert_bad(BASE + 'setup = ["npm", "ci"]\n', "setup")
        self.assert_bad(BASE + 'setup = 42\n', "setup")
        self.assert_bad(BASE + 'test = ["npm", "test"]\n', "test")
        self.assert_bad(BASE + 'test = 42\n', "test")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class AutonomySettingsTests(unittest.TestCase):
    """Temporary autonomy table (#55)."""

    def load(self, table):
        import datetime
        from opl.settings import _opt_autonomy
        return _opt_autonomy({"autonomy": table} if table is not None else {}, "project x")

    def test_absent_is_off(self):
        self.assertEqual(self.load(None), ("", None))

    def test_parses_string_and_date(self):
        import datetime
        want = ("claude", datetime.date(2026, 10, 14))
        self.assertEqual(self.load({"lead": "claude", "until": "2026-10-14"}), want)
        self.assertEqual(self.load({"lead": "claude", "until": datetime.date(2026, 10, 14)}), want)

    def test_rejects_spark_missing_until_and_bad_date(self):
        from opl.settings import SettingsError
        for table in ({"lead": "spark", "until": "2026-10-14"},
                      {"lead": "claude"},
                      {"lead": "claude", "until": "soon"},
                      {"until": "2026-10-14"},
                      "claude"):
            with self.assertRaises(SettingsError):
                self.load(table)

    def test_active_only_until_the_date_inclusive(self):
        import datetime
        from opl.settings import Project, autonomy_active
        p = Project("k", "K", "o/r", "Public", False, autonomy_lead="claude",
                    autonomy_until=datetime.date(2026, 10, 14))
        self.assertEqual(autonomy_active(p, datetime.date(2026, 10, 14)), "claude")
        self.assertEqual(autonomy_active(p, datetime.date(2026, 10, 15)), "")
        self.assertEqual(autonomy_active(Project("k", "K", "o/r", "Public", False),
                                         datetime.date(2026, 10, 1)), "")
