#!/usr/bin/env python3
"""Tests for bin/opl-conductor-start (start / status / stop / watchdog).

Synthetic only: the script runs from a temporary copy of the toolkit whose
bin/opl-conductor is a fake that takes the real instance lock and records
what it received. OpenProject is a local health endpoint. Linux/WSL only
(the script needs setsid and flock); skipped elsewhere. Standard library
only.
"""

import http.server
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASH = shutil.which("bash")
LINUX_TOOLS = os.name == "posix" and all(
    shutil.which(t) for t in ("bash", "setsid", "flock"))

SECRET = "synthetic-conductor-key-abc123"

FAKE_CONDUCTOR = r'''#!/usr/bin/env bash
exec python3 - "$@" <<'PY'
import fcntl, os, signal, sys, time, tomllib
with open(os.path.join(os.environ["OPL_CONFIG_DIR"], "opl.toml"), "rb") as f:
    state = tomllib.load(f)["conductor"]["state_dir"]
fh = open(os.path.join(state, "conductor.lock"), "a+")
fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
fh.truncate(0)
fh.write("%d\n" % os.getpid())
fh.flush()
print("fake-conductor args=%s admin=%s conductor=%s path0=%s" % (
    " ".join(sys.argv[1:]), "OPL_TOKEN_ADMIN" in os.environ,
    bool(os.environ.get("OPL_TOKEN_CONDUCTOR")),
    os.environ["PATH"].split(":")[0]), flush=True)
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
deadline = time.time() + 60
while time.time() < deadline:
    time.sleep(0.2)
PY
'''

TOML = """
[openproject]
url = "%s"
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
state_dir = "%s"

[runner]
command = ["example-worker"]
max_parallel = 2

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
"""


