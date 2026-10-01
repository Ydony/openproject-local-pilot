"""OpenCode adapter for the Spark runner (T5.1 live-pilot blocker).

The conductor's Spark route already produces a scrubbed markdown packet,
an isolated worktree, and bounded execution through
``opl.conductor.spark.supervisor.run_worker`` (which owns the process
tree, enforces time/stall limits, and isolates the environment to
``[runner] worker_env`` plus OS basics). What was missing was a
practically launchable worker command that speaks OpenCode:

packet file -> prompt, working folder, explicit model/variant.

This module is that minimal generic adapter. It does NOT invent a
second-model route: only the Spark/OpenCode route is implemented. A
missing real-model route for Claude/Codex is a blocker, never a reason
to relabel a Spark run (see LIVE-PILOT-RESULT.md).

OpenCode CLI shape (verified against https://opencode.ai/docs/cli/ ,
``opencode run`` section)::

    opencode run --model <provider/model> [--variant <v>] --dir <workdir>
        [--format json] <prompt...>

``--model`` is ``provider/model``; ``--variant`` is provider-specific
reasoning effort; ``--dir`` is the working directory. The operator must
re-check ``opencode run --help`` against the installed version during
the live check: flag names are third-party-observed, not proven here.

Windows shell discovery: OpenCode honours ``OPENCODE_GIT_BASH_PATH``
(path to the Git Bash executable on Windows). Git Bash (``bash.exe``
under a Git install) and the WSL launcher (``wsl.exe``) are different
programs: this module never reports one as the other.

Secrets discipline: functions here handle names only. Packet text is
already scrubbed by the runner before it is written; ``build_command``
additionally refuses to build when any ``forbidden`` value (a secret
*value*) appears in the prompt or argv. Nothing here reads, prints,
stores, or embeds a secret value.

Standard library only.
"""

from __future__ import annotations

import json
import ntpath
import os
import re

DEFAULT_BIN = "opencode"

#: Environment variable naming the Git Bash executable (OpenCode's own
#: documented override). A path, never a secret.
GIT_BASH_ENV = "OPENCODE_GIT_BASH_PATH"

#: Config/env name for an explicit opencode binary override (a path,
#: never a secret). The runner passes the binary as an argv element;
#: this name is only read by the thin ``bin/opl-opencode-run`` wrapper.
BIN_ENV = "OPL_OPENCODE_BIN"

#: Upper bound for a packet read: prompts are kilobytes, never megabytes.
MAX_PACKET_BYTES = 200_000


# -- OpenCode JSON lines -> plain text (shared parser, issue #27) ------------
#
# Real OpenCode 1.18 events nest everything under ``part``::
#
#     {"type": "text", "part": {"text": "..."}}            -> assistant text
#     {"type": "tool_use", "part": {"tool": "bash",
#        "state": {"title": "...", "status": ...}}}        -> [tool] <title>
#     {"type": "step_finish", "part": {"tokens": {"input": N,
#        "output": N, ...}, "cost": F}}                   -> usage totals
#     {"type": "step_start", ...}                          -> silent
#
# Legacy flat shapes (``{"type": "text", "text": ...}``,
# ``{"type": "tool", "title": ...}``, ``{"type": "usage", ...}``) from the
# first guessed converter are still accepted, so old fakes/logs convert.
# ``sandbox_run`` reuses these functions; it must not define its own copy.

#: Tool-ish event types the launcher may emit for tool calls.
TOOL_TYPES = ("tool", "tool_use", "tool_call")


