"""Screens rule: Needs you, Action, Models and the At-risk marker.

Pure: screens(world) returns Changes. The first matching action wins;
needs_you mirrors whether an action is set, and changes only fire on value
differences (idempotent under apply_changes). Standard library only.
"""

from __future__ import annotations

from datetime import timedelta

from opl.conductor.rules.enforce import APPROVED_ONWARDS, violations
from opl.conductor.rules.merge import NEEDS_OWNER_OK
from opl.conductor.state import Change

_REASSIGN_AFTER = timedelta(days=3)


def _display(login):
    return str(login)[:1].upper() + str(login)[1:]


def screens(world):
    """Compute screen-maintenance changes for a world."""
    bad = violations(world)
    now = world.now
    changes = []
    for item in sorted(world.items.values(), key=lambda i: i.id):
        action, reason = _action(world, item, bad.get(item.id))
        want_needs = action is not None
        if item.needs_you != want_needs:
            changes.append(Change(rule="screens", target="item",
                                  key=str(item.id), field="needs_you",
                                  new=want_needs,
                                  reason=reason or "Needs you cleared"))
        if (item.action or None) != action:
            changes.append(Change(rule="screens", target="item",
                                  key=str(item.id), field="action",
                                  new=action,
                                  reason=reason or "Action cleared"))
        if item.type == "Feature":
            models = sorted({_display(t.assignee) for t in world.children(item.id)
                             if t.type == "Task" and t.assignee})
            if sorted(item.models) != models:
                changes.append(Change(rule="screens", target="item",
                                      key=str(item.id), field="models",
                                      new=tuple(models),
                                      reason="Models: %s" % ", ".join(models)))
    for project in sorted(world.projects.values(), key=lambda p: p.key):
        risky = any(
            i.project == project.key and i.type == "Task" and i.status == "Blocked"
            for i in world.items.values()
        ) or any(
            world.items[iid].project == project.key for iid in bad
        )
        if project.at_risk != risky:
            changes.append(Change(
                rule="screens", target="project", key=project.key,
                field="at_risk", new=risky,
                reason=("At risk: blocked tasks or violations present"
                        if risky else "On track: nothing blocked or violating")))
    return changes


def _action(world, item, violation):
    """Return (action, reason); action None means no owner attention needed."""
    if item.type == "Task" and item.status == "Blocked":
        if violation is not None:
            return "Unblock", "Unblock: %s" % violation
        return "Unblock", "Unblock: task is Blocked"
    if violation is not None:
        return "Unblock", "Unblock: %s" % violation
    if (item.type == "Feature" and item.status in APPROVED_ONWARDS
            and not [c for c in world.children(item.id) if c.type == "Task"]):
        return "Unblock", "Unblock: Feature has no tasks"
    if (item.type == "Task" and item.risk in NEEDS_OWNER_OK
            and item.status == "In review"
            and item.review_result == "Pass" and not item.merge_ok):
        return ("OK merge",
                "OK merge: %s-risk task passed review; owner approval needed"
                % item.risk)
    if item.type == "Feature" and item.status == "Proposed":
        return "Approve", "Approve: feature proposed"
    if item.type == "Feature" and item.status == "In test" and item.test_result:
        return "Decide deploy", "Decide deploy: feature in test with a test result"
    if item.type == "Feature" and item.status == "In production":
        return "Confirm done", "Confirm done: feature in production"
    if (item.type == "Task" and item.assignee in ("claude", "codex")
            and item.status in ("Ready", "In review")
            and (world.now - item.status_since) >= _REASSIGN_AFTER):
        return ("Reassign",
                "Reassign: %s waiting over 3 days in %s"
                % (item.assignee, item.status))
    return None, ""
