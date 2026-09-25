"""Usage collection: ccusage sessions joined to tasks by folder.

Standard library only. Shapes follow the pinned ccusage 20.0.24 report
output (see docs/USAGE-MAPPING.md): Claude/OpenCode rows carry
`modelBreakdowns[]` with `modelName`; Codex rows carry a `models` object
plus `sessionFile`. ccusage's own prices are ignored; only token counts
are used, priced with `config/prices.toml`. OpenCode folders come from a
read-only SQLite adapter on `opencode.db`.

The ccusage command and every log root are parameters, so tests use fakes
and never touch the network or real homes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import urllib.request

logger = logging.getLogger("opl.usage")

CCUSAGE_VERSION = "20.0.24"
CCUSAGE_COMMAND = ("npx", "ccusage@" + CCUSAGE_VERSION)
TIMEOUT_S = 120

_TOOLS = ("claude", "codex", "opencode")


class UsageError(Exception):
    """ccusage failed or its output/logs were unreadable."""


def _num(value, default=0):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _breakdown(part):
    """One per-model token bundle from either breakdown spelling."""
    if not isinstance(part, dict):
        return None
    return {
        "model": str(part.get("model", part.get("modelName", ""))),
        "input": _num(part.get("inputTokens")),
        "output": _num(part.get("outputTokens")),
        "cache_read": _num(part.get("cacheReadTokens")),
        "cache_write": _num(part.get("cacheCreationTokens")),
        "reasoning": _num(part.get("reasoningOutputTokens")),
        "total": (_num(part.get("totalTokens"))
                  if part.get("totalTokens") is not None else None),
    }


def parse_sessions(payload):
    """Normalize one `ccusage <tool> session --json` payload to rows.

    Each row holds id, session_file (codex), input, output, cache_read,
    cache_write and the per-model `breakdowns` list. Counters are kept as
    reported: session `totalTokens`/costs are never summed with parts, so
    nothing is double counted.
    """
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise UsageError("ccusage output is not JSON: %s" % exc)
    if not isinstance(payload, dict):
        raise UsageError("ccusage output has no session list")
    rows = payload.get("sessions")
    if rows is None:
        rows = payload.get("data", [])
    sessions = []
    for entry in rows or []:
        if not isinstance(entry, dict):
            continue
        breakdowns = []
        for part in entry.get("modelBreakdowns", []) or []:
            row = _breakdown(part)
            if row is not None:
                breakdowns.append(row)
        for name, metrics in (entry.get("models") or {}).items() \
                if isinstance(entry.get("models"), dict) else []:
            row = _breakdown(dict(metrics, modelName=name))
            if row is not None:
                breakdowns.append(row)
        sessions.append({
            "id": str(entry.get("sessionId", entry.get("session", ""))),
            "session_file": str(entry.get("sessionFile", "")),
            "input": _num(entry.get("inputTokens")),
            "output": _num(entry.get("outputTokens")),
            "cache_read": _num(entry.get("cacheReadTokens")),
            "cache_write": _num(entry.get("cacheCreationTokens")),
            "total": (_num(entry.get("totalTokens"))
                      if entry.get("totalTokens") is not None else None),
            "breakdowns": breakdowns,
        })
    return sessions


def _read_json_lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def _claude_folders(home):
    """{session id: cwd} from Claude Code transcripts.

    The report id is the transcript filename stem; the folder is that
    file's `cwd` entry — never the lossy folder slug. Orphaned and
    superseded duplicates are skipped.
    """
    found = {}
    root = os.path.join(home, "projects")
    if not os.path.isdir(root):
        return found
    for slug in sorted(os.listdir(root)):
        folder = os.path.join(root, slug)
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".jsonl") or ".orphaned-" in name \
                    or ".superseded-" in name:
                continue
            sid = name[: -len(".jsonl")]
            for obj in _read_json_lines(os.path.join(folder, name)):
                if isinstance(obj, dict) and obj.get("cwd"):
                    found[sid] = str(obj["cwd"])
                    break
    return found


def _rollout_cwd(path):
    for obj in _read_json_lines(path):
        if not isinstance(obj, dict):
            continue
        payload = obj.get("payload") or {}
        if obj.get("type") == "session_meta" and payload.get("cwd"):
            return (str(payload.get("id") or ""), str(payload["cwd"]))
    return None, None


def _codex_transcripts(home):
    """(archived, lookup keys, folder) for every rollout transcript."""
    found = []
    roots = []
    live = os.path.join(home, "sessions")
    if os.path.isdir(live):
        roots.append((False, live))
    arch = os.path.join(home, "archived_sessions")
    if os.path.isdir(arch):
        roots.append((True, arch))
    for archived, root in roots:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in sorted(filenames):
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(dirpath, name)
                sid, cwd = _rollout_cwd(path)
                if not cwd:
                    continue
                stem = name[: -len(".jsonl")]
                parts = stem.split("-")
                uuid = "-".join(parts[-5:]) if len(parts) > 2 else stem
                keys = [k for k in (sid, name, stem, uuid) if k]
                found.append((archived, keys, cwd))
    # Live transcripts win over archived copies of the same session.
    found.sort(key=lambda e: (e[0], e[1][0]))
    return found


def _codex_folders(home):
    """{session id: cwd} from Codex rollout transcripts.

    Nested day folders and archived transcripts are all read; archive
    duplicates of one session fold into a single entry.
    """
    found = {}
    for _archived, keys, cwd in _codex_transcripts(home):
        if keys[0] not in found:
            found[keys[0]] = cwd
    return found


def _codex_file_index(home):
    """Filename variants → folder for joining codex report rows."""
    index = {}
    for _archived, keys, cwd in _codex_transcripts(home):
        for key in keys:
            index.setdefault(key, cwd)
    return index


def _opencode_folders(home):
    """{session id: directory} from the OpenCode SQLite session table.

    Read-only (`mode=ro` + `query_only`): only `opencode.db` is opened,
    never auth files. Only the `session` table is read, so message/part
    joins can never multiply rows.
    """
    found = {}
    db = os.path.join(home, "opencode.db")
    if not os.path.isfile(db):
        return found
    try:
        # pathname2url escapes "?", "#" and "%" and gives Windows drive
        # paths the triple-slash URI form (TH.19).
        uri = "file:%s?mode=ro" % urllib.request.pathname2url(
            os.path.abspath(db))
        conn = sqlite3.connect(uri, uri=True, timeout=10)
    except sqlite3.Error as exc:
        raise UsageError("opencode.db unreadable: %s" % exc)
    try:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute("SELECT id, directory FROM session")
        for sid, directory in rows:
            if sid and directory:
                found[str(sid)] = str(directory)
    except sqlite3.Error as exc:
        raise UsageError("opencode.db session table unreadable: %s" % exc)
    finally:
        conn.close()
    return found


def session_folders(tool, home):
    """Map session ids to working folders for one tool's log root."""
    if tool == "claude":
        return _claude_folders(home)
    if tool == "codex":
        return _codex_folders(home)
    if tool == "opencode":
        return _opencode_folders(home)
    raise UsageError("unknown usage tool %r" % (tool,))


