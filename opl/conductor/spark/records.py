"""Run records: one JSON line per Spark run plus the tuning report.

Standard library only. Every worker invocation (build attempt, review run,
test run) appends a line to `<state_dir>/runs.jsonl`; `bin/opl-conductor
runs` reads it back into per-(kind, size) medians/maxima and a timeout
count so the time limits can be tuned from real durations.
"""

from __future__ import annotations

import json
import os
import statistics

FILENAME = "runs.jsonl"

_REQUIRED = ("task", "kind", "size", "started", "ended",
             "duration_s", "outcome", "cost_usd")


def record_run(state_dir, task, kind, size, started, ended,
               duration_s, outcome, cost_usd=0.0, worktree="", commit=""):
    """Append one run record; creates the state dir when missing."""
    os.makedirs(state_dir, exist_ok=True)
    row = {"task": task, "kind": kind, "size": size,
           "started": started, "ended": ended,
           "duration_s": float(duration_s), "outcome": outcome,
           "cost_usd": float(cost_usd), "worktree": worktree,
           "commit": commit}
    path = os.path.join(state_dir, FILENAME)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def read_runs(state_dir):
    """Read all well-formed records; malformed lines are skipped."""
    path = os.path.join(state_dir, FILENAME)
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and "task" in row:
                rows.append(row)
    return rows


def summarize(runs):
    """Group durations by (kind, size); count timeouts and stalls."""
    groups = {}
    timeouts = 0
    for row in runs:
        if row.get("outcome") in ("timeout", "stalled"):
            timeouts += 1
        try:
            duration = float(row.get("duration_s", 0.0))
        except (TypeError, ValueError):
            continue
        key = (str(row.get("kind", "?")), str(row.get("size", "-")))
        groups.setdefault(key, []).append(duration)
    summary = {"groups": {}, "timeouts": timeouts, "runs": len(runs)}
    for key in sorted(groups):
        durations = sorted(groups[key])
        summary["groups"][key] = {
            "count": len(durations),
            "median_s": statistics.median(durations),
            "max_s": durations[-1],
        }
    return summary


def format_report(summary):
    """Render the tuning report as human-readable text."""
    if not summary["groups"]:
        return "no runs recorded yet\n"
    lines = []
    for (kind, size), stats in sorted(summary["groups"].items()):
        lines.append(
            "%s size %s: n=%d median=%.1fs max=%.1fs"
            % (kind, size, stats["count"],
               stats["median_s"], stats["max_s"]))
    lines.append("timeouts/stalls: %d of %d runs"
                 % (summary["timeouts"], summary["runs"]))
    return "\n".join(lines) + "\n"
