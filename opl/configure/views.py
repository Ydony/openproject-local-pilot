"""Apply saved views (queries) and pin Needs me to the owner's My page.

Idempotent and dry-run capable like apply_api. Query payload shapes follow
the API v3 queries/grids conventions; exact key names (orders, timeline
flags, grid widget shape) are best-available and flagged for live proof in
T1.L — they live in small mapping helpers so adjusting them is trivial.
Standard library only.
"""

from __future__ import annotations

from opl import hal
from opl.configure.common import _eid, find_user, project_ids
from opl.openproject import ApiError

# Built-in query field names pass through; anything else is a custom field
# resolved to customField{id} through the work-package schemas.
BUILTIN_QUERY_FIELDS = frozenset(
    {
        "id", "project", "type", "parent", "subject", "status",
        "priority", "progress", "assignee", "version",
    }
)


def _custom_ids(client, pids):
    """Custom-field name -> property across the model's types.

    There is no global work-package schema: fields are read from each
    per-project/type schema root (`/schemas/{project}-{type}`) and merged;
    a field's property is the same in every project (TH.7).
    """
    tids = [_eid(e) for e in client.get_all("/api/v3/types")
            if e.get("name") in ("Epic", "Feature", "Task")]
    ids = {}
    for pid in sorted({str(p) for p in pids.values()}):
        for tid in tids:
            schema = client.get(
                "/api/v3/work_packages/schemas/%s-%s" % (pid, tid)) or {}
            for name, prop in hal.schema_fields(schema).items():
                ids.setdefault(name, prop)
    return ids


def _map_field(name, custom_ids):
    if name in BUILTIN_QUERY_FIELDS:
        return name
    if name in custom_ids:
        return custom_ids[name]
    raise ApiError(0, "/api/v3/work_packages/schemas",
                   "query field %r is neither built-in nor a custom field" % name)


def _query_body(view, custom_ids):
    body = {
        "name": view.name,
        "public": True,
        "starred": bool(view.starred),
        "filters": [
            {_map_field(f[0], custom_ids): {"operator": f[1], "values": list(f[2])}}
            for f in view.filters
        ],
        "orders": [list(pair) for pair in view.sort],
        "columns": [_map_field(c, custom_ids) for c in view.columns],
    }
    if view.group_by:
        body["groupBy"] = _map_field(view.group_by, custom_ids)
    if view.timeline:
        body["timelineVisible"] = True
    return body


def _norm(element):
    """Comparable subset of a stored query (what we manage)."""
    get = element.get
    return {
        "name": get("name"),
        "public": get("public"),
        "starred": get("starred"),
        "filters": get("filters"),
        "orders": get("orders"),
        "columns": get("columns"),
        "groupBy": get("groupBy"),
        "timelineVisible": get("timelineVisible"),
    }


def apply_views(client, model, settings, dry_run=False):
    """Create/update the model's views; return human-readable actions."""
    actions = []

    def write(desc, func, *args):
        actions.append(desc)
        if not dry_run:
            return func(*args)
        return None

    pids = project_ids(client, settings)
    custom_ids = _custom_ids(client, pids)

    queries = {}
    for element in client.get_all("/api/v3/queries"):
        links = element.get("_links", {})
        project = links.get("project", {}).get("href")
        pid = project.rstrip("/").rsplit("/", 1)[-1] if project else None
        queries[(element.get("name"), pid)] = element

    query_ids = {}
    for view in model.views:
        targets = (
            [(None, "global")]
            if view.scope == "global"
            else [(str(pids[key]), key) for key in pids]
        )
        for pid, label in targets:
            body = _query_body(view, custom_ids)
            if pid is not None:
                body["project"] = {"href": "/api/v3/projects/%s" % pid}
            current = queries.get((view.name, pid))
            if current is None:
                desc = "create view %s (%s)" % (view.name, label)
                created = write(desc, client.post, "/api/v3/queries", body)
                query_ids[(view.name, pid)] = (
                    _eid(created) if created is not None else None
                )
            elif _norm(current) != _norm(body):
                desc = "update view %s (%s)" % (view.name, label)
                write(desc, client.patch,
                      "/api/v3/queries/%s" % _eid(current), body)
                query_ids[(view.name, pid)] = _eid(current)
            else:
                query_ids[(view.name, pid)] = _eid(current)

    for view in model.views:
        if view.my_page:
            _pin_my_page(client, settings, view, query_ids, write, dry_run)

    return actions


def _pin_my_page(client, settings, view, query_ids, write, dry_run):
    """Pin a global view to the owner's My page grid (best-effort shapes)."""
    owner = find_user(client, settings.openproject.owner_login)
    if owner is None:
        raise ApiError(0, "/api/v3/users",
                       "owner account %r not found"
                       % settings.openproject.owner_login)
    grids = client.get_all("/api/v3/grids")
    mine = None
    for grid in grids:
        scope = str(grid.get("scope", ""))
        if scope.lower().replace("_", " ") == "my page":
            mine = grid
            break
    if mine is None:
        raise ApiError(0, "/api/v3/grids", "owner My page grid not found")
    qid = query_ids.get((view.name, None))
    widgets = mine.get("widgets", [])
    for widget in widgets:
        links = widget.get("_links", {})
        # Accept the query link nested HAL-style or top-level: the exact
        # widget shape is the least certain part (live proof in T1.L).
        candidates = [links.get("query", {}).get("href", ""),
                      widget.get("query", {}).get("href", "")]
        if any(str(href).endswith("/%s" % qid) for href in candidates):
            return
    grid_href = (mine.get("_links") or {}).get("self", {}).get("href") or (
        "/api/v3/grids/%s" % _eid(mine))
    write(
        "pin %s on My page" % view.name,
        client.post,
        grid_href.rstrip("/") + "/widgets",
        {"query": {"href": "/api/v3/queries/%s" % qid}},
    )
