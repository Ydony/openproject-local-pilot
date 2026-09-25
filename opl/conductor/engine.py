"""Conductor engine: combine rules, guard, and apply or log.

run_once composes the four rules, drops no-ops, resolves conflicts and
enforces the ownership guard. apply() writes JSON lines in watch mode or
drives OpenProject/GitHub in live mode. Standard library only.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from opl import hal
from opl.conductor.rules import enforce as rule_enforce
from opl.conductor.rules import costs as rule_costs
from opl.conductor.rules import merge as rule_merge
from opl.conductor.rules import screens as rule_screens
from opl.conductor.rules import stages as rule_stages
from opl.openproject import ApiError

logger = logging.getLogger("opl.conductor")


def _current(world, change):
    if change.target == "item":
        try:
            item = world.items[int(change.key)]
        except (KeyError, ValueError, TypeError):
            return object()
        return getattr(item, change.field, None)
    if change.target == "project":
        project = world.projects.get(change.key)
        return getattr(project, change.field, None) if project else None
    if change.target == "pr":
        pr = world.pull_requests.get(change.key)
        return pr.merged if pr else None
    return object()


def drop_noops(changes, world):
    """Split changes into (effective, no-op descriptions)."""
    kept, dropped = [], []
    for change in changes:
        if _current(world, change) == change.new:
            dropped.append("%s %s %s already %r"
                           % (change.rule, change.target, change.key, change.new))
        else:
            kept.append(change)
    return kept, dropped


def resolve_conflicts(changes):
    """Deduplicate identical changes; drop contested ones with log lines."""
    groups = {}
    for change in changes:
        groups.setdefault((change.target, change.key, change.field), []).append(change)
    kept, logs = [], []
    for (target, key, field), group in sorted(groups.items()):
        first = group[0]
        if all(change.new == first.new for change in group[1:]):
            kept.append(first)
        else:
            logs.append(
                "conflict on %s %s %s: %s - dropping all"
                % (target, key, field,
                   ", ".join("%s wants %r" % (c.rule, c.new) for c in group)))
    return kept, logs


def guard_transitions(changes, world, model):
    """Drop status changes the Conductor role may not make, with log lines."""
    kept, logs = [], []
    for change in changes:
        if change.target == "item" and change.field == "status":
            try:
                item = world.items[int(change.key)]
            except (KeyError, ValueError, TypeError):
                logs.append("guard: unknown item %s - dropping" % (change.key,))
                continue
            allowed = model.transitions("Conductor", item.type)
            if (item.status, change.new) not in allowed:
                logs.append(
                    "guard: dropping %s %s -> %s (not a Conductor transition for %s)"
                    % (change.key, item.status, change.new, item.type))
                continue
        kept.append(change)
    return kept, logs


def run_once(world, model, costs=()):
    """Run every rule and return the guarded, conflict-free changes.

    `costs` is a precomputed list of cost Changes (the costs rule needs
    prices, estimates and usage that run_once does not load itself).
    """
    changes = (rule_enforce.enforce(world) + rule_stages.stages(world, model)
               + rule_screens.screens(world) + rule_merge.merge(world)
               + list(costs))
    changes, _ = drop_noops(changes, world)
    changes, conflicts = resolve_conflicts(changes)
    for line in conflicts:
        logger.warning(line)
    changes, guard_drops = guard_transitions(changes, world, model)
    for line in guard_drops:
        logger.warning(line)
    return changes


def apply(changes, world, op_client, gh, live, log_path):
    """Watch (default): append JSON lines. Live: drive the APIs."""
    if not live:
        if changes:
            parent = os.path.dirname(log_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            stamp = datetime.now(timezone.utc).isoformat()
            with open(log_path, "a", encoding="utf-8", newline="\n") as fh:
                for change in changes:
                    fh.write(json.dumps({
                        "time": stamp,
                        "rule": change.rule,
                        "target": change.target,
                        "key": change.key,
                        "field": change.field,
                        "old": _current(world, change),
                        "new": change.new,
                        "reason": change.reason,
                    }) + "\n")
        return
    # One PATCH per item per cycle: several changes to the same item share
    # a lockVersion, so separate PATCHes would 409 each other. Groups flush
    # before the remaining (pr/project) changes; all touch independent
    # resources, so the order between kinds does not matter.
    groups = {}
    order = []
    rest = []
    for change in changes:
        if change.target == "item":
            if change.key not in groups:
                groups[change.key] = []
                order.append(change.key)
            groups[change.key].append(change)
        else:
            rest.append(change)
    try:
        lookups = _LiveLookups(op_client)
    except ApiError as exc:
        logger.error("live lookups failed: %s", exc)
        return
    for key in order:
        try:
            _apply_item_group(key, groups[key], world, op_client, lookups)
        except ApiError as exc:
            logger.error("live apply failed for item %s: %s", key, exc)
    for change in rest:
        try:
            _apply_live(change, world, op_client, gh, lookups)
        except ApiError as exc:
            logger.error("live apply failed for %s %s: %s",
                         change.target, change.key, exc)


class _LiveLookups:
    """Resolve API ids once per live apply.

    Statuses and types come from their collections. Custom fields come
    from the per-project-type schema (`/api/v3/work_packages/schemas/
    {project}-{type}` — the global `/schema` path does not exist), read at
    the schema root by opl.hal, which also resolves the `allowedValues`
    of list fields to option links (TH.7).
    """

    _CUSTOM = {"action": "Action", "needs_you": "Needs you", "models": "Models",
               "review_result": "Review result", "test_result": "Test result",
               "est_cost": "Est. cost", "actual_cost": "Actual cost",
               "actual_tokens": "Actual tokens"}

    def __init__(self, op_client):
        self._op = op_client
        self.statuses = {}
        for element in op_client.get_all("/api/v3/statuses"):
            if element.get("name") is not None:
                self.statuses[element["name"]] = element.get("id")
        self.types = {}
        for element in op_client.get_all("/api/v3/types"):
            if element.get("name") is not None:
                self.types[element["name"]] = element.get("id")
        self._schemas = {}
        self._forms = {}

    def status_id(self, name):
        if name not in self.statuses:
            raise ApiError(0, "/api/v3/statuses",
                           "status %r not found" % (name,))
        return self.statuses[name]

    def schema(self, pid, tid):
        key = (str(pid), str(tid))
        if key not in self._schemas:
            self._schemas[key] = self._op.get(
                "/api/v3/work_packages/schemas/%s-%s" % key) or {}
        return self._schemas[key]

    def form_schema(self, pid, tid, wp_id):
        """The schema of a work package's edit form (with allowed values).

        Cached per project/type: the option lists used here (Action,
        Models, Review/Test result) belong to the custom field, not to the
        item. Don't reuse this cache for anything item-dependent (e.g.
        per-user permissions) without keying it by work package.
        """
        key = (str(pid), str(tid))
        if key not in self._forms:
            current = self._op.get("/api/v3/work_packages/%s" % wp_id) or {}
            form = self._op.post(
                "/api/v3/work_packages/%s/form" % wp_id,
                {"lockVersion": current.get("lockVersion", 0)}) or {}
            self._forms[key] = (form.get("_embedded") or {}).get("schema") or {}
        return self._forms[key]

    def custom_prop(self, field, pid, tid):
        name = self._CUSTOM.get(field, field)
        prop = hal.schema_fields(self.schema(pid, tid)).get(name)
        if prop is None:
            raise ApiError(0, "/api/v3/work_packages/schemas/%s-%s" % (pid, tid),
                           "custom field %r not exposed" % (name,))
        return prop

    def option_href(self, field, value, pid, tid, wp_id=None):
        """Link (or list of links) for a list-field value; None clears.

        A plain schema carries no allowed values: OpenProject only fills
        them in on a form (TH.23, Codex N3). So options come from the
        schema when present, else from the edit form of `wp_id` (one form
        per project/type, cached).
        """
        name = self._CUSTOM.get(field, field)
        prop = self.custom_prop(field, pid, tid)
        if value is None:
            return prop, {"href": None}
        allowed = hal.allowed_options(self.schema(pid, tid), prop)
        if not allowed and wp_id is not None:
            allowed = hal.allowed_options(self.form_schema(pid, tid, wp_id), prop)
        if isinstance(value, (list, tuple)):
            return prop, [self._option_href(name, v, allowed) for v in value]
        return prop, self._option_href(name, value, allowed)

    @staticmethod
    def _option_href(name, value, allowed):
        if str(value) in allowed:
            return {"href": allowed[str(value)]}
        raise ApiError(0, "/api/v3/work_packages/schemas",
                       "option %r not found for field %r" % (value, name))


def _apply_live(change, world, op_client, gh, lookups):
    # Kept for single non-item changes; item changes go through
    # _apply_item_group so one PATCH carries the whole cycle's edits.
    if change.target == "pr":
        # The merge rule sets `new` to the reviewed head SHA; GitHub
        # refuses the merge if the head has moved since (TH.5).
        sha = change.new if isinstance(change.new, str) else None
        if not sha:
            raise ApiError(0, change.key, "merge without a reviewed SHA refused")
        if not gh.merge(change.key, sha=sha):
            raise ApiError(0, change.key, "merge not accepted")
        for item in world.items.values():
            if item.pr_url == change.key:
                op_client.post(
                    "/api/v3/work_packages/%s/activities" % item.id,
                    {"comment": {"raw": change.reason}})
                break
        else:
            logger.warning("merged %s with no matching item", change.key)
        return
    if change.target == "project":
        project = world.projects[change.key]
        # Best available per review (T1.1-flagged unknown): the project
        # status is most likely a link. Fails loudly if wrong.
        op_client.patch(
            "/api/v3/projects/%s" % project.op_id,
            {"_links": {"status": {"href": "/api/v3/project_statuses/%s"
                                  % ("at_risk" if change.new else "on_track")}}})
        return
    raise ApiError(0, change.key, "unexpected single change %r" % (change.field,))


def _apply_item_group(item_id, group, world, op_client, lookups):
    item = world.items[int(item_id)]
    project = world.projects[item.project]
    tid = lookups.types.get(item.type)
    if tid is None:
        raise ApiError(0, "/api/v3/types",
                       "type %r not found" % (item.type,))
    body = {"lockVersion": item.lock_version}
    links = {}
    reasons = []
    for change in group:
        if change.field == "status":
            links["status"] = {"href": "/api/v3/statuses/%s"
                                       % lookups.status_id(change.new)}
            reasons.append(change.reason)
        elif change.field == "needs_you":
            prop = lookups.custom_prop(change.field, project.op_id, tid)
            body[prop] = bool(change.new)
        elif change.field in ("action", "models"):
            prop, link = lookups.option_href(change.field, change.new,
                                             project.op_id, tid, item.id)
            links[prop] = link
        elif change.field == "review_result":
            # Only ever cleared by the conductor (a moved or unbound
            # review, TH.5); the reason is posted so the reviewer sees why.
            prop, link = lookups.option_href(change.field, change.new,
                                             project.op_id, tid, item.id)
            links[prop] = link
            reasons.append(change.reason)
        elif change.field in ("est_cost", "actual_cost", "actual_tokens"):
            prop = lookups.custom_prop(change.field, project.op_id, tid)
            body[prop] = change.new
        else:
            raise ApiError(0, change.key,
                           "no live mapping for field %r" % (change.field,))
    if links:
        body["_links"] = links
    op_client.patch("/api/v3/work_packages/%s" % item.id, body)
    if reasons:
        op_client.post("/api/v3/work_packages/%s/activities" % item.id,
                       {"comment": {"raw": "\n\n".join(reasons)}})