def _codex_keys(row):
    """Transcript lookup keys for one codex report row, best first."""
    keys = []
    for raw in (row.get("session_file", ""), row.get("id", "")):
        text = str(raw).replace("\\", "/").rstrip("/").split("/")[-1]
        if not text:
            continue
        keys.append(text)
        if text.endswith(".jsonl"):
            keys.append(text[: -len(".jsonl")])
        stem = text[: -len(".jsonl")] if text.endswith(".jsonl") else text
        parts = stem.split("-")
        if len(parts) > 2:
            keys.append("-".join(parts[-5:]))
    seen, ordered = set(), []
    for key in keys:
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def codex_join(report_rows, index, world, state_dir=None):
    """Match codex report rows to (kind, id) via transcript folders.

    `index` maps transcript lookup keys (basename, stem, UUID suffix) to
    folders. The report's `directory` field is never trusted; folders go
    through the same attribution rule as every other tool (TH.19).
    """
    attribute = attributor(world, state_dir)
    matched = {}
    for row in report_rows:
        for key in _codex_keys(row):
            folder = index.get(key)
            if folder is None:
                continue
            found = attribute(folder)
            if found is not None:
                matched[row["id"]] = found
            break
    return matched


# Delimited worktree prefixes, matched at the start of one of the last two
# path segments: build/fix/review cost belongs to the task, test cost to
# the feature. Every Spark worktree label produces exactly one of these.
_FOLDER_RE = re.compile(r"^(task-T|fix-|review-|test-)(\d+)-")


