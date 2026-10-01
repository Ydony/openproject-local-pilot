"""External config: $OPL_CONFIG_DIR/opl.toml plus secret environment variables.

Standard library only (needs Python 3.11+ for tomllib). Token *values* never
appear here: the config names environment variables, and this module reads
them at use time. Nothing in this module ever prints or logs a value.
"""

from __future__ import annotations

import datetime
import os
import re
import tomllib
from dataclasses import dataclass, field


class SettingsError(Exception):
    """The external config is missing or malformed."""


class MissingSecret(SettingsError):
    """A required secret's environment variable is not set.

    Carries only the variable *name*, never the value.
    """

    def __init__(self, var_name):
        super().__init__(
            "secret environment variable %r is not set" % (var_name,)
        )
        self.var_name = var_name


@dataclass(frozen=True)
class OpenProject:
    url: str
    admin_token_env: str
    owner_login: str


@dataclass(frozen=True)
class Conductor:
    live: bool
    interval_seconds: int
    state_dir: str
    # Shared directory the conductor installs project runtimes into and the
    # unprivileged worker reads (issue #45); see docs/SANDBOX.md (Runtimes).
    runtimes_dir: str = "/opt/opl-runtimes"
    # Stop work in a project whose readiness check fails (issue #44).
    # On by default; false is for synthetic test setups only.
    readiness_gate: bool = True


@dataclass(frozen=True)
class Runner:
    command: tuple
    max_parallel: int
    limits_minutes: dict
    # Env var NAMES passed through to worker processes (the worker's own API
    # key). Everything else is withheld; see supervisor.worker_environment.
    worker_env: tuple = ()


@dataclass(frozen=True)
class Signal:
    kind: str
    name: str


@dataclass(frozen=True)
class Project:
    key: str
    name: str
    repo: str
    visibility: str
    has_test_env: bool
    test_url: str = ""
    test_signal: object = None
    prod_signal: object = None
    # Local checkout the Spark runner works in (T4.3). Empty means the
    # project has no local repo configured.
    local_repo: str = ""
    # Ref the runner bases worktrees on (local: branch, origin/<branch> or
    # SHA). Never sent to GitHub.
    base_ref: str = "main"
    # GitHub branch new PRs target (TH.11). A plain branch name: never a
    # remote-tracking ref like origin/main.
    pr_base: str = "main"
    # Optional build/test readiness declaration (issue #42). Empty means
    # undeclared: the project simply has no declared runtime needs.
    # `runtime` is a list of runtime specs (e.g. ["node@22"]); unknown
    # entries are allowed but reported via unknown_runtimes(). `setup`
    # and `test` are shell commands run from the project checkout
    # (e.g. "npm ci" / "npm test"); empty means undeclared.
    runtime: tuple = ()
    setup: str = ""
    test: str = ""
    # Temporary autonomy (issue #55): until `autonomy_until` (a date,
    # inclusive) the model `autonomy_lead` acts for the owner on this
    # project: its Merge OK counts as the owner's. Empty/None = off.
    autonomy_lead: str = ""
    autonomy_until: object = None


# Runtimes the toolkit knows how to provide. Unknown entries are allowed
# (owner laptops and future stacks) but reported — via unknown_runtimes()
# and in the configure/conductor docs — so a missing Node.js (or similar
# gap) surfaces instead of failing silently late in a run.
KNOWN_RUNTIMES = frozenset(
    {
        "node@22",
        "node@20",
        "python@3.11",
        "python@3.12",
        "python@3.13",
    }
)


def autonomy_active(project, today):
    """The acting lead login while the project's autonomy is active, else ""."""
    until = getattr(project, "autonomy_until", None)
    lead = getattr(project, "autonomy_lead", "")
    if not lead or until is None or today > until:
        return ""
    return lead


def _opt_autonomy(p, where):
    """Optional [project.autonomy] table -> (lead, until date)."""
    raw = p.get("autonomy")
    if raw is None:
        return "", None
    if not isinstance(raw, dict):
        raise SettingsError("%s: autonomy must be a table {lead, until}" % where)
    lead = raw.get("lead")
    if not isinstance(lead, str) or not lead.strip():
        raise SettingsError("%s: autonomy.lead must be a model login, e.g. \"claude\"" % where)
    if lead.strip() == "spark":
        raise SettingsError("%s: autonomy.lead may not be spark (a worker, not a lead)" % where)
    until = raw.get("until")
    if isinstance(until, str):
        try:
            until = datetime.date.fromisoformat(until)
        except ValueError:
            until = None
    if isinstance(until, datetime.datetime):
        until = until.date()
    if not isinstance(until, datetime.date):
        raise SettingsError("%s: autonomy.until must be a date, e.g. 2026-10-14 (required)" % where)
    return lead.strip(), until


def unknown_runtimes(project):
    """Runtime entries not in KNOWN_RUNTIMES.

    Tolerated by the loader (never a SettingsError); callers report them
    so readiness gaps are visible. Returns a tuple, possibly empty.
    """
    return tuple(r for r in (project.runtime or ()) if r not in KNOWN_RUNTIMES)


