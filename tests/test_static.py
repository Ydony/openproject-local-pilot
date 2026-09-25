#!/usr/bin/env python3
"""Static + synthetic checks for the OpenProject localhost operator toolkit.

Runs with stdlib only:  python3 tests/test_static.py   (or: bash tests/run.sh)

Covers SPEC deliverable 3 without any private data or live deployment:
  shell syntax | secret placeholders | loopback binding | unchanged
  upstream files | bounded waits | destructive restore safeguards |
  synthetic setup + shared-library behaviour.

Live Docker checks are intentionally NOT here; see tests/live_docker.sh,
which is optional, clearly labelled, and skipped unless RUN_LIVE_DOCKER=1.
"""

import os
import re
import hashlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(REPO, "bin")
LIB = os.path.join(REPO, "lib")
COMPOSE = os.path.join(REPO, "compose")
BIN_SCRIPTS = sorted(
    os.path.join(BIN, f) for f in os.listdir(BIN) if not f.startswith(".")
)
LIB_SCRIPTS = [os.path.join(LIB, "common.sh")]
FAKEBIN = os.path.join(REPO, "tests", "fakebin")
ALL_SH = (
    BIN_SCRIPTS
    + LIB_SCRIPTS
    + [os.path.join(REPO, "tests", "live_docker.sh")]
    + [os.path.join(FAKEBIN, "docker"), os.path.join(FAKEBIN, "curl")]
)

BASH = shutil.which("bash")
GIT = shutil.which("git")


def read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def run_bash(script, env_extra=None, timeout=120):
    env = dict(os.environ)
    env["OPL_ROOT"] = sh_path(REPO)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [BASH, "-c", script], capture_output=True, text=True, timeout=timeout, env=env
    )