def task_for_folder(path):
    """Match a session folder to (kind, id): ("task", tid) or ("feature", fid).

    Never substring-matches: `T5` does not match `T50`, and a dated
    scratch folder matches nothing. Returns None when no segment qualifies.
    """
    text = str(path).replace("\\", "/").rstrip("/")
    segments = [seg for seg in text.split("/") if seg][-2:]
    for seg in reversed(segments):
        match = _FOLDER_RE.match(seg)
        if match:
            kind = "feature" if match.group(1) == "test-" else "task"
            return kind, int(match.group(2))
    return None


def match_tasks(world, folders):
    """Map session ids to (kind, id) via task_for_folder."""
    ids = {item.id for item in world.items.values()
           if item.type in ("Task", "Feature")}
    matched = {}
    for sid, folder in folders.items():
        found = task_for_folder(folder)
        if found is not None and found[1] in ids:
            matched[sid] = found
    return matched


def _canon_folder(path):
    """Comparable folder form: forward slashes, no trailing slash."""
    return str(path).replace("\\", "/").rstrip("/")


def manifest_tasks(state_dir):
    """Map registered worktree paths to task ids from runs.jsonl."""
    from opl.conductor.spark.records import read_runs

    try:
        rows = read_runs(state_dir)
    except OSError:
        return {}
    found = {}
    for row in rows:
        if isinstance(row, dict) and row.get("worktree") and row.get("task"):
            found[_canon_folder(row["worktree"])] = row["task"]
    return found


def match_manifest(world, state_dir, folders):
    """Map session ids to (kind, id) by registered worktree only.

    A session counts only when its folder is a recorded worktree; a folder
    matching several tasks (or none) stays unknown.
    """
    tasks = {i.id for i in world.items.values() if i.type == "Task"}
    worktrees = manifest_tasks(state_dir)
    matched = {}
    for sid, folder in folders.items():
        norm = _canon_folder(folder)
        hits = {tid for path, tid in worktrees.items()
                if tid in tasks and (norm == path
                                     or norm.startswith(path + "/"))}
        if len(hits) == 1:
            matched[sid] = ("task", next(iter(hits)))
    return matched


def manifest_targets(state_dir):
    """Registered worktree path -> set of (kind, id) from runs.jsonl.

    Test runs belong to their feature; every other run to its task. A
    path recorded for more than one item keeps all of them, so the
    caller can see it is ambiguous (never last-wins).
    """
    from opl.conductor.spark.records import read_runs

    if not state_dir:
        return {}
    try:
        rows = read_runs(state_dir)
    except OSError:
        return {}
    found = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("worktree"):
            continue
        try:
            oid = int(row.get("task"))
        except (TypeError, ValueError):
            continue
        kind = "feature" if row.get("kind") == "test" else "task"
        found.setdefault(_canon_folder(row["worktree"]), set()).add((kind, oid))
    return found


def attributor(world, state_dir=None):
    """folder -> (kind, id) or None, one rule for every tool (TH.19).

    1. A folder at or under registered worktrees (runs.jsonl) belongs to
       their item when exactly one item is recorded there; two or more is
       ambiguous and stays unknown, with no fallback.
    2. An unregistered folder uses the delimited prefix rule
       (task_for_folder): owner-started Claude/Codex sessions, DESIGN 7.
    3. Anything else stays unknown. The item must exist with the matching
       type (Task for "task", Feature for "feature").
    """
    types = {item.id: item.type for item in world.items.values()}
    registered = manifest_targets(state_dir)

    def valid(target):
        kind, oid = target
        return types.get(oid) == ("Feature" if kind == "feature" else "Task")

    def attribute(folder):
        norm = _canon_folder(folder)
        hits = set()
        seen = False
        for path, targets in registered.items():
            if norm == path or norm.startswith(path + "/"):
                seen = True
                hits |= targets
        if seen:
            if len(hits) == 1:
                target = next(iter(hits))
                return target if valid(target) else None
            return None
        found = task_for_folder(folder)
        if found is not None and valid(found):
            return found
        return None

    return attribute


