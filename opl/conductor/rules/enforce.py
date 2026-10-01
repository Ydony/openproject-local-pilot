"""Enforce rule: nesting, Spark-on-Private, approval authority, reviewer,
risk and type-status discipline.

Pure: violations(world) reports reasons, enforce(world) blocks violating
tasks. Stage-order knowledge (which feature statuses count as "Approved or
later") and the per-type status lists mirror config/pm-model.toml; if the
model changes, update APPROVED_ONWARDS and TYPE_STATUSES here. Callers
holding a loaded model may pass it as violations(world, model) so the
live file drives the type-status check. Standard library only.
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

# Statuses that close a task: merged, or dropped by a lead model (#51).
_CLOSED_TASK = frozenset({"Merged", "Dropped"})

# Valid statuses per type. Mirrors config/pm-model.toml; if the model
# changes, update here. Callers that have a loaded model (opl.model.Model)
# may pass it to violations()/enforce() so the live file drives the check
# instead of this fallback.
TYPE_STATUSES = {
    "Epic": frozenset({"Open", "Closed"}),
    "Feature": frozenset({
        "Proposed", "Approved", "Building", "In test", "In production",
        "Done", "Parked", "Rejected",
    }),
    "Task": frozenset({
        "Draft", "Ready", "In progress", "In review", "Merged", "Blocked",
        "Dropped",
    }),
}

TYPE_DEFAULT = {"Epic": "Open", "Feature": "Proposed", "Task": "Draft"}


def _project(world, item):
    return world.projects.get(item.project)


def _type_statuses(model):
    if model is not None:
        try:
            mapping = {t.name: frozenset(t.statuses) for t in model.types}
        except AttributeError:
            mapping = {}
        if mapping:
            return mapping
    return TYPE_STATUSES


def _type_default(model, type_name):
    if model is not None:
        try:
            for t in model.types:
                if t.name == type_name:
                    return t.default_status
        except AttributeError:
            pass
    return TYPE_DEFAULT.get(type_name)


def _status_violation(item, model=None):
    """Reason when the item's status is not one of its type's statuses."""
    allowed = _type_statuses(model).get(item.type)
    if allowed is None or item.status in allowed:
        return None
    owner = None
    for tname, sset in _type_statuses(model).items():
        if item.status in sset:
            owner = tname
            break
    default = _type_default(model, item.type)
    if owner is not None:
        return ("%s has %s status %s; set %s"
                % (item.type, owner, item.status, default))
    return ("%s has unknown status %s; set %s"
            % (item.type, item.status, default))


def violations(world, model=None):
    """Map item id -> reason for every current violation."""
    found = {}
    for item in world.items.values():
        bad = _status_violation(item, model)
        if bad is not None:
            found[item.id] = bad
            continue
        if item.type == "Task" and item.status in _CLOSED_TASK:
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
        # Approval fields only gate a merge. Once the task's own PR is
        # merged, editing them changes nothing, so they are not judged:
        # blocking would strand a finished task (stages moves only
        # In review -> Merged). Seen live when Merge OK was ticked seconds
        # after a Low-risk merge (#52). Structural rules still apply.
        pr = world.pull_requests.get(item.pr_url) if item.pr_url else None
        merged = pr is not None and pr.merged
        if (parent.status in APPROVED_ONWARDS
                and RISK_RANK.get(item.risk, 0)
                < RISK_RANK.get(item.risk_highest_since_approval, 0)
                and not item.risk_lowered_by_owner):
            return "Risk lowered without the owner"
        # Approvals are plain fields any editor can set, so only the
        # journal's author counts (TH.5). To recover, clear the field; the
        # owner or reviewer then sets it again.
        if item.merge_ok and not item.merge_ok_by_owner and not merged:
            return "Unauthorised approval: Merge OK was not set by the owner"
        if (item.review_result == "Pass" and not item.review_by_reviewer
                and not merged):
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


# How to get out of each block, posted with it (#54): a Blocked task with
# no instructions made the owner guess. Keys are violation prefixes.
_RECOVERY = (
    ("Task must sit under a Feature",
     "set the task's parent to a Feature"),
    ("Spark may not work on Private projects",
     "assign the task to Claude or Codex"),
    ("Risk lowered without the owner",
     "set Risk back to its approved level; only the owner lowers it"),
    ("Unauthorised approval: Merge OK",
     "clear Merge OK; only the owner (or the project's autonomy lead while "
     "autonomy is active) sets it, and only Medium/High risk needs it"),
    ("Unauthorised approval: Review result",
     "clear Review result; the task's Reviewer sets it after reviewing "
     "the PR at its head"),
    ("Reviewer must differ from builder",
     "set a Reviewer other than the assignee"),
    ("Risk needs a Claude or Codex reviewer",
     "set Reviewer to Claude or Codex"),
    ("Task needs assignee, reviewer, size and risk",
     "fill in Assignee, Reviewer, Size and Risk"),
)
_LEAVE_BLOCKED = ("Then leave Blocked: with a PR, a lead model moves the task "
                  "Blocked -> In progress -> In review; without one, the "
                  "owner moves it to Draft and the conductor restarts it.")


def recovery_text(violation):
    """The violation plus how to recover from it (#54)."""
    for prefix, fix in _RECOVERY:
        if violation.startswith(prefix):
            return "%s. To recover: %s. %s" % (violation, fix, _LEAVE_BLOCKED)
    return "%s. %s" % (violation, _LEAVE_BLOCKED)


def enforce(world, model=None):
    """Block each violating, not-already-Blocked task."""
    violating = violations(world, model)
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
                reason=recovery_text(violating[item_id]),
            )
        )
    return changes
