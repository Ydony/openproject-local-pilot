"""Build a conductor World from OpenProject (T3.2; GitHub joins in T3.3).

Reads the settings projects and their Epic/Feature/Task work packages.
HAL shapes are decoded by opl.hal (TH.7): custom fields come from the
per-project/type schema root, list and user values from `_links`, every
collection is paged with get_all, and any listing error propagates so the
caller skips the whole cycle rather than acting on a partial snapshot.
Standard library only.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace

from opl import hal
from opl.conductor import state
from opl.conductor.state import Deploy, Item, Project
from opl.conductor.rules.enforce import APPROVED_ONWARDS, RISK_RANK
from opl.openproject import ApiError

logger = logging.getLogger("opl.conductor")

# Sorted by id so a work package created mid-read lands on the last page
# instead of shifting earlier offsets.
_WP_PAGE = {"pageSize": "200", "sortBy": json.dumps([["id", "asc"]])}

# A review binds to one commit: the reviewer's comment line
# "reviewed: <full 40-hex PR head sha>" (TH.5).
_REVIEWED = re.compile(r"^reviewed:\s*([0-9a-fA-F]{40})\s*$", re.MULTILINE)

# The model role whose members may tick Merge OK (config/pm-model.toml).
OWNER_ROLE = "Owner"


def _eid(element):
    if element.get("id") is not None:
        return element["id"]
    return hal.tail(hal.link_href(element, "self") or "")


def _number(raw):
    """Float custom-field value, None when missing or unparsable."""
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _integer(raw):
    """Integer custom-field value, None when missing or unparsable."""
    if raw is None or raw == "":
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


class _Users:
    """Map user links to logins.

    `/api/v3/users` (and every user's `login`) is admin-only, so with the
    conductor's own token it answers 403. Then a link's display title is
    matched against the model's accounts: every word of the title must be
    that account's name or login (the default formats give "Claude",
    "claude" or "Claude claude"). Bot accounts have no password and the v3
    API only lets admins rename users, so a bot cannot take another's
    title; TH.7a confirms this live. Unknown users keep their title, so
    they still count as assigned but never as a model.
    """

    def __init__(self, client, model):
        self.by_id = {}
        try:
            for element in client.get_all("/api/v3/users"):
                if element.get("id") is not None and element.get("login"):
                    self.by_id[str(element["id"])] = element["login"]
        except ApiError as exc:
            if exc.status not in (401, 403):
                raise
        self.accounts = tuple((u.login, u.name)
                              for u in (getattr(model, "users", None) or ()))

    def login(self, resource, name):
        href = hal.link_href(resource, name)
        if not href:
            return None
        uid = hal.tail(href)
        if uid in self.by_id:
            return self.by_id[uid]
        title = (hal.link_title(resource, name) or "").strip()
        words = set(re.split(r"[\s,]+", title)) - {""}
        for login, display in self.accounts:
            if words and words <= {login, display}:
                return login
        return title or "user:%s" % uid


class _Schemas:
    """Custom-field name -> property per (project, type), fetched once."""

    def __init__(self, client):
        self._client = client
        self._fields = {}

    def fields(self, pid, tid):
        key = (str(pid), str(tid))
        if key not in self._fields:
            schema = self._client.get(
                "/api/v3/work_packages/schemas/%s-%s" % key) or {}
            self._fields[key] = hal.schema_fields(schema)
        return self._fields[key]


def _to_signal(value):
    if value is None:
        return None
    return state.Signal(kind=value.kind, name=value.name)


def collect_openproject(client, settings, model, now):
    """Return (projects, items) keyed by settings key and work-package id.

    `now` is currently unused (kept for staleness checks later phases may
    add). status_since comes from the latest status change in the activity
    journal, falling back to createdAt (then updatedAt) when none
    qualifies. Raises ApiError when any listing fails, so no partial world
    is ever built.
    """
    types = {}
    for element in client.get_all("/api/v3/types"):
        if element.get("name") is not None:
            types[element["name"]] = _eid(element)
    statuses = {}
    for element in client.get_all("/api/v3/statuses"):
        if element.get("name") is not None:
            statuses[element["name"]] = _eid(element)
    users = _Users(client, model)
    schemas = _Schemas(client)
    type_names = {str(v): k for k, v in types.items()}
    status_names = {str(v): k for k, v in statuses.items()}

    projects = {}
    items = {}
    risk_records = {}
    for sproject in settings.projects:
        found = client.get_all(
            "/api/v3/projects",
            {"filters": json.dumps(
                [{"identifier": {"operator": "=", "values": [sproject.key]}}])},
        )
        match = None
        for element in found:
            ident = element.get("identifier", "")
            if ident == sproject.key or element.get("name") == sproject.name:
                match = element
                break
        if match is None:
            raise ApiError(
                0, "/api/v3/projects",
                "settings project %r not found in OpenProject" % sproject.key,
            )
        pid = _eid(match)
        projects[sproject.key] = Project(
            key=sproject.key,
            op_id=int(pid) if str(pid).isdigit() else 0,
            repo=sproject.repo,
            visibility=sproject.visibility,
            has_test_env=sproject.has_test_env,
            test_signal=_to_signal(sproject.test_signal),
            prod_signal=_to_signal(sproject.prod_signal),
            at_risk=False,
        )

        owners = _owner_ids(client, pid)
        # An explicit filter replaces the API's default "open only" filter,
        # so closed items (Merged, Shipped) are listed too.
        params = dict(_WP_PAGE)
        params["filters"] = json.dumps(
            [{"project": {"operator": "=", "values": [str(pid)]}}])
        for element in client.get_all("/api/v3/work_packages", params):
            type_id = hal.tail(hal.link_href(element, "type") or "")
            type_name = type_names.get(type_id, "")
            if type_name not in ("Epic", "Feature", "Task"):
                continue
            status_id = hal.tail(hal.link_href(element, "status") or "")
            wid = int(_eid(element))
            fields = schemas.fields(pid, type_id)
            custom = {name: hal.custom_value(element, prop)
                      for name, prop in fields.items()}
            try:
                lock_version = int(element.get("lockVersion", 0))
            except (TypeError, ValueError):
                lock_version = 0
            models = custom.get("Models")
            reviewer_id = (hal.link_id(element, fields["Reviewer"])
                           if "Reviewer" in fields else None)
            # An approval on the item makes its journal load-bearing: an
            # unreadable one then skips the cycle instead of guessing.
            approving = type_name == "Task" and (
                bool(custom.get("Merge OK")) or custom.get("Review result") == "Pass")
            journal = _journal(client, element, wid, strict=approving)
            risk_records[wid] = (element, owners)
            approvals = (_approvals(journal, reviewer_id, owners)
                         if type_name == "Task" else {})
            items[wid] = Item(
                id=wid,
                project=sproject.key,
                type=type_name,
                status=status_names.get(status_id, ""),
                status_since=_status_since(journal, element, wid),
                parent_id=hal.link_id(element, "parent"),
                assignee=users.login(element, "assignee"),
                reviewer=(users.login(element, fields["Reviewer"])
                          if "Reviewer" in fields else None),
                size=custom.get("Size"),
                risk=custom.get("Risk"),
                pr_url=custom.get("PR link"),
                review_result=custom.get("Review result"),
                merge_ok=bool(custom.get("Merge OK")),
                test_result=custom.get("Test result"),
                est_cost=_number(custom.get("Est. cost")),
                actual_cost=_number(custom.get("Actual cost")),
                actual_tokens=_integer(custom.get("Actual tokens")),
                needs_you=bool(custom.get("Needs you")),
                action=custom.get("Action"),
                models=tuple(models) if isinstance(models, list) else (),
                lock_version=lock_version,
                **approvals,
            )

    # Parent and child may be on different pages: determine which journals
    # are load-bearing only after the complete item listing is available.
    _collect_risk(client, items, risk_records)

    predecessors: dict = {}
    for relation in client.get_all("/api/v3/relations"):
        frm = hal.link_id(relation, "from")
        to = hal.link_id(relation, "to")
        if frm not in items or to not in items:
            continue
        kind = str(relation.get("type", ""))
        if kind == "precedes":
            predecessors.setdefault(to, set()).add(frm)
        elif kind == "follows":
            predecessors.setdefault(frm, set()).add(to)
    for wid, preds in predecessors.items():
        item = items[wid]
        items[wid] = replace(item, predecessors=tuple(sorted(preds)))

    return projects, items


def _owner_ids(client, pid):
    """User ids holding the Owner role in a project (TH.5).

    "The owner" is whoever the project's memberships give that role, so
    the conductor needs no admin rights to recognise them. Unreadable
    memberships raise: without them no approval can be attributed.
    """
    owners = set()
    for membership in client.get_all(
            "/api/v3/memberships",
            {"filters": json.dumps(
                [{"project": {"operator": "=", "values": [str(pid)]}}])}):
        roles = hal.link(membership, "roles") or []
        if any(isinstance(r, dict) and r.get("title") == OWNER_ROLE
               for r in roles):
            principal = hal.link_id(membership, "principal")
            if principal is not None:
                owners.add(principal)
    return owners


def _journal(client, element, wid, strict):
    """The item's activity entries; [] when unreadable unless `strict`."""
    url = hal.link_href(element, "activities") or (
        "/api/v3/work_packages/%s/activities" % wid)
    try:
        return client.get_all(url)
    except ApiError as exc:
        if strict:
            raise
        logger.warning("activities for #%s unreadable: %s", wid, exc)
        return []