def match_sessions(world, folders, state_dir=None):
    """Map session ids to (kind, id) with the shared attribution rule."""
    attribute = attributor(world, state_dir)
    matched = {}
    for sid, folder in folders.items():
        found = attribute(folder)
        if found is not None:
            matched[sid] = found
    return matched


def _close(a, b):
    """Token counters are integers: conventions must match exactly
    (TH.23, Codex F3; a tolerance could pick the wrong convention)."""
    return int(round(a)) == int(round(b))


def _normalized(part):
    """Exclusive (input, billed output, cache_read, cache_write, tokens).

    `totalTokens` tells the counting convention (TH.19, Codex E6):
    - exclusive: total = input + output + cache + reasoning (reasoning is
      billed at the output rate on top of output);
    - exclusive, reasoning inside output: total = input + output + cache;
    - inclusive (OpenAI style): total = input + output, where cache is part
      of input and reasoning part of output; cache is taken out of input,
      and output is billed once.
    Without a total the counters are taken as exclusive. A total that
    fits no convention returns None: the session cannot be priced.
    """
    i, o = part["input"], part["output"]
    cr, cw, r = part["cache_read"], part["cache_write"], part["reasoning"]
    total = part.get("total")
    if total is None or _close(total, i + o + cr + cw + r):
        return i, o + r, cr, cw, i + o + cr + cw + r
    if _close(total, i + o + cr + cw):
        return i, o, cr, cw, i + o + cr + cw
    if _close(total, i + o) and cr + cw <= i and r <= o:
        return i - cr - cw, o, cr, cw, i + o
    return None


def price_sessions(world, models, sessions, sid_to_task, incomplete=None):
    """Price matched sessions per model breakdown at matching [[model]] rates.

    Returns {(kind, id): {"tokens": int, "cost": float}}. Counters are
    normalised to exclusive form first (see _normalized). Session-level
    cache not already attributed to breakdowns splits across them by input
    share. If any session of an item cannot be priced (no breakdowns, an
    unknown model, or counters that fit no convention), the item's whole
    actual is left out: unknown, never a partial sum, never zero (TH.19).
    Those items are added to `incomplete` when a set is passed, so the
    caller can drop them across tools too.
    """
    from opl.costs_config import match_model, token_cost

    actuals = {}
    broken = set()
    items = world.items
    for row in sessions:
        target = sid_to_task.get(row["id"])
        if target is None:
            continue
        kind, oid = target
        item = items.get(oid)
        if item is None:
            continue
        if not row["breakdowns"]:
            logger.warning("usage: session %s has no model breakdowns; "
                           "leaving %s %d empty", row["id"], kind, oid)
            broken.add(target)
            continue
        entries, parts = [], []
        for part in row["breakdowns"]:
            entry = match_model(models, part["model"])
            if entry is None:
                logger.warning("usage: unknown model %r in session %s; "
                               "leaving %s %d empty",
                               part["model"], row["id"], kind, oid)
                break
            norm = _normalized(part)
            if norm is None:
                logger.warning("usage: session %s model %r counters fit no "
                               "known convention; leaving %s %d empty",
                               row["id"], part["model"], kind, oid)
                break
            entries.append(entry)
            parts.append(norm)
        else:
            parts_read = sum(p[2] for p in parts)
            parts_write = sum(p[3] for p in parts)
            rest_read = max(0.0, row["cache_read"] - parts_read)
            rest_write = max(0.0, row["cache_write"] - parts_write)
            if row.get("total") is not None and not _close(
                    row["total"], sum(p[4] for p in parts) + rest_read + rest_write):
                # The model rows don't account for the whole session: some
                # usage would go unpriced, so the session is unknown.
                logger.warning("usage: session %s total does not match its "
                               "model rows; leaving %s %d empty",
                               row["id"], kind, oid)
                broken.add(target)
                continue
            total_in = sum(p[0] for p in parts)
            cost, tokens = 0.0, 0
            for (inp, out, cr, cw, count), entry in zip(parts, entries):
                share = (inp / total_in) if total_in else 1.0 / len(parts)
                cost += token_cost(
                    entry, input_tokens=inp, output_tokens=out,
                    cache_read_tokens=cr + rest_read * share,
                    cache_write_tokens=cw + rest_write * share)
                tokens += int(count + (rest_read + rest_write) * share)
            slot = actuals.setdefault(target, {"tokens": 0, "cost": 0.0})
            slot["tokens"] += tokens
            slot["cost"] += cost
            continue
        broken.add(target)
    for target in broken:
        actuals.pop(target, None)
    if incomplete is not None:
        incomplete |= broken
    return actuals