def _number(value):
    """Numeric value as-is, else None (bools are never counts)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def extract_usage(obj):
    """(input, output, cost) numbers from an event, or None.

    Real shape: ``part.tokens`` holds ``input``/``output`` and ``part``
    holds ``cost``. Legacy shapes (flat ``{"input": N, "output": N,
    "cost": N}`` or nested ``{"usage": {...}}``) are also accepted;
    missing keys default to 0/0.0. Non-numeric or absent usage is None.
    """
    if not isinstance(obj, dict):
        return None
    part = obj.get("part")
    if not isinstance(part, dict):
        part = {}
    found = False
    total_in = 0
    total_out = 0
    cost = 0.0
    tokens = part.get("tokens")
    if isinstance(tokens, dict):
        for key in ("input", "output"):
            raw = _number(tokens.get(key))
            if raw is not None:
                found = True
                if key == "input":
                    total_in += raw
                else:
                    total_out += raw
    raw_cost = _number(part.get("cost"))
    if raw_cost is not None:
        found = True
        cost += float(raw_cost)
    # Legacy fallbacks: {"usage": {...}} or flat input/output/cost.
    legacy = obj.get("usage")
    sources = [legacy] if isinstance(legacy, dict) else []
    sources.append(obj)
    part_usage = part.get("usage")
    if isinstance(part_usage, dict):
        sources.insert(0, part_usage)
    for source in sources:
        for key in ("input", "output", "cost"):
            # Skip keys already covered by the real shape.
            if source is obj and key in ("input", "output") and \
                    isinstance(tokens, dict) and key in tokens:
                continue
            if source is obj and key == "cost" and \
                    _number(part.get("cost")) is not None:
                continue
            raw = _number(source.get(key))
            if raw is not None:
                found = True
                if key == "input":
                    total_in += raw
                elif key == "output":
                    total_out += raw
                else:
                    cost += float(raw)
    if not found:
        return None
    return (total_in, total_out, cost)


def render_event(obj):
    """(text_lines, usage) for one parsed JSON event.

    Assistant text passes through verbatim (so the worker's
    ``OPL-RESULT:`` line survives intact); tool calls render as
    ``[tool] <title>``; usage-only events contribute no text.
    Unknown shapes contribute nothing.
    """
    if not isinstance(obj, dict):
        return None, None
    kind = obj.get("type", "")
    usage = extract_usage(obj)
    part = obj.get("part")
    if not isinstance(part, dict):
        part = {}
    if kind == "text":
        text = part.get("text", obj.get("text"))
        if isinstance(text, str) and text:
            return text.splitlines() or [""], usage
        return None, usage
    if kind in TOOL_TYPES:
        state = part.get("state")
        if not isinstance(state, dict):
            state = {}
        title = (state.get("title") or part.get("title")
                 or obj.get("title") or obj.get("name")
                 or part.get("tool") or obj.get("tool") or "tool")
        return ["[tool] %s" % title], usage
    if kind == "error":
        return [render_error(obj)], usage
    return None, usage


def render_error(obj):
    """One ``[error]`` line for an OpenCode error event (#67).

    Name, HTTP status, retryability and message only; never response
    headers or bodies. Without it a provider refusal left the run log
    holding nothing but ``usage unknown``.
    """
    error = obj.get("error") if isinstance(obj.get("error"), dict) else {}
    data = error.get("data") if isinstance(error.get("data"), dict) else {}
    name = str(error.get("name") or "error")[:40]
    status = data.get("statusCode")
    message = " ".join(str(data.get("message") or error.get("message")
                           or "no message").split())[:300]
    retry = data.get("isRetryable")
    parts = [name]
    if isinstance(status, int):
        parts.append(str(status))
    if retry is False:
        parts.append("(not retryable)")
    return "[error] %s: %s" % (" ".join(parts), message)


# HTTP statuses that mean the provider refuses this account, not this task:
# auth, billing, permission and rate/quota limits (#67).
_PROVIDER_STATUSES = frozenset({401, 402, 403, 429})
_ERROR_LINE_RE = re.compile(r"^\[error\] \S+(?: (\d{3}))?( \(not retryable\))?: ")


def provider_error(text):
    """The first ``[error]`` line that is a provider refusal, or None.

    A refusal (status 401/402/403/429, or marked not retryable) fails
    every run the same way, so it must pause Spark rather than count
    against the task (#67).
    """
    for line in (text or "").splitlines():
        match = _ERROR_LINE_RE.match(line.strip())
        if not match:
            continue
        status = int(match.group(1)) if match.group(1) else None
        if status in _PROVIDER_STATUSES or match.group(2):
            return line.strip()
    return None


def convert_line(line):
    """(text_lines, usage) for one raw launcher stdout line.

    Non-JSON lines pass through verbatim, so a worker result line is
    never lost to a format mismatch.
    """
    text = (line or "").rstrip("\n")
    try:
        obj = json.loads(text)
    except ValueError:
        return ([text] if text else None), None
    rendered, usage = render_event(obj)
    if rendered is None and usage is None:
        return None, None
    return rendered, usage


def format_usage(totals):
    """At most 3 usage summary lines (or ``usage unknown``)."""
    if totals is None:
        return ["usage unknown"]
    total_in, total_out, cost = totals
    return ["usage: input=%d output=%d cost=%.4f"
            % (total_in, total_out, cost)]


_USAGE_RE = re.compile(
    r"^usage:\s*input=(\d+)\s+output=(\d+)\s+cost=([0-9]+(?:\.[0-9]+)?)\s*$",
    re.MULTILINE)


def parse_usage_cost(text):
    """Cost from an adapter ``usage:`` summary line, or None.

    Shared parser for the ``usage: input=N output=N cost=F`` line that
    ``format_usage`` emits: the supervisor reuses this so a run without
    an ``OPL-COST:`` line still records its real cost, and a run with
    neither records unknown (None), never 0. ``usage unknown`` parses
    as None.
    """
    match = _USAGE_RE.search(text or "")
    if not match:
        return None
    try:
        return float(match.group(3))
    except ValueError:
        return None


def _split_path(path_value):
    """Split a PATH-style string on the platform separator."""
    return [p for p in (path_value or "").split(os.pathsep) if p]


def discover_shell(*, os_name=None, env=None, path_value=None, exists=None):
    """Locate Git Bash and the WSL launcher without confusing them.

    Returns ``{"git_bash": path-or-None, "wsl": path-or-None}`` where
    each path is the executable found, or None when absent.

    - ``git_bash`` is only ever a ``bash.exe`` (Git Bash) or a POSIX
      ``bash``/``sh``; it is never ``wsl.exe``.
    - ``wsl`` is only ever ``wsl.exe``; it is never reported as bash.
    - On POSIX the shell is ``/bin/sh`` when present (reported under
      ``git_bash`` key as ``"posix:/bin/sh"`` is NOT used; instead the
      caller gets ``{"git_bash": None, "wsl": None}`` plus
      ``"posix"`` -- see below).

    For testability every input is injectable: ``os_name`` defaults to
    ``os.name``, ``env`` to ``os.environ``, ``path_value`` to the PATH,
    and ``exists`` to ``os.path.isfile``.
    """
    if os_name is None:
        os_name = os.name
    if env is None:
        env = os.environ
    if path_value is None:
        try:
            path_value = env.get("PATH", "")
        except AttributeError:
            path_value = ""
    if exists is None:
        exists = os.path.isfile

    if os_name != "nt":
        posix = "/bin/sh" if exists("/bin/sh") else None
        return {"posix": posix, "git_bash": None, "wsl": None}

    found_bash = None
    found_wsl = None

    # OpenCode's own override wins for Git Bash only when it names bash.
    override = (env.get(GIT_BASH_ENV, "") or "").strip() if hasattr(env, "get") else ""
    if override and override.lower().endswith("bash.exe") and exists(override):
        found_bash = override

    # Windows path rules, not the host's: the same decision must come out
    # whether this code runs on Windows or is tested on Linux.
    dirs = [p for p in (path_value or "").split(";") if p]
    for directory in dirs:
        if found_bash is None:
            candidate = ntpath.join(directory, "bash.exe")
            try:
                if exists(candidate):
                    # Never accept a WSL launcher under a bash name.
                    if ntpath.basename(candidate).lower() != "wsl.exe":
                        found_bash = candidate
            except (OSError, ValueError):
                pass
        if found_wsl is None:
            candidate = ntpath.join(directory, "wsl.exe")
            try:
                if exists(candidate):
                    found_wsl = candidate
            except (OSError, ValueError):
                pass
        if found_bash is not None and found_wsl is not None:
            break

    return {"git_bash": found_bash, "wsl": found_wsl}


def validate_model(model):
    """Require an explicit ``provider/model`` (no silent defaults)."""
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be an explicit provider/model, got %r" % (model,))
    text = model.strip()
    if " " in text or "\n" in text or "\t" in text:
        raise ValueError("model must not contain whitespace: %r" % (model,))
    if "/" not in text:
        raise ValueError(
            "model must be provider/model (e.g. 'example/spark-1'), got %r" % (model,)
        )
    provider, _, name = text.partition("/")
    if not provider or not name:
        raise ValueError("model must be provider/model, got %r" % (model,))
    return text


def validate_variant(variant):
    """A variant is optional; when given it must be a single token."""
    if variant is None or variant == "":
        return ""
    if not isinstance(variant, str):
        raise ValueError("variant must be a string, got %r" % (variant,))
    text = variant.strip()
    if not text:
        return ""
    if any(ch.isspace() for ch in text):
        raise ValueError("variant must be a single token, got %r" % (variant,))
    return text


def read_packet(packet_path, *, limit_bytes=MAX_PACKET_BYTES):
    """Read the packet file as the prompt (fail-closed on empty/huge)."""
    if not isinstance(packet_path, str) or not packet_path:
        raise ValueError("packet path must be a non-empty string")
    try:
        size = os.path.getsize(packet_path)
    except OSError as exc:
        raise ValueError("cannot read packet %r: %s" % (packet_path, exc))
    if size <= 0:
        raise ValueError("packet %r is empty" % (packet_path,))
    if size > limit_bytes:
        raise ValueError(
            "packet %r is %d bytes (limit %d)" % (packet_path, size, limit_bytes)
        )
    with open(packet_path, encoding="utf-8") as fh:
        text = fh.read()
    if not text.strip():
        raise ValueError("packet %r has no prompt text" % (packet_path,))
    return text


def resolve_bin(explicit=None, *, env=None):
    """Which opencode binary to exec (a path, never a secret value)."""
    if explicit:
        if not isinstance(explicit, str):
            raise ValueError("opencode binary must be a string")
        return explicit
    if env is None:
        env = os.environ
    try:
        configured = (env.get(BIN_ENV, "") or "").strip()
    except AttributeError:
        configured = ""
    return configured or DEFAULT_BIN


def build_command(opencode_bin, model, variant, workdir, prompt_text,
                  *, forbidden=()):
    """Build ``opencode run`` argv from packet/workdir/model/variant.

    Layout: ``[bin, "run", "--model", M, ("--variant", V)?,
    "--dir", workdir, prompt]``. ``forbidden`` holds secret *values*
    that must not appear anywhere in the argv (fail-closed).
    """
    binary = opencode_bin or DEFAULT_BIN
    if not isinstance(binary, str) or not binary.strip():
        raise ValueError("opencode binary must be a non-empty string")
    clean_model = validate_model(model)
    clean_variant = validate_variant(variant)
    if not isinstance(workdir, str) or not workdir.strip():
        raise ValueError("workdir must be a non-empty path")
    if not isinstance(prompt_text, str) or not prompt_text.strip():
        raise ValueError("prompt text must be non-empty")

    argv = [binary.strip(), "run", "--model", clean_model]
    if clean_variant:
        argv += ["--variant", clean_variant]
    argv += ["--dir", workdir.strip(), prompt_text]

    for secret in forbidden or ():
        if secret and len(secret) >= 4 and secret in " ".join(argv):
            raise ValueError("refusing to build argv containing a secret value")
    return argv