# A GitHub PR base is a plain branch name. Remote-tracking refs
# (origin/main), HEAD, and anything outside ref-name syntax are refused so
# a local start ref can never leak into a PR base (TH.11, R14).
_PR_BASE_RE = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")


def valid_pr_base(name):
    """True for a plain GitHub branch name, False for refs and junk."""
    if not isinstance(name, str) or not name:
        return False
    if name in ("HEAD",) or name.startswith("origin/"):
        return False
    if name[:1] in ("-", ".", "/"):
        return False
    if ".." in name or "@{" in name or name.endswith(".lock"):
        return False
    return bool(_PR_BASE_RE.match(name))


def _validated_pr_base(p, where):
    """pr_base from config, defaulting to main; rejects remote refs."""
    value = _opt_str(p, "pr_base", where) or "main"
    if not valid_pr_base(value):
        raise SettingsError(
            "%s: pr_base must be a plain GitHub branch name, got %r "
            "(a local start ref like origin/main is not a PR base)"
            % (where, value)
        )
    return value


TOKEN_WHO = ("admin", "claude", "codex", "spark", "conductor", "github")

# Every runner limit the conductor needs. Extra keys are tolerated, but these
# six must exist so a typo'd config fails fast instead of mid-run.
REQUIRED_LIMITS = ("S", "M", "L", "review", "test", "stall")


@dataclass(frozen=True)
class Settings:
    openproject: OpenProject
    tokens: dict
    github_token_env: str
    users_email_domain: str
    conductor: Conductor
    runner: Runner
    projects: tuple
    # Optional [permcheck] table (project sandbox for T2.3). Unknown keys
    # inside are tolerated; permcheck.py validates what it needs.
    permcheck: dict = field(default_factory=dict)

    def _secret(self, var_name):
        value = os.environ.get(var_name, "")
        if not value:
            raise MissingSecret(var_name)
        return value

    def token(self, who):
        """Return the secret for `who` (never logged by callers)."""
        if who == "admin":
            return self._secret(self.openproject.admin_token_env)
        if who == "github":
            return self._secret(self.github_token_env)
        if who in self.tokens:
            return self._secret(self.tokens[who])
        raise SettingsError("unknown token owner %r" % (who,))


def _req(mapping, key, where):
    if not isinstance(mapping, dict) or key not in mapping:
        raise SettingsError("%s: missing required key %r" % (where, key))
    return mapping[key]


def _opt_gate(cond):
    value = cond.get("readiness_gate", True)
    if not isinstance(value, bool):
        raise SettingsError("conductor: readiness_gate must be true or false")
    return value


def _opt_runtimes_dir(cond):
    value = cond.get("runtimes_dir", "/opt/opl-runtimes")
    if not isinstance(value, str) or not value.startswith("/") or ".." in value:
        raise SettingsError("conductor: runtimes_dir must be an absolute path")
    return value


def _opt_str(mapping, key, where):
    value = mapping.get(key, "")
    if not isinstance(value, str):
        raise SettingsError("%s: %r must be a string" % (where, key))
    return value


def _opt_runtime(p, where):
    """Optional `runtime` list (e.g. ["node@22]); absent means ()."""
    if "runtime" not in p:
        return ()
    value = p["runtime"]
    if not isinstance(value, list) or not all(
        isinstance(r, str) and r.strip() for r in value
    ):
        raise SettingsError(
            "%s: %r must be a list of non-empty strings, e.g. [\"node@22\"]"
            % (where, "runtime")
        )
    return tuple(value)


def _opt_command(p, key, where):
    """Optional per-project shell command (`setup`/`test`); absent means "".

    Must be a string when present (e.g. setup = "npm ci"). Empty means
    undeclared, exactly like absent.
    """
    if key not in p:
        return ""
    value = p[key]
    if not isinstance(value, str):
        raise SettingsError(
            "%s: %r must be a string command, e.g. \"npm ci\"" % (where, key)
        )
    return value


def _signal(raw, where):
    if not isinstance(raw, dict):
        raise SettingsError("%s: signal must be a {kind, name} table" % where)
    kind = _req(raw, "kind", where)
    name = _req(raw, "name", where)
    if kind not in ("workflow", "environment"):
        raise SettingsError(
            "%s: signal kind must be \"workflow\" or \"environment\", got %r"
            % (where, kind)
        )
    return Signal(kind=kind, name=name)