def run_ccusage(command, tool, timeout=TIMEOUT_S, env=None):
    """Run one focused ccusage session report; return the parsed payload.

    `env` is the whole environment for ccusage; the conductor passes one
    without any token variable (TH.19). None means the current one.
    """
    try:
        proc = subprocess.run(
            list(command) + [tool, "session", "--json"],
            capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UsageError("ccusage %s failed: %s" % (tool, exc))
    if proc.returncode != 0:
        raise UsageError("ccusage %s exited %d: %s"
                         % (tool, proc.returncode,
                            proc.stderr.strip()[:200]))
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise UsageError("ccusage %s output is not JSON: %s" % (tool, exc))


def collect_actuals(world, prices, command=CCUSAGE_COMMAND, homes=None,
                    timeout=TIMEOUT_S, state_dir=None, env=None,
                    spark_data=None):
    """Actual tokens+cost per (kind, id) across tools (folder-matched).

    `homes` maps tool -> log root (default: the real homes, never used in
    tests). Sessions are attributed with one rule for every tool
    (attributor): a registered worktree from `state_dir`'s runs.jsonl
    first, else the delimited folder prefix. `spark_data` is Spark's
    shared OpenCode data dir (TH.18): it is read next to the owner's, and
    ccusage is pointed at both through OPENCODE_DATA_DIR. `env` is the
    environment for ccusage (the conductor strips every token variable).
    An item with any unpriceable session is left out entirely. Matched
    and unmatched counts and the ccusage version are logged for coverage.
    Raises UsageError when any ccusage call fails; the caller logs it and
    skips that loop's actuals.
    """
    if homes is None:
        homes = {"claude": os.path.expanduser("~/.claude"),
                 "codex": os.path.expanduser("~/.codex"),
                 "opencode": os.path.expanduser("~/.local/share/opencode")}
    base_env = dict(os.environ if env is None else env)
    actuals = {}
    incomplete = set()
    coverage = {}
    for tool in _TOOLS:
        tool_env = dict(base_env)
        if tool == "opencode":
            roots = [homes.get(tool, "")]
            if spark_data and os.path.isdir(spark_data):
                roots.append(os.path.join(spark_data, "opencode"))
            roots = [r for r in roots if r]
            tool_env["OPENCODE_DATA_DIR"] = ",".join(roots)
        sessions = parse_sessions(run_ccusage(command, tool, timeout, tool_env))
        ids = {r["id"] for r in sessions}
        if tool == "codex":
            matched = codex_join(sessions, _codex_file_index(
                homes.get(tool, "")), world, state_dir)
        else:
            if tool == "opencode":
                folders = {}
                for root in roots:
                    folders.update(session_folders(tool, root))
            else:
                folders = session_folders(tool, homes.get(tool, ""))
            matched = {sid: target for sid, target in
                       match_sessions(world, folders, state_dir).items()
                       if sid in ids}
        priced = price_sessions(world, prices["models"], sessions, matched,
                                incomplete)
        for key, slot in priced.items():
            total = actuals.setdefault(key, {"tokens": 0, "cost": 0.0})
            total["tokens"] += slot["tokens"]
            total["cost"] += slot["cost"]
        coverage[tool] = {"matched": len(matched), "unmatched": len(
            [i for i in ids if i not in matched])}
    # Explicitly unknown (not merely absent): the costs rule clears any
    # stale actual for these instead of keeping an old number (TH.23).
    for key in incomplete:
        actuals[key] = {"unknown": True}
    coverage["incomplete"] = len(incomplete)
    coverage["ccusage"] = CCUSAGE_VERSION
    logger.info("usage coverage: %s",
                json.dumps(coverage, sort_keys=True))
    return actuals


def _codex_transcript_keys(folders):
    """Expand {session id: folder} with basename/stem/UUID lookup keys."""
    index = {}
    for sid, folder in folders.items():
        index.setdefault(sid, folder)
    return index