def _approvals(journal, reviewer_id, owners):
    """Who approved, from the journal (TH.5).

    Merge OK counts only when its latest change is by an Owner-role
    member; Review result only when its latest change is by the task's
    Reviewer. The reviewed SHA is the Reviewer's latest "reviewed: <sha>"
    comment line.
    """
    merge_entry = hal.latest_change(journal, "Merge OK")
    review_entry = hal.latest_change(journal, "Review result")
    reviewed, reviewed_at = None, None
    for entry in journal:
        if reviewer_id is None or hal.link_id(entry, "user") != reviewer_id:
            continue
        match = None
        for match in _REVIEWED.finditer(hal.comment_text(entry)):
            pass
        if match is None:
            continue
        try:
            at = hal.parse_time(entry.get("createdAt", ""))
        except (TypeError, ValueError):
            continue
        if reviewed_at is None or at >= reviewed_at:
            reviewed, reviewed_at = match.group(1).lower(), at
    return {
        "merge_ok_by_owner": (merge_entry is not None
                              and hal.link_id(merge_entry, "user") in owners),
        "review_by_reviewer": (review_entry is not None
                               and reviewer_id is not None
                               and hal.link_id(review_entry, "user") == reviewer_id),
        "reviewed_sha": reviewed,
    }


def _history_error():
    # Never embed server journal contents (which can contain private text).
    return ApiError(0, "/api/v3/work_packages", "unusable Risk approval history")