def _settings_from_data(data, source):
    op = _req(data, "openproject", source)
    openproject = OpenProject(
        url=_req(op, "url", "openproject"),
        admin_token_env=_req(op, "admin_token_env", "openproject"),
        owner_login=_req(op, "owner_login", "openproject"),
    )
    tokens = dict(_req(data, "tokens", source))
    for who in ("claude", "codex", "spark", "conductor"):
        if who not in tokens:
            raise SettingsError(
                "%s: [tokens] is missing %r" % (source, who)
            )
    github = _req(data, "github", source)
    users = _req(data, "users", source)
    email_domain = _req(users, "email_domain", "users")
    cond = _req(data, "conductor", source)
    conductor = Conductor(
        live=bool(_req(cond, "live", "conductor")),
        interval_seconds=_req(cond, "interval_seconds", "conductor"),
        state_dir=os.path.expanduser(str(_req(cond, "state_dir", "conductor"))),
        runtimes_dir=_opt_runtimes_dir(cond),
        readiness_gate=_opt_gate(cond),
    )
    run = _req(data, "runner", source)
    command = _req(run, "command", "runner")
    if not isinstance(command, list) or not all(
        isinstance(c, str) for c in command
    ):
        raise SettingsError("runner: command must be a list of strings")
    limits = _req(run, "limits_minutes", "runner")
    if not isinstance(limits, dict):
        raise SettingsError("runner: limits_minutes must be a table")
    for key in REQUIRED_LIMITS:
        if key not in limits:
            raise SettingsError(
                "runner: limits_minutes is missing %r" % (key,)
            )
    worker_env = run.get("worker_env", [])
    if not isinstance(worker_env, list) or not all(
        isinstance(name, str) and name for name in worker_env
    ):
        raise SettingsError("runner: worker_env must be a list of env var names")
    # A worker may only receive its own key. Refuse every other role's token
    # variable so a config slip can never hand the worker admin, GitHub,
    # conductor or other models' credentials.
    forbidden = {op["admin_token_env"], _req(github, "token_env", "github")}
    forbidden.update(tokens.values())
    leaked = sorted(set(worker_env) & forbidden)
    if leaked:
        raise SettingsError(
            "runner: worker_env must not include token variables of other "
            "roles: %s" % ", ".join(leaked)
        )
    runner = Runner(
        command=tuple(command),
        max_parallel=_req(run, "max_parallel", "runner"),
        limits_minutes=dict(limits),
        worker_env=tuple(worker_env),
    )
    projects = []
    for p in data.get("project", []):
        where = "project %r" % p.get("key", "?")
        visibility = _req(p, "visibility", where)
        if visibility not in ("Public", "Private"):
            raise SettingsError(
                "%s: visibility must be \"Public\" or \"Private\", got %r"
                % (where, visibility)
            )
        has_test_env = bool(_req(p, "has_test_env", where))
        test_signal = None
        if "test_signal" in p:
            test_signal = _signal(p["test_signal"], where + " test_signal")
        if has_test_env and test_signal is None:
            raise SettingsError(
                "%s: has_test_env is true but test_signal is missing" % where
            )
        projects.append(
            Project(
                key=_req(p, "key", where),
                name=_req(p, "name", where),
                repo=_req(p, "repo", where),
                visibility=visibility,
                has_test_env=has_test_env,
                test_url=str(p.get("test_url", "")),
                test_signal=test_signal,
                prod_signal=_signal(_req(p, "prod_signal", where), where),
                local_repo=os.path.expanduser(_opt_str(p, "local_repo", where)),
                base_ref=_opt_str(p, "base_ref", where) or "main",
                pr_base=_validated_pr_base(p, where),
                runtime=_opt_runtime(p, where),
                setup=_opt_command(p, "setup", where),
                test=_opt_command(p, "test", where),
                autonomy_lead=_opt_autonomy(p, where)[0],
                autonomy_until=_opt_autonomy(p, where)[1],
            )
        )
    permcheck = data.get("permcheck", {})
    if not isinstance(permcheck, dict):
        raise SettingsError("permcheck: must be a table")
    return Settings(
        openproject=openproject,
        tokens=tokens,
        github_token_env=_req(github, "token_env", "github"),
        users_email_domain=email_domain,
        conductor=conductor,
        runner=runner,
        projects=tuple(projects),
        permcheck=dict(permcheck),
    )


def load_file(path):
    """Load settings from an explicit file path."""
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SettingsError("cannot load settings file %s: %s" % (path, exc))
    if not isinstance(data, dict):
        raise SettingsError("settings file %s: top level must be a table" % path)
    return _settings_from_data(data, str(path))


def default_path():
    base = os.environ.get("OPL_CONFIG_DIR", "~/.config/opl")
    return os.path.join(os.path.expanduser(base), "opl.toml")


def load(config_dir=None):
    """Load `$OPL_CONFIG_DIR/opl.toml` (default `~/.config/opl/opl.toml`)."""
    if config_dir is None:
        path = default_path()
    else:
        path = os.path.join(os.path.expanduser(config_dir), "opl.toml")
    return load_file(path)


def redact(text, settings):
    """Replace every known secret value in `text` with `***`.

    Only values currently present in the environment are known; missing
    variables are skipped, never an error. Values shorter than 4 characters
    are skipped too: masking every "x" would destroy the text for no
    realistic secret.
    """
    names = (
        [settings.openproject.admin_token_env]
        + list(settings.tokens.values())
        + [settings.github_token_env]
    )
    values = []
    for name in names:
        value = os.environ.get(name, "")
        if value and len(value) >= 4:
            values.append(value)
    # Plain and base64 forms of every known value, plus any Authorization
    # header, via the shared scrubber (TH.2).
    from opl.redaction import scrub

    return scrub(text, values)
