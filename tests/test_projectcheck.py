#!/usr/bin/env python3
"""opl-project-check (#43): every item with fakes; nothing live, nothing printed secret."""

import base64
import datetime
import io
import os
import tempfile
import unittest

from opl import projectcheck as pc
from opl.openproject import ApiError
from opl.settings import Project

NOW = datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc)
WORKFLOW = base64.b64encode(b"name: ci\non:\n  pull_request:\n").decode()


class FakeOp:
    def __init__(self, members=("claude", "codex", "conductor", "spark"),
                 exists=True):
        self.members = members
        self.exists = exists

    def get_all(self, path, params=None):
        if path == "/api/v3/projects":
            if not self.exists:
                return []
            return [{"id": 7, "name": "Demo", "_links": {
                "memberships": {"href": "/api/v3/memberships?project=7"}}}]
        if path.startswith("/api/v3/memberships"):
            return [{"_links": {"principal": {"href": "/api/v3/users/%s" % m}}}
                    for m in self.members]
        raise ApiError(404, path, "unexpected")

    def get(self, path, params=None):
        if path.startswith("/api/v3/users/"):
            return {"login": path.rsplit("/", 1)[1]}
        raise ApiError(404, path, "unexpected")


class FakeGh:
    def __init__(self, push=True, expiry="2026-12-31 00:00:00 UTC",
                 protection=None, deny=(), workflow=True):
        self.push = push
        self.expiry = expiry
        self.protection = protection
        self.deny = deny
        self.workflow = workflow

    def get(self, path, params=None, headers_out=None):
        for fragment in self.deny:
            if fragment in path:
                raise ApiError(403, path, "forbidden")
        base = "/repos/o/r"
        if path == base:
            if headers_out is not None and self.expiry:
                headers_out["github-authentication-token-expiration"] = self.expiry
            return {"default_branch": "main", "permissions": {"push": self.push}}
        if path == base + "/actions/workflows":
            return {"workflows": [{"path": ".github/workflows/ci.yml"}]
                    if self.workflow else []}
        if path == base + "/contents/.github/workflows/ci.yml":
            return {"content": WORKFLOW}
        if path == base + "/deployments":
            return []
        if path == base + "/commits/main/status":
            return {"state": "success"}
        if path == base + "/branches/main/protection":
            if self.protection is None:
                raise ApiError(404, path, "Branch not protected")
            return self.protection
        raise ApiError(404, path, "unexpected " + path)


class Launcher:
    def __init__(self, version=3, probe_rc=0, probe_out="probe ok"):
        self.version = version
        self.probe_rc = probe_rc
        self.probe_out = probe_out
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "version":
            if self.version is None:
                return 1, "sudo: a password is required"
            return 0, "opl-spark-launch %d runtimes setup probe" % self.version
        return self.probe_rc, self.probe_out


def project(**kw):
    base = dict(key="demo", name="Demo", repo="o/r", visibility="Public",
                has_test_env=False, local_repo="", test="npm test",
                setup="npm ci")
    base.update(kw)
    return Project(**base)


def env(tmp, op=None, gh=None, launcher=None, git_out=(0, ""), which=None,
        runtimes_dir=None):
    return pc.Env(op=op or FakeOp(), gh=gh or FakeGh(),
                  run_launcher=launcher or Launcher(),
                  which=which or (lambda name: "/usr/bin/" + name),
                  git=lambda args, cwd: git_out
                  if args[0] != "fetch" else (0, ""),
                  runtimes_dir=runtimes_dir or tmp, now=NOW,
                  run_id="probe-test")


