"""Estimate calibration: actual tokens per model × size vs estimates.toml.

Standard library only. Groups finished tasks' Actual tokens by (assignee,
size), reports median and 90th percentile against the current estimate,
and suggests a direction. Never edits any file.
"""

from __future__ import annotations

import math
import statistics


def _p90(values):
    ordered = sorted(values)
    rank = int(math.ceil(0.9 * len(ordered))) - 1
    return ordered[max(0, rank)]


def calibrate(world, estimates):
    """One row per (model, size) with actuals: estimate, median, p90."""
    groups = {}
    for item in world.items.values():
        if item.type != "Task" or item.actual_tokens is None:
            continue
        if (item.assignee, item.size) not in estimates:
            continue
        groups.setdefault((item.assignee, item.size), []).append(
            item.actual_tokens)
    rows = []
    for (model, size) in sorted(groups):
        values = groups[(model, size)]
        row = estimates[(model, size)]
        estimate = row["input_tokens"] + row["output_tokens"]
        median = statistics.median(values)
        ninetieth = _p90(values)
        if median > estimate:
            suggestion = "raise estimate towards the median"
        elif ninetieth < estimate:
            suggestion = "lower estimate towards the p90"
        else:
            suggestion = "ok"
        rows.append({"model": model, "size": size, "n": len(values),
                     "estimate_tokens": estimate,
                     "median_tokens": median, "p90_tokens": ninetieth,
                     "suggestion": suggestion})
    return rows


def format_calibration(rows):
    """Render the calibration report as human-readable text."""
    if not rows:
        return "no actuals recorded yet\n"
    lines = []
    for row in rows:
        lines.append(
            "%s/%s: n=%d estimate=%d median=%d p90=%d -> %s"
            % (row["model"], row["size"], row["n"],
               row["estimate_tokens"], row["median_tokens"],
               row["p90_tokens"], row["suggestion"]))
    return "\n".join(lines) + "\n"