def _approved_since(journal, element):
    """First approval, not the latest Building/In-test transition.

    A later reapproval does not erase earlier approved risk. If created in
    an approved state without transitions, creation is the boundary.
    """
    try:
        changes = hal.field_changes(journal, "Status")
        for at, entry in changes:
            if hal.new_value(entry, "Status") in APPROVED_ONWARDS:
                # If already approved before the first recorded transition,
                # conservatively start at creation instead.
                if hal.old_value(entry, "Status") in APPROVED_ONWARDS:
                    break
                return at
        at = hal.parse_time(element.get("createdAt", ""))
        if at.tzinfo is None:
            raise ValueError("missing timezone")
        return at
    except (TypeError, ValueError):
        raise _history_error() from None


def _risk_approval(journal, current, approved_at, owners):
    """Replay Risk since approval, retaining the peak and lowering author.

    Raising back to the peak repairs an unauthorized lowering. Raising only
    partway does not erase it. Owner permission applies to a lowering, not
    every future edit by a builder. Empty means no recorded Risk changes.
    """
    try:
        changes = [(at, entry) for at, entry in hal.field_changes(journal, "Risk")
                   if at >= approved_at]
        value = current
        if value not in RISK_RANK:
            raise ValueError("unknown Risk")
        for _, entry in reversed(changes):
            old, new = hal.old_value(entry, "Risk"), hal.new_value(entry, "Risk")
            if old not in RISK_RANK or new not in RISK_RANK or new != value:
                raise ValueError("inconsistent Risk history")
            value = old
        highest = value
        authorized = False
        for _, entry in changes:
            new = hal.new_value(entry, "Risk")
            if RISK_RANK[new] < RISK_RANK[value]:
                authorized = hal.link_id(entry, "user") in owners
            if RISK_RANK[new] >= RISK_RANK[highest]:
                highest, authorized = new, False
            value = new
        return {"risk_highest_since_approval": highest,
                "risk_lowered_by_owner": authorized}
    except (TypeError, ValueError):
        raise _history_error() from None


def _collect_risk(client, items, records):
    """Strict history fetch for all active tasks with approved parents."""
    boundaries = {}
    for wid, item in list(items.items()):
        parent = items.get(item.parent_id)
        if (item.type != "Task" or item.status == "Merged" or parent is None
                or parent.type != "Feature" or parent.status not in APPROVED_ONWARDS):
            continue
        if parent.id not in boundaries:
            element, _ = records[parent.id]
            journal = _journal(client, element, parent.id, strict=True)
            boundaries[parent.id] = _approved_since(journal, element)
        element, owners = records[wid]
        journal = _journal(client, element, wid, strict=True)
        items[wid] = replace(item, **_risk_approval(
            journal, item.risk, boundaries[parent.id], owners))


def _status_since(journal, element, wid):
    """When the item entered its current status.

    The newest journal entry whose details (formattables such as "Status
    changed from A to B") touch Status; with none, the item has held its
    status since creation (createdAt), and updatedAt is the last resort.
    """
    entry = hal.latest_change(journal, "Status")
    for value in ((entry or {}).get("createdAt"), element.get("createdAt"),
                  element.get("updatedAt")):
        if value:
            try:
                return hal.parse_time(value)
            except (TypeError, ValueError):
                continue
    raise ApiError(0, "/api/v3/work_packages/%s" % wid,
                   "no usable timestamp for status_since")


def collect_github(gh, projects, items):
    """Collect PR states for items with pr_url and deploys per project."""
    pull_requests = {}
    for item in items.values():
        if item.pr_url and item.pr_url not in pull_requests:
            pull_requests[item.pr_url] = gh.pull_request(item.pr_url)
    deploys = []
    for project in projects.values():
        if project.test_signal is not None and project.test_signal.kind == "workflow":
            deploys.extend(gh.test_deploys(project))
        if project.prod_signal is not None:
            if project.prod_signal.kind == "environment":
                deploys.extend(gh.prod_deploys(project))
            elif project.prod_signal.kind == "workflow":
                for deploy in gh.test_deploys(project, project.prod_signal):
                    deploys.append(Deploy(project=deploy.project,
                                          target="production", at=deploy.at))
    return pull_requests, tuple(deploys)
