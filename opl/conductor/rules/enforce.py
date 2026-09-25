"""Enforce rule: nesting, Spark-on-Private, approval authority, reviewer and
risk discipline.

Pure: violations(world) reports reasons, enforce(world) blocks violating
tasks. Stage-order knowledge (which feature statuses count as "Approved or
later") mirrors config/pm-model.toml; if the model order changes, update
APPROVED_ONWARDS here. Standard library only.
"""

from __future__ import annotations

from opl.conductor.state import Change
from opl.github import pr_source_problem


# Feature statuses at or past approval, in model order. Parked/Rejected are
# sideways states, not "later".
APPROVED_ONWARDS = frozenset(
    {"Approved", "Building", "In test", "In production", "Done"}
)

RISK_RANK = {None: 0, "Low": 1, "Medium": 2, "High": 3}

# Statuses that close a task. (Task types have no other closed state.)
_CLOSED_TASK = frozenset({"Merged"})


def _project(world, item):
    return world.projects.get(item.project)


def violations(world):
    """Map item id -> reason for every current violation."""
    found = {}
    for item in world.items.values():
        if item.status in _CLOSED_TASK:
            continue
        reason = _violation(world, item)
        if reason is not None:
            found[item.id] = reason
    return found


def _violation(world, item):
    parent = world.items.get(item.parent_id) if item.parent_id is not None else None
    project = _project(world, item)
    visibility = project.visibility if project is not None else ""
    if item.type == "Task":
        if parent is None or parent.type != "Feature":
            return "Task must sit under a Feature"
        if item.assignee == "spark" and visibility == "Private":
            return "Spark may not work on Private projects"
        if item.pr_url and project is not None:
            wrong = pr_source_problem(project.repo, item.pr_url)
            if wrong:
                return wrong
        if (parent.status in APPROVED_ONWARDS
                and RISK_RANK.get(item.risk, 0)
                < RISK_RANK.get(item.risk_highest_since_approval, 0)
                and not item.risk_lowered_by_owner):
            return "Risk lowered without the owner"
        # Approvals are plain fields any editor can set, so only the
        # journal's author counts (TH.5). To recover, clear the field; the
        # owner or reviewer then sets it again.
        if item.merge_ok and not item.merge_ok_by_owner:
            return "Unauthorised approval: Merge OK was not set by the owner"
        if item.review_result == "Pass" and not item.review_by_reviewer:
            return ("Unauthorised approval: Review result was not set by "
                    "the task's reviewer")
        # E4 and E5 only judge a reviewer that is actually set; a missing
        # reviewer during planning is E6's job (after approval), not theirs.
        if (
            item.reviewer
            and item.reviewer == item.assignee
            and not (
                visibility == "Public"
                and item.risk == "Low"
                and item.assignee == "spark"
            )
        ):
            return "Reviewer must differ from builder"
        if (
            item.reviewer
            and (item.risk in ("Medium", "High") or visibility == "Private")
            and item.reviewer not in ("claude", "codex")
        ):
            return "Risk needs a Claude or Codex reviewer"
        if (
            parent.status in APPROVED_ONWARDS
            and not (item.assignee and item.reviewer and item.size and item.risk)
        ):
            return "Task needs assignee, reviewer, size and risk"
    elif item.type == "Feature":
        if parent is None or parent.type != "Epic":
            return "Feature must sit under an Epic"
    return None


def enforce(world):
    """Block each violating, not-already-Blocked task."""
    violating = violations(world)
    changes = []
    for item_id in sorted(violating):
        item = world.items[item_id]
        if item.type != "Task" or item.status == "Blocked":
            continue
        changes.append(
            Change(
                rule="enforce",
                target="item",
                key=str(item_id),
                field="status",
                new="Blocked",
                reason=violating[item_id],
            )
        )
    return changes