def by_item(results):
    return {r.item: r for r in results}


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opl-pc-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)

    def run_all(self, proj=None, **kw):
        proj = proj or project(local_repo=self.repo)
        return by_item(pc.check_project(None, proj, env(self.tmp, **kw)))

    def test_ready_public_project_passes_everything(self):
        results = pc.check_project(None, project(local_repo=self.repo),
                                   env(self.tmp))
        self.assertEqual([r for r in results if r.status == pc.FAIL], [])
        items = by_item(results)
        self.assertEqual(items["sandbox: smoke run"].status, pc.PASS)
        self.assertEqual(items["github: token expiry"].status, pc.PASS)

    def test_missing_project_is_a_failure_with_a_fix(self):
        results = pc.check_project(None, project(local_repo=self.repo),
                                   env(self.tmp, op=FakeOp(exists=False)))
        first = results[0]
        self.assertEqual(first.status, pc.FAIL)
        self.assertIn("opl-configure", first.fix)

    def test_missing_member_named_and_spark_only_for_public(self):
        items = self.run_all(op=FakeOp(members=("claude", "conductor")))
        self.assertIn("codex", items["openproject: members"].detail)
        self.assertIn("spark", items["openproject: members"].detail)
        private = by_item(pc.check_project(
            None, project(visibility="Private", local_repo=self.repo),
            env(self.tmp, op=FakeOp(members=("claude", "codex", "conductor")))))
        self.assertEqual(private["openproject: members"].status, pc.PASS)

    def test_spark_member_of_private_project_fails(self):
        items = by_item(pc.check_project(
            None, project(visibility="Private", local_repo=self.repo),
            env(self.tmp)))
        self.assertEqual(items["openproject: members"].status, pc.FAIL)

    def test_no_push_permission_fails(self):
        items = self.run_all(gh=FakeGh(push=False))
        self.assertEqual(items["github: write access"].status, pc.FAIL)
        self.assertIn("Contents", items["github: write access"].fix)

    def test_unreadable_pieces_fail_with_token_scope(self):
        items = self.run_all(gh=FakeGh(deny=("/actions/workflows",
                                             "/deployments", "/status",
                                             "/protection")))
        for name in ("github: actions readable", "github: deployments readable",
                     "github: statuses readable",
                     "github: branch protection readable"):
            self.assertEqual(items[name].status, pc.FAIL, name)
            self.assertIn("Edit the token", items[name].fix)

    def test_no_protection_is_only_a_warning(self):
        items = self.run_all()
        self.assertEqual(items["github: branch protection readable"].status,
                         pc.WARN)

    def test_no_pr_check_fails(self):
        items = self.run_all(gh=FakeGh(workflow=False))
        self.assertEqual(items["github: a PR check exists"].status, pc.FAIL)
        required = self.run_all(gh=FakeGh(
            workflow=False,
            protection={"required_status_checks": {"contexts": ["ci"]}}))
        self.assertEqual(required["github: a PR check exists"].status, pc.PASS)

    def test_token_expiry_warn_fail_pass(self):
        for expiry, status in (("2026-10-10 00:00:00 UTC", pc.WARN),
                               ("2026-09-01 00:00:00 UTC", pc.FAIL),
                               ("2027-03-01 00:00:00 UTC", pc.PASS),
                               (None, pc.PASS)):
            with self.subTest(expiry=expiry):
                items = self.run_all(gh=FakeGh(expiry=expiry))
                self.assertEqual(items["github: token expiry"].status, status)

    def test_dirty_or_missing_checkout_fails(self):
        items = self.run_all(git_out=(0, " M file.txt"))
        self.assertEqual(items["sandbox: local checkout is clean"].status,
                         pc.FAIL)
        missing = by_item(pc.check_project(
            None, project(local_repo=os.path.join(self.tmp, "nope")),
            env(self.tmp)))
        self.assertEqual(missing["sandbox: local checkout"].status, pc.FAIL)

    def test_launcher_unavailable_fails_with_install_steps(self):
        items = self.run_all(launcher=Launcher(version=None))
        self.assertEqual(items["sandbox: launcher"].status, pc.FAIL)
        self.assertIn("SANDBOX.md", items["sandbox: launcher"].fix)

    def test_first_launcher_without_version_mode_is_old_not_broken(self):
        class V1(Launcher):
            def __call__(self, args):
                self.calls.append(args)
                return 2, "opl-spark-launch: bad run id"

        undeclared = self.run_all(proj=project(local_repo=self.repo, test="",
                                               setup=""), launcher=V1())
        self.assertEqual(undeclared["sandbox: launcher"].status, pc.WARN)
        declared = self.run_all(launcher=V1())
        self.assertEqual(declared["sandbox: launcher"].status, pc.FAIL)
        self.assertIn("SANDBOX.md", declared["sandbox: launcher"].fix)

    def test_old_launcher_warns_and_skips_the_probe(self):
        launcher = Launcher(version=2)
        items = self.run_all(launcher=launcher)
        self.assertEqual(items["sandbox: smoke run"].status, pc.WARN)
        self.assertFalse(any(c[0] == "probe" for c in launcher.calls))

    def test_smoke_run_passes_the_declared_commands_to_the_probe(self):
        os.makedirs(os.path.join(self.tmp, "node", "22.17.0"))
        open(os.path.join(self.tmp, "node", "22.17.0", ".opl-installed"),
             "w").close()
        launcher = Launcher()
        items = self.run_all(proj=project(local_repo=self.repo,
                                          runtime=("node@22",)),
                             launcher=launcher)
        probe = [c for c in launcher.calls if c[0] == "probe"][0]
        self.assertIn("node@22.17.0", probe)
        self.assertEqual(base64.b64decode(
            probe[probe.index("--setup-b64") + 1]), b"npm ci")
        self.assertEqual(base64.b64decode(
            probe[probe.index("--test-b64") + 1]), b"npm test")
        self.assertEqual(items["sandbox: runtime node@22"].status, pc.PASS)

    def test_failed_smoke_run_shows_output_and_fix(self):
        items = self.run_all(launcher=Launcher(
            probe_rc=4, probe_out="[test] failed with exit 1\nlint error"))
        smoke = items["sandbox: smoke run"]
        self.assertEqual(smoke.status, pc.FAIL)
        self.assertIn("lint error", smoke.detail)
        self.assertIn("npm test", smoke.fix)

    def test_runtime_not_installed_warns_and_skips_smoke(self):
        items = self.run_all(proj=project(local_repo=self.repo,
                                          runtime=("node@22",)))
        self.assertEqual(items["sandbox: runtime node@22"].status, pc.WARN)
        self.assertEqual(items["sandbox: smoke run"].status, pc.SKIP)

    def test_undeclared_test_warns(self):
        items = self.run_all(proj=project(local_repo=self.repo, test="",
                                          setup=""))
        self.assertEqual(items["sandbox: smoke run"].status, pc.WARN)
        self.assertEqual(items["sandbox: readiness declared"].status, pc.WARN)

    def test_private_project_checks_checkout_and_runtime_on_path(self):
        proj = project(visibility="Private", local_repo=self.repo,
                       runtime=("node@22",))
        ok = by_item(pc.check_project(
            None, proj, env(self.tmp, op=FakeOp(members=(
                "claude", "codex", "conductor")))))
        self.assertEqual(ok["checkout: runtime node@22"].status, pc.PASS)
        self.assertNotIn("sandbox: launcher", ok)
        bad = by_item(pc.check_project(
            None, proj, env(self.tmp, which=lambda name: None,
                            op=FakeOp(members=("claude", "codex",
                                               "conductor")))))
        self.assertEqual(bad["checkout: runtime node@22"].status, pc.FAIL)

    def test_render_prints_fix_for_failures_only(self):
        text = pc.render("demo", [
            pc.Result(pc.PASS, "a", fix="never shown"),
            pc.Result(pc.FAIL, "b", "broken", "do this")])
        self.assertIn("PASS a", text)
        self.assertIn("FAIL b: broken", text)
        self.assertIn("fix: do this", text)
        self.assertNotIn("never shown", text)


class CliTests(unittest.TestCase):
    def run_cli(self, argv, gh=None):
        tmp = tempfile.mkdtemp(prefix="opl-pc-")

        class S:
            projects = [project(local_repo=tmp)]

        out = io.StringIO()
        code = pc.main(argv, env_factory=lambda s: env(tmp, gh=gh),
                       load=lambda: S, out=out)
        return code, out.getvalue()

    def test_exit_zero_when_nothing_fails(self):
        code, text = self.run_cli(["demo"])
        self.assertEqual(code, 0, text)
        self.assertIn("== demo", text)

    def test_exit_one_when_anything_fails(self):
        code, text = self.run_cli(["--all"], gh=FakeGh(push=False))
        self.assertEqual(code, 1)
        self.assertIn("FAIL github: write access", text)

    def test_unknown_project_is_exit_two(self):
        code, _text = self.run_cli(["nope"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
