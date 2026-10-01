"""Per-project readiness check (issue #43): can every model really work here?

Read-only. For one configured project it prints PASS / FAIL / WARN / SKIP per
item and, for every FAIL or WARN, the exact fix (a click path or a command).
The conductor reuses `check_project` (issue #44) so a project that is not
ready never gets work started in it.

Everything the check touches is injected through `Env`, so tests run with the
same fakes as the rest of the toolkit: an OpenProject client, a GitHub client,
a launcher runner, `which` and a git runner. Secrets are never printed: the
only token-derived value ever shown is an expiry date.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field

from opl import runtimes
from opl.configure.common import _eid, find_project
from opl.openproject import ApiError

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"

#: Token expiry closer than this is a warning; already expired is a failure.
TOKEN_WARN_DAYS = 14
#: First launcher version with the no-model `probe` mode.
PROBE_VERSION = 3
#: The models every project needs as members; spark only on Public projects.
REQUIRED_MEMBERS = ("claude", "codex", "conductor")
_EXPIRY_HEADER = "github-authentication-token-expiration"
_VERSION_LINE = re.compile(r"^opl-spark-launch (\d+)\b")


@dataclass(frozen=True)
class Result:
    status: str
    item: str
    detail: str = ""
    fix: str = ""


def failed(results):
    """The FAIL results, in order."""
    return [r for r in results if r.status == FAIL]


@dataclass
class Env:
    """Everything a check may touch; the CLI builds the real one."""

    op: object                      # admin OpenProject client
    gh: object                      # GitHub client (conductor token)
    run_launcher: object            # (mode_args) -> (returncode, output)
    which: object = shutil.which    # (name) -> path or None
    git: object = None              # (args, cwd) -> (returncode, output)
    runtimes_dir: str = "/opt/opl-runtimes"
    now: object = field(default_factory=lambda: datetime.datetime.now(
        datetime.timezone.utc))
    run_id: object = field(default_factory=lambda: "probe-%d" % int(time.time()))


def real_git(args, cwd):
    proc = subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True,
                          text=True, timeout=120)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _short(text, limit=300):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


# -- OpenProject ------------------------------------------------------------

def _login(op, href, cache):
    if href not in cache:
        try:
            cache[href] = (op.get(href) or {}).get("login", "")
        except ApiError:
            cache[href] = ""
    return cache[href]


def check_openproject(env, project):
    results = []
    try:
        element = find_project(env.op, project.name)
    except ApiError as exc:
        return [Result(FAIL, "openproject: project exists", _short(exc),
                       "Create it: opl-configure --apply (the project must be "
                       "in opl.toml and the owner account must exist)")]
    results.append(Result(PASS, "openproject: project exists",
                          "id %s" % _eid(element)))
    href = ((element.get("_links") or {}).get("memberships") or {}).get("href")
    members = set()
    if href:
        cache = {}
        try:
            for membership in env.op.get_all(href):
                principal = ((membership.get("_links") or {})
                             .get("principal") or {}).get("href", "")
                if principal:
                    members.add(_login(env.op, principal, cache))
        except ApiError as exc:
            return results + [Result(FAIL, "openproject: members",
                                     _short(exc), "Check the admin token can "
                                     "read memberships")]
    needed = list(REQUIRED_MEMBERS)
    if project.visibility == "Public":
        needed.append("spark")
    missing = [login for login in needed if login not in members]
    if missing:
        results.append(Result(
            FAIL, "openproject: members", "missing: %s" % ", ".join(missing),
            "Run opl-configure --apply, or add them in OpenProject: Project "
            "> Members > + Member, role Model"))
    elif project.visibility == "Private" and "spark" in members:
        results.append(Result(
            FAIL, "openproject: members", "spark is a member of a Private project",
            "Remove spark: Project > Members > spark > Delete (Spark never "
            "works on Private projects)"))
    else:
        results.append(Result(PASS, "openproject: members",
                              ", ".join(needed)))
    return results


# -- GitHub -----------------------------------------------------------------

def _expiry(headers, now):
    raw = (headers or {}).get(_EXPIRY_HEADER, "").strip()
    if not raw:
        return Result(PASS, "github: token expiry", "no expiry set")
    text = raw.replace(" UTC", "").strip()
    try:
        when = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return Result(WARN, "github: token expiry",
                      "unreadable expiry header %r" % raw[:40])
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    days = (when - now).days
    fix = ("Renew it: GitHub > Settings > Developer settings > Personal access "
           "tokens, then update the conductor's token file")
    if days < 0:
        return Result(FAIL, "github: token expiry", "expired %s" % when.date(),
                      fix)
    if days < TOKEN_WARN_DAYS:
        return Result(WARN, "github: token expiry",
                      "expires %s (in %d days)" % (when.date(), days), fix)
    return Result(PASS, "github: token expiry",
                  "expires %s (in %d days)" % (when.date(), days))


def _workflow_on_pull_request(gh, base, workflows):
    for workflow in workflows:
        path = workflow.get("path", "")
        if not path:
            continue
        try:
            data = gh.get("%s/contents/%s" % (base, path)) or {}
            text = base64.b64decode(data.get("content", "")).decode(
                "utf-8", "replace")
        except (ApiError, ValueError):
            continue
        if re.search(r"(?m)^\s*(pull_request|pull_request_target)\b|"
                     r"\bon:\s*\[?[^\n]*\bpull_request\b", text):
            return path
    return None


def check_github(env, project):
    owner, repo = project.repo.split("/", 1)
    base = "/repos/%s/%s" % (owner, repo)
    results = []
    headers = {}
    try:
        data = env.gh.get(base, headers_out=headers) or {}
    except ApiError as exc:
        return [Result(FAIL, "github: repo readable", _short(exc),
                       "Give the conductor token access to %s (fine-grained "
                       "token: Repository access, Contents: Read and write)"
                       % project.repo)]
    results.append(Result(PASS, "github: repo readable", project.repo))
    branch = data.get("default_branch", "main")
    if (data.get("permissions") or {}).get("push"):
        results.append(Result(PASS, "github: write access",
                              "push permission (read from the API, nothing "
                              "was written)"))
    else:
        results.append(Result(
            FAIL, "github: write access", "the token cannot push",
            "Edit the token: Contents: Read and write, Pull requests: Read "
            "and write, Workflows: Read and write (workflow files)"))
    workflows = []
    try:
        listing = env.gh.get(base + "/actions/workflows") or {}
        workflows = listing.get("workflows", [])
        results.append(Result(PASS, "github: actions readable",
                              "%d workflow(s)" % len(workflows)))
    except ApiError as exc:
        results.append(Result(FAIL, "github: actions readable", _short(exc),
                              "Edit the token: Actions: Read"))
    for item, path, scope in (
            ("deployments", base + "/deployments?per_page=1",
             "Deployments: Read"),
            ("statuses", "%s/commits/%s/status" % (base, branch),
             "Commit statuses: Read")):
        try:
            env.gh.get(path.split("?")[0],
                       {"per_page": "1"} if "?" in path else None)
            results.append(Result(PASS, "github: %s readable" % item))
        except ApiError as exc:
            results.append(Result(FAIL, "github: %s readable" % item,
                                  _short(exc), "Edit the token: " + scope))
    required = None
    try:
        protection = env.gh.get("%s/branches/%s/protection" % (base, branch))
        required = ((protection or {}).get("required_status_checks") or {})
        results.append(Result(PASS, "github: branch protection readable",
                              "%s is protected" % branch))
    except ApiError as exc:
        if exc.status == 404:
            results.append(Result(
                WARN, "github: branch protection readable",
                "%s has no protection" % branch,
                "Optional: Settings > Branches > add a rule requiring the "
                "CI check before merge"))
        else:
            results.append(Result(
                FAIL, "github: branch protection readable", _short(exc),
                "Edit the token: Administration: Read"))
    has_required = bool(required and (required.get("checks")
                                      or required.get("contexts")))
    workflow = None if has_required else _workflow_on_pull_request(
        env.gh, base, workflows)
    if has_required:
        results.append(Result(PASS, "github: a PR check exists",
                              "required status checks"))
    elif workflow:
        results.append(Result(PASS, "github: a PR check exists",
                              "workflow %s runs on pull_request" % workflow))
    else:
        results.append(Result(
            FAIL, "github: a PR check exists",
            "no required check and no workflow on pull_request",
            "Add .github/workflows/ci.yml with `on: pull_request` that runs "
            "the project's lint and tests; the merge gate needs a check"))
    results.append(_expiry(headers, env.now))
    return results


# -- Spark sandbox (Public) -------------------------------------------------

def _launcher_version(env):
    try:
        code, out = env.run_launcher(["version"])
    except OSError as exc:
        return None, _short(exc)
    match = _VERSION_LINE.match((out or "").strip())
    if code != 0 or not match:
        return None, _short(out or "no output")
    return int(match.group(1)), ""


def check_sandbox(env, project):
    results = []
    repo_dir = project.local_repo
    if not repo_dir or not os.path.isdir(repo_dir):
        results.append(Result(
            FAIL, "sandbox: local checkout", "local_repo %r is missing" % repo_dir,
            "git clone https://github.com/%s.git <path> and set local_repo "
            "in opl.toml" % project.repo))
    else:
        git = env.git or real_git
        code, out = git(["status", "--porcelain"], repo_dir)
        if code != 0:
            results.append(Result(FAIL, "sandbox: local checkout", _short(out),
                                  "local_repo is not a git checkout"))
        elif out.strip():
            results.append(Result(
                FAIL, "sandbox: local checkout is clean",
                "uncommitted changes", "git -C %s status, then commit, "
                "stash outside the toolkit or discard them" % repo_dir))
        else:
            code, out = git(["fetch", "--dry-run"], repo_dir)
            if code == 0:
                results.append(Result(PASS, "sandbox: local checkout",
                                      "exists, clean, fetchable"))
            else:
                results.append(Result(
                    FAIL, "sandbox: local checkout is fetchable", _short(out),
                    "Check the network and `git -C %s remote -v`" % repo_dir))
    version, why = _launcher_version(env)
    declared = bool(project.setup or project.test or project.runtime)
    if version is None:
        results.append(Result(
            FAIL, "sandbox: launcher", why,
            "Install the launcher as root (docs/SANDBOX.md, Owner steps) and "
            "check `sudo -n -u opl-worker /usr/local/libexec/opl-spark-launch "
            "version`"))
        return results
    results.append(Result(PASS, "sandbox: launcher", "version %d" % version))
    runtime_args = []
    runtime_missing = False
    for spec in project.runtime:
        try:
            name, _prefix = runtimes.parse_spec(spec)
            found = runtimes.find_installed(env.runtimes_dir, spec)
        except runtimes.RuntimeFailure as exc:
            results.append(Result(FAIL, "sandbox: runtime " + spec, str(exc),
                                  "Use the form name@version, e.g. node@22"))
            runtime_missing = True
            continue
        if found:
            results.append(Result(PASS, "sandbox: runtime " + spec,
                                  "%s %s installed" % (name, found)))
            runtime_args += ["--runtime", "%s@%s" % (name, found)]
        else:
            results.append(Result(
                WARN, "sandbox: runtime " + spec, "not installed yet",
                "The conductor installs it before the first run; first make "
                "sure %s exists and is owned by the conductor user (docs/"
                "SANDBOX.md), then re-run this check" % env.runtimes_dir))
            runtime_missing = True
    if not project.test:
        results.append(Result(
            WARN, "sandbox: smoke run", "no `test` declared",
            "Declare the project's test command: test = \"...\" under "
            "[[project]] in opl.toml, so Spark proves its work"))
    elif version < PROBE_VERSION:
        results.append(Result(
            WARN, "sandbox: smoke run", "launcher %d has no probe mode" % version,
            "Reinstall the launcher from sandbox/opl-spark-launch (docs/"
            "SANDBOX.md, Owner steps)"))
    elif runtime_missing:
        results.append(Result(SKIP, "sandbox: smoke run",
                              "declared runtime not installed yet"))
    else:
        args = ["probe", "--run", env.run_id, "--repo", project.repo]
        args += runtime_args
        if project.setup:
            args += ["--setup-b64", _b64(project.setup)]
        args += ["--test-b64", _b64(project.test)]
        try:
            code, out = env.run_launcher(args)
        except OSError as exc:
            code, out = 255, str(exc)
        if code == 0:
            results.append(Result(PASS, "sandbox: smoke run",
                                  "setup and test pass in a fresh clone as "
                                  "the worker"))
        else:
            tail = " | ".join(line for line in (out or "").splitlines()
                              if line.strip())[-300:]
            results.append(Result(
                FAIL, "sandbox: smoke run", "exit %s: %s" % (code, tail),
                "Make `%s` pass on a clean clone of %s (the output above says "
                "why), or fix the declared setup/test in opl.toml"
                % (project.test, project.repo)))
    if not declared and version is not None:
        results.append(Result(
            WARN, "sandbox: readiness declared", "no runtime/setup/test",
            "Declare runtime, setup and test under [[project]] in opl.toml "
            "(docs/CONFIGURE.md) so Spark can verify its work"))
    return results


# -- Claude / Codex (Private) -----------------------------------------------

def check_private(env, project):
    results = []
    repo_dir = project.local_repo
    if repo_dir and os.path.isdir(repo_dir):
        results.append(Result(PASS, "checkout: local checkout", repo_dir))
    else:
        results.append(Result(
            FAIL, "checkout: local checkout",
            "local_repo %r is missing" % repo_dir,
            "git clone the repository and set local_repo in opl.toml"))
    for spec in project.runtime:
        try:
            name, _prefix = runtimes.parse_spec(spec)
        except runtimes.RuntimeFailure as exc:
            results.append(Result(FAIL, "checkout: runtime " + spec, str(exc),
                                  "Use the form name@version, e.g. node@22"))
            continue
        if env.which(name):
            results.append(Result(PASS, "checkout: runtime " + spec,
                                  "%s found on PATH" % name))
        else:
            results.append(Result(
                FAIL, "checkout: runtime " + spec, "%s not on PATH" % name,
                "Install %s for the account that runs Claude/Codex (the "
                "toolkit never installs software on a private machine)" % name))
    return results


def check_project(settings, project, env):
    """All results for one project; never raises ApiError."""
    results = check_openproject(env, project)
    results += check_github(env, project)
    if project.visibility == "Public":
        results += check_sandbox(env, project)
    else:
        results += check_private(env, project)
    return results


# -- CLI --------------------------------------------------------------------

def render(key, results):
    lines = ["== %s" % key]
    for result in results:
        line = "%-4s %s" % (result.status, result.item)
        if result.detail:
            line += ": " + result.detail
        lines.append(line)
        if result.status in (FAIL, WARN) and result.fix:
            lines.append("       fix: " + result.fix)
    return "\n".join(lines)


def build_env(settings, op=None, gh=None):
    """The real Env; the conductor passes its own clients (#44)."""
    from opl.conductor.spark import sandbox_run
    from opl.github import GitHub
    from opl.openproject import Client

    def run_launcher(mode_args):
        argv = sandbox_run.sudo_argv(
            "sudo", sandbox_run.DEFAULT_WORKER, sandbox_run.DEFAULT_LAUNCHER,
            mode_args)
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=2400)
        return proc.returncode, proc.stdout + proc.stderr

    return Env(
        op=op or Client(settings.openproject.url, settings.token("admin")),
        gh=gh or GitHub(settings.token("github")),
        run_launcher=run_launcher,
        runtimes_dir=settings.conductor.runtimes_dir)


def main(argv=None, env_factory=build_env, load=None, out=sys.stdout):
    parser = argparse.ArgumentParser(prog="opl-project-check")
    parser.add_argument("key", nargs="?", help="project key from opl.toml")
    parser.add_argument("--all", action="store_true",
                        help="check every configured project")
    args = parser.parse_args(argv)
    if bool(args.key) == bool(args.all):
        parser.error("give a project key or --all")
    from opl.settings import SettingsError, load as load_settings

    try:
        settings = (load or load_settings)()
        env = env_factory(settings)
    except SettingsError as exc:
        print("opl-project-check: %s" % exc, file=sys.stderr)
        return 2
    projects = [p for p in settings.projects
                if args.all or p.key == args.key]
    if not projects:
        print("opl-project-check: no such project %r" % args.key,
              file=sys.stderr)
        return 2
    bad = False
    for project in projects:
        results = check_project(settings, project, env)
        print(render(project.key, results), file=out)
        bad = bad or bool(failed(results))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