class _Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.path == "/health_checks/default"
        self.send_response(200 if ok else 404)
        self.end_headers()
        self.wfile.write(b"ok" if ok else b"no")

    def log_message(self, *args):
        pass


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(LINUX_TOOLS, "needs Linux bash, setsid and flock")
class ConductorStartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opl-cstart-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        root = os.path.join(self.tmp, "toolkit")
        os.makedirs(os.path.join(root, "bin"))
        shutil.copytree(os.path.join(REPO, "lib"), os.path.join(root, "lib"))
        shutil.copytree(os.path.join(REPO, "opl"), os.path.join(root, "opl"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.script = os.path.join(root, "bin", "opl-conductor-start")
        shutil.copy(os.path.join(REPO, "bin", "opl-conductor-start"), self.script)
        fake = os.path.join(root, "bin", "opl-conductor")
        with open(fake, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(FAKE_CONDUCTOR)
        for path in (self.script, fake):
            os.chmod(path, 0o755)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Health)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        self.home = os.path.join(self.tmp, "home")
        self.config = os.path.join(self.home, ".config", "opl")
        os.makedirs(self.config)
        self.state = os.path.join(self.tmp, "state")
        self.write_config("http://127.0.0.1:%d" % self.server.server_address[1])
        self.env_file = os.path.join(self.config, "tokens.env")
        self.write_env("# keys\n\nexport OPL_TOKEN_CONDUCTOR=%s\n" % SECRET)
        self.addCleanup(self.run_script, "stop")

    def write_config(self, url):
        with open(os.path.join(self.config, "opl.toml"), "w", encoding="utf-8") as fh:
            fh.write(TOML % (url, self.state))

    def write_env(self, text, mode=0o600):
        with open(self.env_file, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.chmod(self.env_file, mode)

    def run_script(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith("OPL_")}
        env.update(HOME=self.home, OPL_CONFIG_DIR=self.config,
                   OPL_TOKEN_ADMIN="synthetic-admin-key-must-vanish")
        proc = subprocess.run([BASH, self.script] + list(args), env=env,
                              capture_output=True, text=True, timeout=120)
        self.assertNotIn(SECRET, proc.stdout + proc.stderr)
        return proc

    def log(self):
        path = os.path.join(self.state, "conductor.out")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_start_status_stop(self):
        started = self.run_script("start", "--live", "--path", "~/nodebin")
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertIn("conductor running", started.stdout)
        self.assertIn(
            "args=--live admin=False conductor=True path0=%s/nodebin" % self.home,
            self.log())
        self.assertNotIn(SECRET, self.log())

        status = self.run_script("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("conductor running", status.stdout)
        self.assertIn("OpenProject healthy", status.stdout)

        again = self.run_script("start", "--live")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("already running", again.stdout)
        self.assertEqual(self.log().count("--- start"), 1)

        stopped = self.run_script("stop")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertEqual(self.run_script("status").returncode, 1)

    def write_heartbeat(self, when):
        with open(os.path.join(self.state, "conductor.heartbeat"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write(when.isoformat() + "\n")

    def lock_pid(self):
        with open(os.path.join(self.state, "conductor.lock"),
                  encoding="utf-8") as fh:
            return fh.read().strip().splitlines()[0]

    def test_status_reports_loop_age(self):
        started = self.run_script("start")
        self.assertEqual(started.returncode, 0, started.stderr)
        status = self.run_script("status")
        self.assertIn("conductor running", status.stdout)
        # The fake conductor never cycles: no heartbeat yet, said plainly.
        self.assertIn("none yet", status.stdout)

        self.write_heartbeat(datetime.now(timezone.utc))
        self.assertIn("loop last ran",
                      self.run_script("status").stdout)

        self.write_heartbeat(datetime.now(timezone.utc) - timedelta(hours=2))
        stale = self.run_script("status")
        self.assertIn("loop last ran", stale.stdout)
        self.assertIn("STALE", stale.stdout)
        # A stale loop still holds the lock: the exit code is unchanged.
        self.assertEqual(stale.returncode, 0, stale.stderr)

    def test_watchdog_leaves_a_healthy_loop_alone(self):
        started = self.run_script("start")
        self.assertEqual(started.returncode, 0, started.stderr)
        pid = self.lock_pid()
        self.write_heartbeat(datetime.now(timezone.utc))
        watched = self.run_script("watchdog")
        self.assertEqual(watched.returncode, 0, watched.stderr)
        self.assertIn("healthy", watched.stdout)
        self.assertEqual(self.lock_pid(), pid)
        self.assertEqual(self.log().count("--- start"), 1)

    def test_watchdog_restarts_a_stale_loop(self):
        started = self.run_script("start")
        self.assertEqual(started.returncode, 0, started.stderr)
        old_pid = self.lock_pid()
        # Two silent hours: the issue's signature (lock held, no cycles).
        self.write_heartbeat(datetime.now(timezone.utc) - timedelta(hours=2))
        watched = self.run_script("watchdog")
        self.assertEqual(watched.returncode, 0, watched.stderr)
        self.assertIn("stall detected", watched.stderr)
        self.assertIn("conductor running", watched.stdout)
        self.assertNotEqual(self.lock_pid(), old_pid)
        self.assertEqual(self.log().count("--- start"), 2)

    def test_watchdog_starts_when_not_running(self):
        watched = self.run_script("watchdog")
        self.assertEqual(watched.returncode, 0, watched.stderr)
        self.assertIn("starting it", watched.stderr)
        self.assertIn("conductor running", watched.stdout)

    def test_key_file_is_never_executed(self):
        marker = os.path.join(self.tmp, "pwned")
        self.write_env("export OPL_TOKEN_CONDUCTOR=$(touch %s)\n" % marker)
        proc = self.run_script("start")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("nothing loaded", proc.stderr)
        self.assertFalse(os.path.exists(marker))
        self.assertNotIn("--- start", self.log())

    def test_refuses_admin_key_in_file(self):
        self.write_env("export OPL_TOKEN_ADMIN=synthetic-admin\n")
        proc = self.run_script("start")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("admin key", proc.stderr)
        self.assertNotIn("--- start", self.log())

    def test_refuses_key_file_readable_by_others(self):
        self.write_env("export OPL_TOKEN_CONDUCTOR=%s\n" % SECRET, mode=0o644)
        proc = self.run_script("start")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("readable by other users", proc.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(self.env_file).st_mode), 0o644)

    def test_waits_for_openproject_then_gives_up(self):
        self.write_config("http://127.0.0.1:%d" % free_port())
        proc = self.run_script("start", "--health-timeout", "1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not healthy", proc.stderr)
        self.assertNotIn("--- start", self.log())


if __name__ == "__main__":
    unittest.main()