def sh_path(path):
    """Translate a host path for bash.

    No-op on POSIX and on machines whose bash already accepts host paths.
    On Windows, converts via cygpath so Git Bash/MSYS tools accept it.
    Without this, no behaviour test can execute shell scripts from a
    Windows checkout; Linux CI is unaffected (os.name != "nt" short-circuits).
    """
    if os.name != "nt":
        return path
    cygpath = shutil.which("cygpath")
    if not cygpath:
        return path
    try:
        proc = subprocess.run(
            [cygpath, "-u", path], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return path
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return path


class SyntaxTests(unittest.TestCase):
    def test_bash_syntax(self):
        self.assertTrue(BASH, "bash is required to validate shell syntax")
        for path in ALL_SH:
            with self.subTest(path=os.path.basename(path)):
                proc = subprocess.run(
                    [BASH, "-n", sh_path(path)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(
                    proc.returncode, 0, f"bash -n failed for {path}: {proc.stderr}"
                )

    def test_scripts_are_executable(self):
        # Git's index mode, not os.access: files created on Windows check
        # out non-executable, so only 100755 in the index guarantees the
        # scripts run after a fresh clone (Linux CI included).
        self.assertTrue(GIT, "git is required to check index modes")
        proc = subprocess.run(
            [GIT, "-C", REPO, "ls-files", "-s"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        modes = {}
        for line in proc.stdout.splitlines():
            parts = line.split(None, 3)
            if len(parts) == 4:
                modes[parts[3]] = parts[0]
        checked = BIN_SCRIPTS + [
            os.path.join(REPO, "tests", "run.sh"),
            os.path.join(REPO, "tests", "live_docker.sh"),
            os.path.join(FAKEBIN, "docker"),
            os.path.join(FAKEBIN, "curl"),
        ]
        for path in checked:
            rel = os.path.relpath(path, REPO).replace(os.sep, "/")
            with self.subTest(path=rel):
                self.assertEqual(
                    modes.get(rel),
                    "100755",
                    "%s is not 100755 in the git index" % rel,
                )


class SecretPlaceholderTests(unittest.TestCase):
    LEAK_PATTERNS = [
        r"BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY",
        r"ghp_[A-Za-z0-9]{8,}",
        r"gho_[A-Za-z0-9]{8,}",
        r"AKIA[0-9A-Z]{16}",
        r"sk-live-[A-Za-z0-9]",
        r"xox[bpas]-[A-Za-z0-9]",
        r"p4ssw0rd",  # upstream default password must not leak into our files
        r"OVERWRITE_ME",
        r"OVERRIDE_ME_PLEASE",
    ]

    def test_no_secret_values_in_tracked_files(self):
        texts = {}
        for base in (BIN, LIB, COMPOSE, REPO):
            for name in sorted(os.listdir(base)):
                path = os.path.join(base, name)
                if os.path.isfile(path) and not name.startswith("."):
                    texts[path] = read(path)
        texts[os.path.join(REPO, ".env.example")] = read(
            os.path.join(REPO, ".env.example")
        )
        for path, content in sorted(texts.items()):
            for pattern in self.LEAK_PATTERNS:
                with self.subTest(file=os.path.basename(path), pattern=pattern):
                    self.assertIsNone(
                        re.search(pattern, content),
                        f"{path} looks like it holds a real secret ({pattern})",
                    )

    def test_env_example_has_only_placeholders(self):
        content = read(os.path.join(REPO, ".env.example"))
        safe = {"false", "true", "17-slim", "8080"}
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            _, value = line.split("=", 1)
            value = value.strip().strip("\"'")
            if len(value) < 16:
                continue
            with self.subTest(line=line):
                ok = value in safe or re.search(
                    r"(?i)change|example|placeholder|localhost|127\.0\.0\.1|your-|insert",
                    value,
                )
                self.assertTrue(ok, f".env.example value is not a placeholder: {line}")

    def test_scripts_never_echo_secrets(self):
        for path in BIN_SCRIPTS + LIB_SCRIPTS:
            content = read(path)
            for i, line in enumerate(content.splitlines(), 1):
                if re.search(r"\becho\b|\bprintf\b", line) and re.search(
                    r"\$(SECRET_KEY_BASE|POSTGRES_PASSWORD|SECRET|PASSWORD)",
                    line,
                ):
                    # Only the redacted diagnostic printer may touch secrets.
                    with self.subTest(file=os.path.basename(path), line=i):
                        self.assertIn(
                            "redact",
                            content,
                            f"{path}:{i} prints a secret variable: {line.strip()}",
                        )

    def test_runtime_secrets_are_gitignored_and_absent(self):
        gitignore = read(os.path.join(REPO, ".gitignore"))
        self.assertIn(".opl-runtime/", gitignore)
        self.assertRegex(gitignore, r"(?m)^\.env$")
        self.assertIn("backups/", gitignore)
        if GIT:
            proc = subprocess.run(
                [GIT, "-C", REPO, "ls-files"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            tracked = proc.stdout.splitlines()
            self.assertNotIn(".env", tracked)
            self.assertFalse(
                [t for t in tracked if t.startswith(".opl-runtime/")],
                "runtime dir must never be tracked",
            )
            self.assertFalse(
                [t for t in tracked if t.startswith("backups/")],
                "backup output must never be tracked",
            )


class LoopbackTests(unittest.TestCase):
    def test_override_template_renders_loopback_only(self):
        # The effective result, not the template text: render exactly like
        # bin/opl-setup does, then check non-comment lines. A comment
        # mentioning the forbidden address must neither trip nor bypass this.
        template = read(os.path.join(COMPOSE, "docker-compose.override.yml"))
        self.assertIn("__OPL_PORT__", template)
        rendered = template.replace("__OPL_LOOPBACK__", "127.0.0.1").replace(
            "__OPL_PORT__", "8080"
        )
        code = "\n".join(
            line for line in rendered.splitlines()
            if not line.strip().startswith("#")
        )
        self.assertIn("127.0.0.1", code)
        self.assertNotIn("0.0.0.0", code)

    def test_env_example_binds_loopback(self):
        example = read(os.path.join(REPO, ".env.example"))
        self.assertRegex(example, r"(?m)^PORT=127\.0\.0\.1:\d+")

    def test_no_public_bind_in_scripts_or_lib(self):
        for path in BIN_SCRIPTS + LIB_SCRIPTS:
            with self.subTest(path=os.path.basename(path)):
                self.assertNotIn("0.0.0.0", read(path))

    def test_health_checks_target_loopback(self):
        common = read(os.path.join(LIB, "common.sh"))
        self.assertIn("OPL_LOOPBACK", common)
        start = read(os.path.join(BIN, "opl-start"))
        self.assertIn("health_url", start)


class UpstreamPristineTests(unittest.TestCase):
    # Markers of the real upstream bundle that must never be vendored into
    # our own tracked files (the bundle lives only in the ignored runtime dir).
    # NOTE: bare service names such as "hocuspocus" are deliberately NOT
    # markers: scripts must name services to start them ("compose up -d web
    # proxy ..."), and naming a service is not vendoring upstream content.
    UPSTREAM_MARKERS = [
        "x-op-app",
        "openproject/openproject:",
        "openproject/proxy",
        "willfarrell/autoheal",
        "memcached",
    ]

    def test_no_vendored_upstream_in_toolkit_files(self):
        checked = BIN_SCRIPTS + LIB_SCRIPTS + [
            os.path.join(COMPOSE, "docker-compose.override.yml")
        ]
        for path in checked:
            content = read(path)
            for marker in self.UPSTREAM_MARKERS:
                with self.subTest(file=os.path.basename(path), marker=marker):
                    self.assertNotIn(
                        marker, content, f"{path} vendors upstream content"
                    )

    def test_override_only_pins_proxy_ports(self):
        template = read(os.path.join(COMPOSE, "docker-compose.override.yml"))
        self.assertIn("proxy:", template)
        self.assertIn("ports:", template)
        for marker in ("image:", "build:", "environment:", "volumes:"):
            self.assertNotIn(marker, template)


class BoundedWaitTests(unittest.TestCase):
    def test_wait_until_helper_exists_and_is_bounded(self):
        common = read(os.path.join(LIB, "common.sh"))
        self.assertIn("wait_until()", common)
        self.assertIn("SECONDS", common)
        self.assertIn("timed out after", common)

    def test_bin_scripts_do_not_sleep_directly(self):
        for path in BIN_SCRIPTS:
            for i, line in enumerate(read(path).splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                with self.subTest(file=os.path.basename(path), line=i):
                    self.assertNotRegex(
                        stripped,
                        r"(^|[;&|])\s*sleep\b",
                        f"{path}:{i} sleeps directly; use wait_until() instead",
                    )

    def test_no_unbounded_loops_outside_common_lib(self):
        for path in BIN_SCRIPTS + [os.path.join(REPO, "tests", "live_docker.sh")]:
            for i, line in enumerate(read(path).splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                with self.subTest(file=os.path.basename(path), line=i):
                    self.assertNotRegex(
                        stripped,
                        r"while\s+(true|:)\b",
                        f"{path}:{i} unbounded loop",
                    )
                    self.assertNotIn("sleep infinity", stripped)

    def test_staged_scripts_use_bounded_waits(self):
        start = read(os.path.join(BIN, "opl-start"))
        self.assertIn("wait_until", start)
        self.assertNotRegex(start, r"(^|\n)\s*compose up -d\s*(\n|$)")
        restore = read(os.path.join(BIN, "opl-restore-test"))
        self.assertIn("wait_until", restore)


class DestructiveSafeguardTests(unittest.TestCase):
    def test_no_volume_deletion_for_active_pilot(self):
        for path in BIN_SCRIPTS:
            name = os.path.basename(path)
            content = read(path)
            for i, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                with self.subTest(file=name, line=i):
                    self.assertNotRegex(
                        stripped, r"volume\s+(rm|prune)\b", f"{name}:{i} deletes volumes"
                    )
                    self.assertNotRegex(
                        stripped, r"system\s+prune\b", f"{name}:{i} prunes docker"
                    )
                    self.assertNotRegex(
                        stripped, r"rm\s+-rf\s+(/|~)(\s|$|/)", f"{name}:{i} rm -rf /"
                    )
            if name == "opl-restore-test":
                continue
            with self.subTest(file=name):
                self.assertNotRegex(
                    content, r"down\s+[^\n]*--volumes|down\s+[^\n]*-v\b",
                    f"{name} deletes volumes via down",
                )

    def test_stop_keeps_volumes_by_default(self):
        stop = read(os.path.join(BIN, "opl-stop"))
        self.assertIn("compose stop", stop)
        self.assertNotIn("--volumes", stop)

    def test_restore_cleanup_is_scoped_to_disposable(self):
        restore = read(os.path.join(BIN, "opl-restore-test"))
        self.assertIn("refuse_active_project", restore)
        # Cleanup runs on EXIT with the status passed through, so deleting
        # the disposable project can never mask a proof failure.
        self.assertRegex(restore, r"(?m)^trap 'rc=\$\?; cleanup; exit \$rc' EXIT")
        # The dcompose wrapper is proven disposable-scoped here (it pins the
        # throwaway project name), so --volumes on a dcompose line is safe.
        self.assertRegex(
            restore,
            r"(?m)^dcompose\(\) \{\n(?:.*\n)*?.*--project-name \"\$DISP\"",
            "dcompose must pin the disposable project name",
        )
        for i, line in enumerate(restore.splitlines(), 1):
            if "--volumes" in line and not line.strip().startswith("#"):
                with self.subTest(line=i):
                    self.assertRegex(
                        line.strip(),
                        r"^dcompose\b",
                        f"restore-test:{i} volume deletion is not scoped to disposable",
                    )
        # Active-project refusal is re-asserted inside the cleanup path.
        self.assertIn('"$DISP" = "$OPL_PROJECT"', restore)


@unittest.skipUnless(BASH, "bash is required for synthetic behaviour tests")
class CommonLibTests(unittest.TestCase):
    def test_refuse_active_project(self):
        proc = run_bash(
            'source "$OPL_ROOT/lib/common.sh"; refuse_active_project "$OPL_PROJECT"'
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("ACTIVE", proc.stderr)
        proc = run_bash(
            'source "$OPL_ROOT/lib/common.sh"; refuse_active_project opl-restore-x'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = run_bash('source "$OPL_ROOT/lib/common.sh"; refuse_active_project ""')
        self.assertNotEqual(proc.returncode, 0)

    def test_wait_until_success_and_timeout(self):
        proc = run_bash(
            'source "$OPL_ROOT/lib/common.sh"; wait_until 30 1 "t" -- true'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        started = time.monotonic()
        proc = run_bash(
            'source "$OPL_ROOT/lib/common.sh"; wait_until 3 1 "t" -- false'
        )
        elapsed = time.monotonic() - started
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("timed out", proc.stderr)
        self.assertLess(elapsed, 15, "wait_until exceeded its bound generously")

    def test_assert_loopback_override_checks_every_entry(self):
        cases = [
            # (content, passes?)
            ('services:\n  proxy:\n    ports:\n      - "127.0.0.1:8080:80"\n', True),
            # A bare port next to a loopback line is still public.
            ('services:\n  proxy:\n    ports:\n      - "127.0.0.1:8080:80"\n      - "8080:80"\n',
             False),
            ('services:\n  proxy:\n    ports:\n      - "0.0.0.0:8080:80"\n', False),
            ('services:\n  proxy:\n    ports:\n', False),
        ]
        for content, passes in cases:
            with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
                fh.write(content)
                path = fh.name
            try:
                proc = run_bash(
                    'source "$OPL_ROOT/lib/common.sh"; assert_loopback_override "$SH_FILE"',
                    env_extra={"SH_FILE": sh_path(path)},
                )
            finally:
                os.unlink(path)
            with self.subTest(content=content):
                if passes:
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                else:
                    self.assertNotEqual(proc.returncode, 0)

    def test_assert_loopback_env(self):
        for content, passes in [
            ("PORT=127.0.0.1:8080\n", True),
            ("PORT=8080\n", False),
            ("PORT=0.0.0.0:8080\n", False),
            ("PORT=127.0.0.1:notaport\n", False),
            ("# nothing here\n", False),
        ]:
            with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
                fh.write(content)
                path = fh.name
            try:
                proc = run_bash(
                    'source "$OPL_ROOT/lib/common.sh"; assert_loopback_env "$SH_FILE"',
                    env_extra={"SH_FILE": sh_path(path)},
                )
            finally:
                os.unlink(path)
            with self.subTest(content=content):
                if passes:
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                else:
                    self.assertNotEqual(proc.returncode, 0)

    def test_assert_loopback_override_ignores_comments(self):
        good = tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False)
        try:
            good.write(
                "# never publish on a public wildcard address\n"
                "services:\n  proxy:\n    ports:\n"
                '      - "127.0.0.1:8080:80"\n'
            )
            good.close()
            proc = run_bash(
                'source "$OPL_ROOT/lib/common.sh"; assert_loopback_override "$SH_FILE"',
                env_extra={"SH_FILE": sh_path(good.name)},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
        finally:
            os.unlink(good.name)
        bad = tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False)
        try:
            bad.write('ports:\n  - "0.0.0.0:8080:80"\n')
            bad.close()
            proc = run_bash(
                'source "$OPL_ROOT/lib/common.sh"; assert_loopback_override "$SH_FILE"',
                env_extra={"SH_FILE": sh_path(bad.name)},
            )
            self.assertNotEqual(proc.returncode, 0)
        finally:
            os.unlink(bad.name)

    def test_redact_line_masks_secrets(self):
        proc = run_bash(
            'source "$OPL_ROOT/lib/common.sh"; redact_line "POSTGRES_PASSWORD=hunter2"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("POSTGRES_PASSWORD=***", proc.stdout)
        self.assertNotIn("hunter2", proc.stdout)
        proc = run_bash(
            'source "$OPL_ROOT/lib/common.sh"; redact_line "DATABASE_URL=postgres://postgres:hunter2@db/x"'
        )
        self.assertIn("DATABASE_URL=***", proc.stdout)
        self.assertNotIn("hunter2", proc.stdout)

    def test_urlencode_escapes_password_specials(self):
        proc = run_bash(
            'source "$OPL_ROOT/lib/common.sh"; urlencode "a@b:c/d"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "a%40b%3Ac%2Fd")
        proc = run_bash('source "$OPL_ROOT/lib/common.sh"; urlencode "abcXYZ019"')
        self.assertEqual(proc.stdout, "abcXYZ019")

    def test_assert_tuning_only(self):
        cases = [
            ("services:\n  worker:\n    mem_limit: 1g\n", True),
            ("# mentions ports: only in a comment\n", True),
            ('services:\n  proxy:\n    ports:\n      - "0.0.0.0:8080:80"\n', False),
            ("services:\n  web:\n    network_mode: host\n", False),
        ]
        for content, passes in cases:
            with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
                fh.write(content)
                path = fh.name
            try:
                proc = run_bash(
                    'source "$OPL_ROOT/lib/common.sh"; assert_tuning_only "$SH_FILE"',
                    env_extra={"SH_FILE": sh_path(path)},
                )
            finally:
                os.unlink(path)
            with self.subTest(content=content):
                if passes:
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                else:
                    self.assertNotEqual(proc.returncode, 0)
        proc = run_bash(
            'source "$OPL_ROOT/lib/common.sh"; assert_tuning_only "$OPL_ROOT/no-such-file.yml"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_compose_adds_local_file_only_when_present(self):
        for present, count in ((False, 2), (True, 3)):
            tmp = tempfile.mkdtemp(prefix="opl-local-")
            try:
                log = os.path.join(tmp, "docker.log")
                with open(os.path.join(tmp, "compose-config.yml"),
                          "w", newline="\n") as fh:
                    fh.write("# synthetic rendered config\n")
                if present:
                    with open(os.path.join(tmp, "docker-compose.local.yml"),
                              "w", newline="\n") as fh:
                        fh.write("services:\n  worker:\n    mem_limit: 1g\n")
                proc = run_bash(
                    'source "$OPL_ROOT/lib/common.sh"; compose config',
                    env_extra={
                        "OPL_RUNTIME_DIR": sh_path(tmp),
                        "OPL_PROJECT": "opl-local-test",
                        "PATH": sh_path(FAKEBIN) + ":/usr/bin:/bin",
                        "FAKE_DOCKER_STATE": sh_path(tmp),
                        "FAKE_DOCKER_LOG": sh_path(log),
                    },
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                with open(log, encoding="utf-8", errors="replace") as fh:
                    logged = fh.read()
                self.assertEqual(logged.count("-f "), count,
                                 "expected %d compose files:\n%s" % (count, logged))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(BASH and GIT, "bash+git required for synthetic setup test")
class SyntheticSetupTests(unittest.TestCase):
    """End-to-end opl-setup against a local synthetic upstream repo (offline)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opl-synth-")
        # Build a tiny local git repo standing in for the upstream bundle.
        self.upstream = os.path.join(self.tmp, "upstream")
        shutil.copytree(os.path.join(REPO, "tests", "fixtures", "upstream"), self.upstream)
        # Explicit branch: `git init` alone follows the machine's
        # init.defaultBranch, which differs between hosts.
        for args in (["init", "-q", "-b", "master"], ["add", "-A"]):
            subprocess.run([GIT, "-C", self.upstream] + args, check=True, timeout=60)
        subprocess.run(
            [GIT, "-C", self.upstream, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-qm", "fixture"],
            check=True,
            timeout=60,
        )
        self.env = {
            "OPL_RUNTIME_DIR": os.path.join(self.tmp, "runtime"),
            "OPL_BACKUP_ROOT": os.path.join(self.tmp, "backups"),
            "OPL_UPSTREAM_URL": self.upstream,
            "OPL_UPSTREAM_REF": "master",
            "OPL_PROJECT": "opl-synth-test",
            "OPL_PORT": "8123",
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_setup(self, path_prefix=None):
        env = dict(os.environ)
        env["OPL_ROOT"] = sh_path(REPO)
        bash_env = dict(self.env)
        # Bash file operations need POSIX paths; python assertions and the
        # Windows git binary keep using self.env untouched.
        bash_env["OPL_RUNTIME_DIR"] = sh_path(self.env["OPL_RUNTIME_DIR"])
        bash_env["OPL_BACKUP_ROOT"] = sh_path(self.env["OPL_BACKUP_ROOT"])
        bash_env["OPL_UPSTREAM_URL"] = sh_path(self.env["OPL_UPSTREAM_URL"])
        env.update(bash_env)
        if path_prefix is not None:
            env["PATH"] = sh_path(path_prefix) + ":" + env["PATH"]
        return subprocess.run(
            [BASH, sh_path(os.path.join(BIN, "opl-setup"))],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
            stdin=subprocess.DEVNULL,
        )

    def test_setup_is_idempotent_and_keeps_secrets(self):
        first = self.run_setup()
        self.assertEqual(first.returncode, 0, first.stderr)
        env_file = os.path.join(self.env["OPL_RUNTIME_DIR"], ".env")
        override = os.path.join(self.env["OPL_RUNTIME_DIR"], "docker-compose.override.yml")
        self.assertTrue(os.path.isfile(env_file))
        self.assertTrue(os.path.isfile(override))
        if os.name == "nt":
            # NTFS cannot express 0600 through os.stat; existence is the
            # check here, strict mode is asserted on POSIX below/CI.
            self.assertTrue(os.path.isfile(env_file))
        else:
            self.assertEqual(oct(os.stat(env_file).st_mode & 0o777), "0o600")
        content = read(env_file)
        self.assertIn("PORT=127.0.0.1:8123", content)
        self.assertNotIn("CHANGE_ME", content)
        self.assertIn("POSTGRES_VERSION=17", content)
        self.assertIn("OPENPROJECT_HOST__NAME=127.0.0.1:8123", content)
        self.assertIn("OPENPROJECT_HSTS=false", content)
        self.assertIn("COLLABORATIVE_SERVER_URL=ws://127.0.0.1:8123/hocuspocus", content)
        self.assertRegex(content, r"(?m)^DATABASE_URL=postgres://postgres:.+@db/openproject")
        rendered = read(override)
        self.assertIn("127.0.0.1:8123:80", rendered)
        self.assertNotIn("0.0.0.0", rendered)
        before = read(env_file)

        second = self.run_setup()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("keeping existing secrets", second.stdout + second.stderr)
        self.assertEqual(read(env_file), before, "second setup replaced secrets")

    def test_setup_backfills_missing_keys_without_touching_existing(self):
        # Simulate a .env left by an earlier setup: only the old keys.
        self.run_setup()
        env_file = os.path.join(self.env["OPL_RUNTIME_DIR"], ".env")
        with open(env_file, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("PORT=127.0.0.1:8123\nSECRET_KEY_BASE=oldsecret\nPOSTGRES_PASSWORD=oldpw\n")
        proc = self.run_setup()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        content = read(env_file)
        self.assertIn("POSTGRES_PASSWORD=oldpw", content)
        self.assertIn("SECRET_KEY_BASE=oldsecret", content)
        self.assertNotIn("POSTGRES_VERSION", content)
        self.assertRegex(
            content,
            r"(?m)^DATABASE_URL=postgres://postgres:oldpw@db/openproject",
        )
        self.assertIn("OPENPROJECT_HOST__NAME=127.0.0.1:8123", content)
        self.assertIn("COLLABORATIVE_SERVER_URL=ws://127.0.0.1:8123/hocuspocus", content)
        self.assertRegex(content, r"(?m)^COLLABORATIVE_SERVER_SECRET=\S+")
        self.assertIn("OPENPROJECT_HSTS=false", content)

    def test_backfill_never_glues_onto_a_line_without_newline(self):
        # Hand-edited files on Windows often lack the final newline.
        self.run_setup()
        env_file = os.path.join(self.env["OPL_RUNTIME_DIR"], ".env")
        with open(env_file, "w", encoding="utf-8", newline="") as fh:
            fh.write("PORT=127.0.0.1:8123\nTAG=17-slim")
        proc = self.run_setup()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        content = read(env_file)
        self.assertIn("TAG=17-slim\n", content)
        for line in content.splitlines():
            self.assertNotIn("TAG=17-slimDATABASE_URL", line)
        # No POSTGRES_PASSWORD here, so no DATABASE_URL is guessed (T0.2
        # rule); the other missing keys still land on their own lines.
        self.assertNotIn("DATABASE_URL", content)
        self.assertRegex(content, r"(?m)^COLLABORATIVE_SERVER_SECRET=\S+")
        self.assertIn("OPENPROJECT_HSTS=false\n", content)

    def test_setup_accepts_tuning_local_file(self):
        self.run_setup()
        with open(os.path.join(self.env["OPL_RUNTIME_DIR"],
                               "docker-compose.local.yml"),
                  "w", encoding="utf-8", newline="\n") as fh:
            fh.write("services:\n  worker:\n    mem_limit: 1g\n")
        proc = self.run_setup()
        self.assertEqual(proc.returncode, 0, proc.stderr)

    @unittest.skipUnless(os.name != "nt", "POSIX file modes only")
    def test_new_env_is_600_from_creation(self):
        # chmod is stubbed to a no-op: if the mode is still 600, the umask
        # (not the later chmod) made it so.
        stubs = os.path.join(self.tmp, "stubs")
        os.mkdir(stubs)
        stub = os.path.join(stubs, "chmod")
        with open(stub, "w", newline="\n") as fh:
            fh.write("#!/usr/bin/env bash\nexit 0\n")
        os.chmod(stub, 0o755)
        proc = self.run_setup(path_prefix=stubs)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        env_file = os.path.join(self.env["OPL_RUNTIME_DIR"], ".env")
        self.assertEqual(oct(os.stat(env_file).st_mode & 0o777), "0o600")


@unittest.skipUnless(BASH, "bash is required for fake-docker behaviour tests")
class FakeDockerTests(unittest.TestCase):
    """Behaviour tests against a scripted fake `docker` (+`curl`) on PATH.

    No daemon, no network, no containers: tests/fakebin/docker logs every
    call to $FAKE_DOCKER_LOG and answers from fixture files under
    $FAKE_DOCKER_STATE. Covers status mapping, backup failure cleanup and
    retention, restore refusal guards, and disposable-only cleanup.
    """

    PROJECT = "opl-fake-test"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opl-fake-")
        self.state = os.path.join(self.tmp, "state")
        os.mkdir(self.state)
        self.log = os.path.join(self.tmp, "docker.log")
        self.runtime = os.path.join(self.tmp, "runtime")
        os.mkdir(self.runtime)
        shutil.copy(
            os.path.join(REPO, "tests", "fixtures", "upstream", "docker-compose.yml"),
            os.path.join(self.runtime, "docker-compose.yml"),
        )
        with open(os.path.join(self.runtime, "docker-compose.override.yml"), "w", newline="\n") as fh:
            fh.write('services:\n  proxy:\n    ports:\n      - "127.0.0.1:8123:80"\n')
        with open(os.path.join(self.runtime, ".env"), "w", newline="\n") as fh:
            fh.write("PORT=127.0.0.1:8123\nPOSTGRES_PASSWORD=fakepw\n")
        self.backups = os.path.join(self.tmp, "backups")
        os.mkdir(self.backups)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixtures -----------------------------------------------------
    def write_state(self, name, content=""):
        # LF always: these files stand in for real docker output, and bash
        # string matching (container ids, inspect values) chokes on
        # Windows CRLF (an id of "db1\r" matches no fixture file).
        with open(os.path.join(self.state, name), "w", newline="\n") as fh:
            fh.write(content)

    def set_container(self, cid, name, status, exitcode, running, oom="false"):
        self.write_state("name-%s" % cid, "/%s-%s-1\n" % (self.PROJECT, name))
        self.write_state("status-%s" % cid, "%s\n" % status)
        self.write_state("exit-%s" % cid, "%s\n" % exitcode)
        self.write_state("running-%s" % cid, "%s\n" % running)
        self.write_state("oom-%s" % cid, "%s\n" % oom)

    def set_ps(self, *cids):
        self.write_state("ps.txt", "".join("%s\n" % c for c in cids))

    COMPOSE_CLEAN = (
        "services:\n  db:\n    image: postgres:17\n    volumes:\n"
        "      - type: volume\n        source: pgdata\n"
        "        target: /var/lib/postgresql/data\n"
    )

    def script_env(self, extra=None):
        env = dict(os.environ)
        env["OPL_ROOT"] = sh_path(REPO)
        env["OPL_RUNTIME_DIR"] = sh_path(self.runtime)
        env["OPL_BACKUP_ROOT"] = sh_path(self.backups)
        env["OPL_PROJECT"] = self.PROJECT
        env["OPL_PORT"] = "8123"
        env["FAKE_DOCKER_STATE"] = sh_path(self.state)
        env["FAKE_DOCKER_LOG"] = sh_path(self.log)
        env["PATH"] = sh_path(FAKEBIN) + ":/usr/bin:/bin"
        if extra:
            env.update(extra)
        return env

    def run_tool(self, name, args=(), extra=None, timeout=120):
        # Pin the fakes by exported shell functions, not just PATH order:
        # Git-Bash's runtime prepends /mingw64/bin to every process PATH, so
        # its real curl would otherwise shadow tests/fakebin/curl on Windows
        # dev machines (a command hash does not survive the exec of the
        # script, but exported functions are inherited by child bash).
        # Same behaviour on Linux CI, where PATH order alone already wins.
        wrapper = (
            'FAKEBIN="$1"; shift; export FAKEBIN; '
            'docker() { "$FAKEBIN/docker" "$@"; }; '
            'curl() { "$FAKEBIN/curl" "$@"; }; '
            'export -f docker curl; '
            'exec "$@"'
        )
        return subprocess.run(
            [BASH, "-c", wrapper, "opl-fakebash", sh_path(FAKEBIN)]
            + [sh_path(os.path.join(BIN, name))]
            + list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self.script_env(extra),
            stdin=subprocess.DEVNULL,
        )

    def docker_log(self):
        if not os.path.isfile(self.log):
            return ""
        with open(self.log) as fh:
            return fh.read()

    def make_backup_dir(self, name, files=2):
        """Hand-build a complete backup dir (manifest + checksums)."""
        bdir = os.path.join(self.backups, name)
        os.mkdir(bdir)
        with open(os.path.join(bdir, "database.sql"), "w") as fh:
            fh.write("SELECT 1;\n")
        asset_src = os.path.join(self.tmp, "asset-src")
        if os.path.isdir(asset_src):
            shutil.rmtree(asset_src)
        os.mkdir(asset_src)
        for i in range(files):
            with open(os.path.join(asset_src, "file%d.txt" % i), "w") as fh:
                fh.write("asset %d\n" % i)
        with tarfile.open(os.path.join(bdir, "assets.tgz"), "w:gz") as tar:
            for entry in sorted(os.listdir(asset_src)):
                tar.add(os.path.join(asset_src, entry), arcname=entry)
        lines = []
        for fname in ("database.sql", "assets.tgz"):
            with open(os.path.join(bdir, fname), "rb") as fh:
                lines.append(
                    "%s  %s" % (hashlib.sha256(fh.read()).hexdigest(), fname)
                )
        with open(os.path.join(bdir, "SHA256SUMS"), "w", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        with open(os.path.join(bdir, "manifest.json"), "w", newline="\n") as fh:
            fh.write('{"tool": "opl-backup", "project": "x"}\n')
        return bdir

    # -- status mapping ------------------------------------------------
    def test_start_call_order(self):
        log = self._start_and_read_log(())
        order = [
            "up -d --no-deps db cache",
            "exec -T db pg_isready",
            "up -d --no-deps seeder",
            "up -d --no-deps web proxy",
            "up -d --no-deps worker cron hocuspocus autoheal",
        ]
        positions = []
        for step in order:
            at = log.find(step)
            self.assertNotEqual(at, -1, "missing step: %s\n%s" % (step, log))
            positions.append(at)
        self.assertEqual(positions, sorted(positions),
                         "start phases out of order:\n%s" % log)

    def test_start_no_background_omits_last_phase(self):
        log = self._start_and_read_log(("--no-background",))
        self.assertIn("up -d --no-deps web proxy", log)
        self.assertNotIn("worker cron hocuspocus autoheal", log)

    def _start_and_read_log(self, args):
        # A finished seeder plus healthy web, so every phase completes fast.
        self.set_ps("seed1")
        self.write_state("name-seed1", "/opl-fake-test-seeder-1\n")
        self.write_state("status-seed1", "exited\n")
        self.write_state("exit-seed1", "0\n")
        self.write_state("running-seed1", "false\n")
        self.write_state("oom-seed1", "false\n")
        proc = self.run_tool("opl-start", args=args,
                             extra={"FAKE_CURL_EXIT": "0"})
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        return self.docker_log()

    def _write_local(self, content):
        with open(os.path.join(self.runtime, "docker-compose.local.yml"),
                  "w", newline="\n") as fh:
            fh.write(content)

    def test_start_refuses_publish_local_file(self):
        self._write_local('services:\n  proxy:\n    ports:\n      - "0.0.0.0:8080:80"\n')
        if os.path.isfile(self.log):
            os.unlink(self.log)
        proc = self.run_tool("opl-start")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("tune", proc.stdout + proc.stderr)
        self.assertFalse(os.path.isfile(self.log),
                         "opl-start called docker before refusing")

    def test_restore_refuses_publish_local_file(self):
        bdir = self.make_backup_dir("backup-probe")
        self._write_local('services:\n  web:\n    network_mode: host\n')
        proc = self.run_tool(
            "opl-restore-test", args=(sh_path(bdir), "--disposable", "opl-restore-t9")
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("tune", proc.stdout + proc.stderr)
        self.assertNotIn(" up ", " ".join(self.docker_log().splitlines()))

    def test_start_refuses_public_port_before_any_docker_call(self):
        for port in ("8080", "0.0.0.0:8080"):
            with open(os.path.join(self.runtime, ".env"), "w", newline="\n") as fh:
                fh.write("PORT=%s\nPOSTGRES_PASSWORD=fakepw\n" % port)
            if os.path.isfile(self.log):
                os.unlink(self.log)
            proc = self.run_tool("opl-start")
            self.assertNotEqual(proc.returncode, 0, port)
            self.assertFalse(
                os.path.isfile(self.log),
                "opl-start called docker before refusing %s" % port,
            )

    def test_status_stopped_when_nothing_runs(self):
        proc = self.run_tool("opl-status")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("STATUS=stopped", proc.stdout)

    def test_status_starting_ready_unhealthy(self):
        self.set_container("db1", "db", "running", "0", "true")
        self.set_container("web1", "web", "running", "0", "true")
        self.set_ps("db1", "web1")
        proc = self.run_tool("opl-status", extra={"FAKE_CURL_EXIT": "1"})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("STATUS=starting", proc.stdout)

        proc = self.run_tool("opl-status", extra={"FAKE_CURL_EXIT": "0"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("STATUS=ready", proc.stdout)
        self.assertIn("127.0.0.1", self.docker_log())

        self.set_container("web1", "web", "exited", "1", "false")
        proc = self.run_tool("opl-status", extra={"FAKE_CURL_EXIT": "1"})
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("STATUS=unhealthy", proc.stdout)

    def test_status_stopped_after_compose_stop(self):
        # compose stop leaves containers exited(0): restorable, not a fault.
        self.set_container("db1", "db", "exited", "0", "false")
        self.set_container("web1", "web", "exited", "0", "false")
        self.set_ps("db1", "web1")
        proc = self.run_tool("opl-status")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("STATUS=stopped", proc.stdout)

    def test_status_restarting_is_unhealthy(self):
        self.set_container("db1", "db", "running", "0", "true")
        self.set_container("c1", "cache", "restarting", "0", "false")
        self.set_ps("db1", "c1")
        proc = self.run_tool("opl-status", extra={"FAKE_CURL_EXIT": "1"})
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("STATUS=unhealthy", proc.stdout)

    # -- backup ----------------------------------------------------------
    def test_backup_failure_removes_partial_folder(self):
        self.set_container("db1", "db", "running", "0", "true")
        self.set_ps("db1")
        proc = self.run_tool("opl-backup", extra={"FAKE_PGDUMP_EXIT": "1"})
        self.assertNotEqual(proc.returncode, 0)
        leftovers = [
            d for d in os.listdir(self.backups)
            if d.startswith("backup-") or ".partial" in d
        ]
        self.assertEqual(leftovers, [], "partial backup folder was left behind")

    def test_two_backups_in_one_second_never_collide(self):
        # TH.3: second-resolution names used to reuse and truncate a finished
        # backup. Each run must get its own complete folder.
        self.set_container("db1", "db", "running", "0", "true")
        self.set_ps("db1")
        # A frozen clock makes both runs land in the same second for sure.
        frozen = os.path.join(self.tmp, "frozen-date")
        os.mkdir(frozen)
        with open(os.path.join(frozen, "date"), "w", newline="\n") as fh:
            fh.write("#!/bin/sh\necho 20260925-120000\n")
        os.chmod(os.path.join(frozen, "date"), 0o755)
        path = sh_path(frozen) + ":" + sh_path(FAKEBIN) + ":/usr/bin:/bin"
        for _ in range(2):
            proc = self.run_tool("opl-backup", args=("--keep", "5"),
                                 extra={"PATH": path})
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        done = sorted(d for d in os.listdir(self.backups) if d.startswith("backup-"))
        self.assertEqual(len(done), 2, done)
        for d in done:
            self.assertTrue(os.path.isfile(os.path.join(self.backups, d, "manifest.json")))
        self.assertFalse([d for d in os.listdir(self.backups) if ".partial" in d])

    def test_urlencode_never_puts_the_password_in_argv(self):
        # TH.3: a python3 stand-in records its argv; the password must reach
        # the encoder over stdin, never as an argument.
        argv_log = os.path.join(self.tmp, "argv.log")
        shim = os.path.join(self.tmp, "shim")
        os.mkdir(shim)
        real = sh_path(sys.executable)
        with open(os.path.join(shim, "python3"), "w", newline="\n") as fh:
            fh.write('#!/bin/sh\nprintf "%%s\\n" "$*" >> "%s"\nexec "%s" "$@"\n'
                     % (sh_path(argv_log), real))
        os.chmod(os.path.join(shim, "python3"), 0o755)
        proc = subprocess.run(
            [BASH, "-c", 'PATH="$1:$PATH"; source "$OPL_ROOT/lib/common.sh"; '
                         'urlencode "hunter2-synthetic"', "x", sh_path(shim)],
            capture_output=True, text=True, timeout=60,
            env=dict(os.environ, OPL_ROOT=sh_path(REPO)))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "hunter2-synthetic")
        with open(argv_log) as fh:
            self.assertNotIn("hunter2-synthetic", fh.read())

    def test_held_retention_lock_is_never_taken_over(self):
        # TH.22 (Codex F5): without flock, an existing lock dir is never
        # removed automatically, whatever its PID file says (dead, live,
        # unreadable, missing). The backup still completes; retention
        # waits for a person; the lock and the old backup stay untouched.
        old = os.path.join(self.backups, "backup-20200101-000000")
        lock = os.path.join(self.backups, ".retention.lock.d")
        for pid in ("4294967295\n", "%d\n" % os.getpid(), "garbage\n", None):
            with self.subTest(pid=pid):
                os.makedirs(old, exist_ok=True)
                with open(os.path.join(old, "manifest.json"), "w") as fh:
                    fh.write('{"tool": "opl-backup", "project": "%s"}\n'
                             % self.PROJECT)
                os.makedirs(lock, exist_ok=True)
                if pid is not None:
                    with open(os.path.join(lock, "pid"), "w") as fh:
                        fh.write(pid)
                self.set_container("db1", "db", "running", "0", "true")
                self.set_ps("db1")
                proc = self.run_tool("opl-backup", args=("--keep", "1"),
                                     extra={"OPL_BACKUP_NO_FLOCK": "1"})
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                self.assertIn("skipping retention", proc.stderr + proc.stdout)
                self.assertTrue(os.path.isdir(lock), "lock was removed")
                self.assertTrue(os.path.isdir(old), "retention ran anyway")
                shutil.rmtree(lock)
                for name in os.listdir(self.backups):
                    if name.startswith("backup-") and name != os.path.basename(old):
                        shutil.rmtree(os.path.join(self.backups, name))

    def test_retention_resumes_once_the_lock_is_gone(self):
        old = os.path.join(self.backups, "backup-20200101-000000")
        os.mkdir(old)
        with open(os.path.join(old, "manifest.json"), "w") as fh:
            fh.write('{"tool": "opl-backup", "project": "%s"}\n' % self.PROJECT)
        self.set_container("db1", "db", "running", "0", "true")
        self.set_ps("db1")
        proc = self.run_tool("opl-backup", args=("--keep", "1"),
                             extra={"OPL_BACKUP_NO_FLOCK": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertFalse(os.path.isdir(old), "retention did not run")
        self.assertFalse(os.path.isdir(
            os.path.join(self.backups, ".retention.lock.d")), "lock not released")

    def test_backup_retention_prunes_manifests_only(self):
        old = os.path.join(self.backups, "backup-20200101-000000")
        os.mkdir(old)
        with open(os.path.join(old, "manifest.json"), "w") as fh:
            fh.write('{"tool": "opl-backup", "project": "%s"}\n' % self.PROJECT)
        junk = os.path.join(self.backups, "backup-junk-no-manifest")
        os.mkdir(junk)
        with open(os.path.join(junk, "notes.txt"), "w") as fh:
            fh.write("not ours\n")
        # TH.3: a manifest from another project/tool is never ours to prune.
        foreign = os.path.join(self.backups, "backup-20190101-000000")
        os.mkdir(foreign)
        with open(os.path.join(foreign, "manifest.json"), "w") as fh:
            fh.write('{"tool": "opl-backup", "project": "someone-else"}\n')
        self.set_container("db1", "db", "running", "0", "true")
        self.set_ps("db1")
        proc = self.run_tool("opl-backup", args=("--keep", "1"))
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertFalse(os.path.isdir(old), "old manifest backup was not pruned")
        self.assertTrue(os.path.isdir(junk), "folder without manifest was deleted")
        self.assertTrue(os.path.isdir(foreign), "another project's backup was pruned")
        fresh = [
            d
            for d in os.listdir(self.backups)
            if d.startswith("backup-")
            and d not in ("backup-junk-no-manifest", "backup-20190101-000000")
        ]
        self.assertEqual(len(fresh), 1)
        for fname in ("manifest.json", "SHA256SUMS", "database.sql", "assets.tgz"):
            self.assertTrue(
                os.path.isfile(os.path.join(self.backups, fresh[0], fname)),
                "complete backup is missing %s" % fname,
            )

    # -- restore -----------------------------------------------------------
    def test_restore_refuses_active_project(self):
        bdir = self.make_backup_dir("backup-probe")
        # The active project name itself must carry the prefix here, so the
        # test reaches the active-project guard instead of the prefix guard.
        proc = self.run_tool(
            "opl-restore-test",
            args=(sh_path(bdir), "--disposable", "opl-restore-live"),
            extra={"OPL_PROJECT": "opl-restore-live"},
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("ACTIVE", proc.stderr)

    def test_restore_refuses_name_without_prefix(self):
        bdir = self.make_backup_dir("backup-probe")
        if os.path.isfile(self.log):
            os.unlink(self.log)
        proc = self.run_tool(
            "opl-restore-test", args=(sh_path(bdir), "--disposable", "other-project")
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("opl-restore-", proc.stdout + proc.stderr)
        self.assertFalse(os.path.isfile(self.log),
                         "refusal happened after docker calls")

    def test_restore_refuses_name_with_existing_containers(self):
        bdir = self.make_backup_dir("backup-probe")
        self.set_ps("c1")
        proc = self.run_tool(
            "opl-restore-test", args=(sh_path(bdir), "--disposable", "opl-restore-t9")
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("already in use", proc.stdout + proc.stderr)
        log = self.docker_log()
        self.assertNotIn(" up ", " ".join(log.splitlines()))

    def test_restore_refuses_host_path_mount(self):
        bdir = self.make_backup_dir("backup-probe")
        self.write_state(
            "compose-config.yml",
            "services:\n  db:\n    volumes:\n"
            "      - type: bind\n        source: /var/lib/postgresql/data\n"
            "        target: /var/lib/postgresql/data\n",
        )
        proc = self.run_tool(
            "opl-restore-test",
            args=(sh_path(bdir), "--disposable", "opl-restore-t1"),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("host path", proc.stdout + proc.stderr)

    def test_restore_refuses_foreign_volume(self):
        bdir = self.make_backup_dir("backup-probe")
        self.write_state(
            "compose-config.yml",
            "services:\n  db:\n    volumes:\n"
            "      - type: volume\n        source: live_pgdata\n"
            "        target: /var/lib/postgresql/data\n",
        )
        proc = self.run_tool(
            "opl-restore-test",
            args=(sh_path(bdir), "--disposable", "opl-restore-t1"),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("outside the throwaway project", proc.stdout + proc.stderr)

    def test_restore_proof_cleans_only_disposable(self):
        bdir = self.make_backup_dir("backup-probe", files=2)
        self.write_state("compose-config.yml", self.COMPOSE_CLEAN)
        proc = self.run_tool(
            "opl-restore-test",
            args=(sh_path(bdir), "--disposable", "opl-restore-t1"),
            extra={"FAKE_PSQL_COUNT": "7"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("RESTORE_PROOF=PASS", proc.stdout)
        self.assertIn("tables=7", proc.stdout)
        log = self.docker_log()
        down_lines = [ln for ln in log.splitlines() if " down " in ln]
        self.assertTrue(down_lines, "no compose down call was logged")
        for ln in down_lines:
            self.assertIn("opl-restore-t1", ln)
            self.assertNotIn(self.PROJECT, ln)


class RunbookTests(unittest.TestCase):
    REQUIRED = [
        "Start",
        "Stop",
        "Status",
        "Backup",
        "Restore",
        "Update",
        "disk",
        "Owner",
        "Lead",
        "worker",
        "127.0.0.1",
    ]

    def test_runbook_covers_owner_needs_in_one_page(self):
        path = os.path.join(REPO, "docs", "RUNBOOK.md")
        self.assertTrue(os.path.isfile(path), "docs/RUNBOOK.md is missing")
        content = read(path).lower()
        for word in self.REQUIRED:
            with self.subTest(word=word):
                self.assertIn(word.lower(), content)
        lines = content.splitlines()
        self.assertLessEqual(len(lines), 130, "runbook exceeds one page")


if __name__ == "__main__":
    unittest.main(verbosity=2)
