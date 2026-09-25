"""Stages rule: move items forward when the world shows progress.

Pure: stages(world) returns Changes. Items carrying a violations() entry are
skipped outright. A feature with no tasks never moves (the screens rule flags
it). Datetimes are compared as collected; deploys without timestamps are
ignored. Standard library only.
"""

from __future__ import annotations

from opl.conductor.rules.enforce import violations
from opl.conductor.state import Change


def _tasks(children):
    return [c for c in children if c.type == "Task"]


def _latest_merge(world, feature):
    """Latest merged_at over child tasks' PRs, or None when unresolvable.

    One child without a known merge time makes the whole answer unknown: a
    deploy newer than the known merges could still predate that one.
    """
    latest = None
    for child in _tasks(world.children(feature.id)):
        pr = world.pull_requests.get(child.pr_url) if child.pr_url else None
        if pr is None or pr.merged_at is None:
            return None
        if latest is None or pr.merged_at > latest:
            latest = pr.merged_at
    return latest


def _deployed_since(world, project_key, target, since):
    for deploy in world.deploys:
        if deploy.project != project_key or deploy.target != target:
            continue
        if deploy.at is None:
            continue
        if since is None or deploy.at >= since:
            return True
    return False


def stages(world, model):
    """Compute stage moves for a world.

    `model` is needed only for S0 (the global default that marks items
    created without an explicit status).
    """
    bad = violations(world)
    changes = []
    for item in sorted(world.items.values(), key=lambda i: i.id):
        if item.id in bad:
            continue
        if item.type == "Task":
            changes.extend(_task_moves(world, item))
        elif item.type in ("Feature", "Epic"):
            changes.extend(_feature_moves(world, item, model))
    return changes


def _task_moves(world, item):
    parent = world.items.get(item.parent_id) if item.parent_id is not None else None
    if item.status == "Draft" and parent is not None and parent.type == "Feature" \
            and parent.status in ("Approved", "Building"):
        ready = True
        for pred_id in item.predecessors:
            pred = world.items.get(pred_id)
            if pred is None or pred.status != "Merged":
                ready = False
                break
        if ready:
            return [Change(rule="stages", target="item", key=str(item.id),
                           field="status", new="Ready",
                           reason="Ready: feature approved and predecessors merged")]
    if item.status == "In review" and item.pr_url:
        pr = world.pull_requests.get(item.pr_url)
        if pr is not None and pr.merged:
            return [Change(rule="stages", target="item", key=str(item.id),
                           field="status", new="Merged",
                           reason="Merged: pull request merged")]
    return []


def _feature_moves(world, item, model):
    default = getattr(model, "global_default", None)
    if default and item.status == default:
        # S0: created without an explicit status, so stuck in the global
        # default. Draft is never a resting state for Features/Epics (no
        # other rule moves into it), so anything sitting there moves on.
        targets = {"Feature": ("Proposed", "Proposed: created without a status"),
                   "Epic": ("Open", "Open: created without a status")}
        if item.type in targets:
            new, reason = targets[item.type]
            return [Change(rule="stages", target="item", key=str(item.id),
                           field="status", new=new, reason=reason)]
    children = _tasks(world.children(item.id))
    if not children:
        return []
    project = world.projects.get(item.project)
    if item.status == "Approved":
        if any(c.status in ("In progress", "In review", "Merged", "Blocked")
               for c in children):
            return [Change(rule="stages", target="item", key=str(item.id),
                           field="status", new="Building",
                           reason="Building: work started")]
    if item.status == "Building":
        if all(c.status == "Merged" for c in children):
            latest = _latest_merge(world, item)
            if project is not None and not project.has_test_env:
                return [Change(rule="stages", target="item", key=str(item.id),
                               field="status", new="In test",
                               reason="In test: all tasks merged (no test environment)")]
            # An unknown merge time is "not yet": an old deploy must not
            # promote the feature when we cannot prove it is newer.
            if latest is not None and project is not None and _deployed_since(
                    world, item.project, "test", latest):
                return [Change(rule="stages", target="item", key=str(item.id),
                               field="status", new="In test",
                               reason="In test: all tasks merged and deployed to test")]
    if item.status == "In test":
        if all(c.status == "Merged" for c in children):
            latest = _latest_merge(world, item)
            if latest is not None and _deployed_since(
                    world, item.project, "production", latest):
                return [Change(rule="stages", target="item", key=str(item.id),
                               field="status", new="In production",
                               reason="In production: production deploy seen")]
    return []
