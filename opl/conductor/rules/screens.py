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

# Subject of the standing epic/feature `bin/opl-configure` creates per
# project (opl/configure/apply_api.py): the one Approved feature that is
# legitimately empty until bugs and chores arrive (DESIGN.md section 1).
_MAINTENANCE_SUBJECT = "Maintenance"


def _display(login):
    return str(login)[:1].upper() + str(login)[1:]


def _is_under_maintenance_epic(world, item):
    """True when the item's parent is the standing Maintenance epic."""
    parent = (world.items.get(item.parent_id)
              if item.parent_id is not None else None)
    return (parent is not None and parent.type == "Epic"
            and parent.subject == _MAINTENANCE_SUBJECT)


def _is_maintenance_feature(world, item):
    """The standing Maintenance feature under the Maintenance epic."""
    if item.type != "Feature" or item.subject != _MAINTENANCE_SUBJECT:
        return False
    return _is_under_maintenance_epic(world, item)


def _permcheck_target(permcheck):
    """Return (project_key, feature) for the permcheck fixture, reusing
    opl.permcheck.fixture_reference so the default subject lives in one
    place. Accepts a Settings or the plain permcheck dict (or None).
    A Settings resolves a configured project name to its world key via
    settings.projects (key or name match); a plain dict is already a key.
    """
    if permcheck is None:
        return "", ""
    from opl.permcheck import fixture_reference

    project, feature = fixture_reference(permcheck)
    if not project:
        return "", feature
    projects = getattr(permcheck, "projects", None)
    if projects:
        for p in projects:
            key = getattr(p, "key", None)
            name = getattr(p, "name", None)
            if project == key or project == name:
                return key, feature
    return project, feature


def _is_permcheck_fixture(world, item, permcheck=None):
    """The configured permission-check fixture feature (issue #34).

    The `[permcheck] feature` (default "Test feature") in the `[permcheck]
    project`, placed under the Maintenance epic. Like _is_maintenance_feature
    it requires the Maintenance-epic parent, so the same subject elsewhere
    is still real work. Returns False when permcheck is unconfigured.
    """
    if permcheck is None or item.type != "Feature":
        return False
    project_key, feature = _permcheck_target(permcheck)
    if not project_key or item.subject != feature:
        return False
    if item.project != project_key:
        return False
    return _is_under_maintenance_epic(world, item)


def screens(world, permcheck=None):
    """Compute screen-maintenance changes for a world.

    `permcheck` is the `[permcheck]` config (Settings or its plain dict);
    when given, the configured fixture feature is exempt from every owner
    action, exactly like the standing Maintenance feature, so stale
    Action/Needs you on it are cleared by the normal maintenance below.
    """
    bad = violations(world)
    now = world.now
    changes = []
    for item in sorted(world.items.values(), key=lambda i: i.id):
        action, reason = _action(world, item, bad.get(item.id), permcheck)
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


def _action(world, item, violation, permcheck=None):
    """Return (action, reason); action None means no owner attention needed."""
    if _is_permcheck_fixture(world, item, permcheck):
        return None, ""
    if item.type == "Task" and item.status == "Blocked":
        if violation is not None:
            return "Unblock", "Unblock: %s" % violation
        return "Unblock", "Unblock: task is Blocked"
    if violation is not None:
        return "Unblock", "Unblock: %s" % violation
    if (item.type == "Feature" and item.status in APPROVED_ONWARDS
            and not [c for c in world.children(item.id) if c.type == "Task"]
            and not _is_maintenance_feature(world, item)):
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
