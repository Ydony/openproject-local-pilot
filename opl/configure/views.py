"""Apply saved views (queries) and pin Needs me to the owner's My page.

Idempotent and dry-run capable like apply_api. Query payloads use the live
OpenProject 17 HAL link form (T5.1 S1, ground truth from the first live run):
filters carry `_links.filter` / `_links.operator` (plus `_links.values` for
link-valued values and a top-level `values` list for scalars), columns /
sort / group-by / project live under `_links`. The grid widget shape is
still best-effort and flagged for live proof. Standard library only.
"""

from __future__ import annotations

from opl import hal
from opl.configure.common import _eid, find_user, project_ids
from opl.openproject import ApiError

# Built-in query field names pass through; anything else is a custom field
# resolved to customField{id} through the work-package schemas.
# `author` is built-in for the My page repair queries (work packages
# created by me); it behaves like `assignee` (a user link).
BUILTIN_QUERY_FIELDS = frozenset(
    {
        "id", "project", "type", "parent", "subject", "status",
        "priority", "progress", "assignee", "author", "version",
    }
)

# Model column names that differ from OpenProject query column ids.
# Verified live against v17 (query form allowed columns/groupBys):
# `percentageDone` and `parent` exist; versions are `targetVersions`
# (plural, a work package can have several) for both columns and grouping.
COLUMN_MAP = {
    "progress": "percentageDone",
    "version": "targetVersions",
    "parent": "parent",
}

# Model filter names that differ from query filter ids.
# UNVERIFIED live: `version` is assumed to be the `targetVersion` filter
# seen in a real stored query (alternatively `version`).
FILTER_MAP = {
    "version": "targetVersion",
}

# Model operator -> query operator id used in the operator href.
# `=` is `%3D` (URL-encoded `=`) and the open-status shorthand is `o`,
# both seen in a real stored query from live v17.
OPERATOR_MAP = {
    "=": "%3D",
    "open": "o",
}

# Filter fields whose values are links resolved by name to hrefs. Everything
# else (custom booleans, custom lists, plain strings) is sent as a top-level
# scalar `values` list.
LINK_VALUE_FIELDS = frozenset(
    {"type", "status", "version", "project", "assignee", "author"}
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
    if name in custom_ids:
        return custom_ids[name]
    if name in BUILTIN_QUERY_FIELDS:
        return name
    raise ApiError(0, "/api/v3/work_packages/schemas",
                   "query field %r is neither built-in nor a custom field" % name)


def _query_id(name, custom_ids, kind="column"):
    """Query id for a model field name (custom fields stay customField{id}).

    `kind` is "column", "filter" or "group": columns apply COLUMN_MAP,
    filters apply FILTER_MAP, groups apply COLUMN_MAP. Anything else passes
    through when built-in, else resolves via the work-package schemas.
    """
    if name in custom_ids:
        return custom_ids[name]
    if kind == "filter" and name in FILTER_MAP:
        return FILTER_MAP[name]
    if kind in ("column", "group", "sort") and name in COLUMN_MAP:
        return COLUMN_MAP[name]
    if name in BUILTIN_QUERY_FIELDS:
        return name
    raise ApiError(0, "/api/v3/work_packages/schemas",
                   "query field %r is neither built-in nor a custom field" % name)


def _operator_id(op):
    return OPERATOR_MAP.get(op, op)


def _scalar_value(value):
    """Map model scalar filter values to the query API form.

    Custom booleans use `t`/`f` (live v17: "Needs you" true -> `"t"`).
    """
    text = str(value)
    if text.lower() == "true":
        return "t"
    if text.lower() == "false":
        return "f"
    return text


def _query_body(view, custom_ids, lookups=None):
    """Build a HAL link-form create/patch body for `view`.

    `lookups` maps link-valued filter fields to name->href dicts, e.g.
    `{"type": {"Feature": "/api/v3/types/12"}, ...}`. Missing entries for a
    link-valued field with values raise ApiError naming the unknown value.
    """
    lookups = lookups or {}
    filters = []
    for field, op, values in view.filters:
        fid = _query_id(field, custom_ids, kind="filter")
        entry = {
            "_links": {
                "filter": {"href": "/api/v3/queries/filters/%s" % fid},
                "operator": {"href": "/api/v3/queries/operators/%s" % _operator_id(op)},
            }
        }
        if values:
            if field in LINK_VALUE_FIELDS:
                resolved = lookups.get(field) or {}
                hrefs = []
                for wanted in values:
                    href = resolved.get(str(wanted))
                    if href is None:
                        raise ApiError(
                            0, "/api/v3/queries/filters/%s" % fid,
                            "query filter %r value %r not found" % (field, wanted),
                        )
                    hrefs.append({"href": href})
                entry["_links"]["values"] = hrefs
            else:
                entry["values"] = [_scalar_value(v) for v in values]
        filters.append(entry)
    links = {
        "columns": [
            {"href": "/api/v3/queries/columns/%s" % _query_id(c, custom_ids, kind="column")}
            for c in view.columns
        ],
    }
    if view.sort:
        links["sortBy"] = [
            {"href": "/api/v3/queries/sort_bys/%s-%s"
             % (_query_id(field, custom_ids, kind="sort"), str(direction).lower())}
            for field, direction in view.sort
        ]
    if view.group_by:
        links["groupBy"] = {
            "href": "/api/v3/queries/group_bys/%s"
            % _query_id(view.group_by, custom_ids, kind="group")
        }
    return {
        "name": view.name,
        "public": True,
        "starred": bool(view.starred),
        "timelineVisible": bool(view.timeline),
        "filters": filters,
        "_links": links,
    }


def _norm(element):
    """Comparable subset of a stored query, by hrefs we manage.

    Both freshly built bodies and live read-backs share this shape: filters
    as (filter href, operator href, value hrefs, scalar values) tuples and
    columns/sort/group/project as hrefs. A second run therefore makes zero
    writes.
    """
    get = element.get
    links = element.get("_links") or {}
    norm_filters = []
    for entry in (get("filters") or []):
        if not isinstance(entry, dict):
            continue
        flinks = entry.get("_links") or {}
        fhref = (flinks.get("filter") or {}).get("href") if isinstance(
            flinks.get("filter"), dict) else None
        ohref = (flinks.get("operator") or {}).get("href") if isinstance(
            flinks.get("operator"), dict) else None
        # Old guessed shape ({"field": {"operator", "values"}}) has no
        # _links: keep its raw form so a legacy stored query never
        # compares equal to a HAL body (it gets updated once).
        if fhref is None and ohref is None:
            norm_filters.append(("legacy", entry))
            continue
        raw_values = flinks.get("values") or []
        if isinstance(raw_values, dict):
            raw_values = [raw_values]
        vhrefs = tuple(
            v.get("href") for v in raw_values
            if isinstance(v, dict) and v.get("href") is not None
        )
        # We send booleans as "t"/"f"; live v17 reads them back as JSON
        # true/false. Compare them as equal so a second run makes no write.
        vals = tuple({True: "t", False: "f"}.get(v, v) if isinstance(v, bool) else v
                     for v in (entry.get("values") or []))
        norm_filters.append((fhref, ohref, vhrefs, vals))
    cols = links.get("columns") or []
    if isinstance(cols, dict):
        cols = [cols]
    col_hrefs = tuple(
        c.get("href") for c in cols if isinstance(c, dict))
    # Back-compat: a legacy top-level string list never equals hrefs.
    if not col_hrefs and isinstance(get("columns"), list):
        col_hrefs = ("legacy", tuple(get("columns") or []))
    sorts = links.get("sortBy") or []
    if isinstance(sorts, dict):
        sorts = [sorts]
    sort_hrefs = tuple(
        s.get("href") for s in sorts if isinstance(s, dict))
    if not sort_hrefs and isinstance(get("orders"), list):
        sort_hrefs = ("legacy", tuple(tuple(p) for p in get("orders") or []))
    group = links.get("groupBy")
    group_href = group.get("href") if isinstance(group, dict) else None
    if group_href is None and get("groupBy") is not None:
        group_href = ("legacy", get("groupBy"))
    project = links.get("project")
    project_href = project.get("href") if isinstance(project, dict) else None
    # A legacy top-level {"project": {"href"}} body (pre-HAL) is also legacy.
    if project_href is None and isinstance(get("project"), dict):
        project_href = get("project").get("href")
    timeline = get("timelineVisible")
    if timeline is None:
        timeline = False
    return {
        "name": get("name"),
        "public": get("public"),
        "starred": get("starred"),
        "timelineVisible": timeline,
        "filters": norm_filters,
        "columns": col_hrefs,
        "sortBy": sort_hrefs,
        "groupBy": group_href,
        "project": project_href,
    }


def _value_lookups(client, model, pids):
    """Name -> href maps for link-valued filter values.

    Only fields the model actually filters by are resolved, so fakes and live
    instances only need the endpoints the views use. Type/status come from
    their collections by name; version/project/user are resolved the same way
    when a view filters by them.
    """
    wanted_fields = set()
    for view in model.views:
        for field, _op, values in view.filters:
            if values and field in LINK_VALUE_FIELDS:
                wanted_fields.add(field)
    lookups = {}
    if "type" in wanted_fields:
        mapping = {}
        for element in client.get_all("/api/v3/types"):
            name = element.get("name")
            href = hal.link_href(element, "self") or (
                "/api/v3/types/%s" % _eid(element) if _eid(element) is not None else None)
            if name is not None and href:
                mapping[str(name)] = href
        lookups["type"] = mapping
    if "status" in wanted_fields:
        mapping = {}
        for element in client.get_all("/api/v3/statuses"):
            name = element.get("name")
            href = hal.link_href(element, "self") or (
                "/api/v3/statuses/%s" % _eid(element) if _eid(element) is not None else None)
            if name is not None and href:
                mapping[str(name)] = href
        lookups["status"] = mapping
    if "version" in wanted_fields:
        mapping = {}
        try:
            elements = client.get_all("/api/v3/versions")
        except ApiError:
            elements = []
        for element in elements:
            name = element.get("name")
            href = hal.link_href(element, "self") or (
                "/api/v3/versions/%s" % _eid(element) if _eid(element) is not None else None)
            if name is not None and href:
                mapping[str(name)] = href
        lookups["version"] = mapping
    if "project" in wanted_fields:
        inv = {str(v): k for k, v in pids.items()}
        lookups["project"] = {
            name: "/api/v3/projects/%s" % pid for pid, name in inv.items()
        }
        # Also map by settings display names for convenience.
        lookups["project"] = dict(lookups["project"])
    if "assignee" in wanted_fields or "author" in wanted_fields:
        mapping = _users_by_login(client)
        if "assignee" in wanted_fields:
            lookups["assignee"] = mapping
        if "author" in wanted_fields:
            lookups["author"] = mapping
    return lookups


def _users_by_login(client):
    """Login -> user href map, reused by _value_lookups and My page repair."""
    mapping = {}
    for element in client.get_all("/api/v3/users"):
        login = element.get("login")
        href = hal.link_href(element, "self") or (
            "/api/v3/users/%s" % _eid(element) if _eid(element) is not None else None)
        if login is not None and href:
            mapping[str(login)] = href
    return mapping


def _repair_view_for(widget, owner_login):
    """Synthetic repair view reusing the Model.View shape and _query_body.

    OpenProject's two My page defaults filter open work packages assigned
    to me / created by me. The hint comes from the widget's surviving
    options text; unknown widgets default to assigned-to-me.
    """
    # Local import avoids a cycle (opl.model does not import views).
    from opl.model import View

    opts = (widget.get("options") or {})
    hint = " ".join([str(widget.get("identifier", ""))]
                    + [str(v) for v in opts.values()]).lower()
    if "creat" in hint or "author" in hint:
        filters = (("author", "=", (owner_login,)), ("status", "open", ()))
        name = "My page repair (created by me)"
    else:
        filters = (("assignee", "=", (owner_login,)), ("status", "open", ()))
        name = "My page repair (assigned to me)"
    return View(
        name=name,
        scope="global",
        filters=filters,
        sort=(),
        columns=("id", "subject", "status"),
        group_by="",
        timeline=False,
        starred=False,
        my_page=False,
    )


def _is_wp_widget(widget):
    """A work-package widget that owns a query: OpenProject's defaults
    (`work_packages_assigned`, `work_packages_created`, ...) and pinned
    tables (`work_packages_table`) alike."""
    return str(widget.get("identifier", "")).startswith("work_packages_")


def _query_is_missing(client, qid):
    """True when GET /api/v3/queries/<id> does not resolve (read-back)."""
    try:
        client.get("/api/v3/queries/%s" % qid)
    except ApiError:
        return True
    return False


def apply_views(client, model, settings, dry_run=False, repair_my_page=False):
    """Create/update the model's views; return human-readable actions.

    `repair_my_page` recreates a hidden query for any My page
    work-package widget whose query is gone, reusing _query_body, and
    points the widget at it before pinning.
    """
    actions = []

    def write(desc, func, *args):
        actions.append(desc)
        if not dry_run:
            return func(*args)
        return None

    try:
        pids = project_ids(client, settings)
    except ApiError:
        if not dry_run:
            raise
        # Fresh instance dry run: no projects yet; plan with placeholders
        # and continue instead of stopping.
        pids = {p.key: "new:%s" % p.key for p in settings.projects}
    try:
        custom_ids = _custom_ids(client, pids)
    except ApiError:
        if not dry_run:
            raise
        custom_ids = {}
    # Dry-run fallback: unknown custom fields map to their own names so the
    # plan can be built before the admin script has created them.
    if dry_run:
        for field in getattr(model, "fields", ()):
            custom_ids.setdefault(field.name, field.name)
    try:
        lookups = _value_lookups(client, model, pids)
    except ApiError:
        if not dry_run:
            raise
        lookups = {}

    queries = {}
    for element in client.get_all("/api/v3/queries"):
        links = element.get("_links", {})
        project = links.get("project", {}).get("href")
        if not isinstance(project, str):
            # Legacy top-level project link (pre-HAL bodies).
            top = element.get("project")
            project = top.get("href") if isinstance(top, dict) else None
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
            try:
                body = _query_body(view, custom_ids, lookups)
            except ApiError:
                if not dry_run:
                    raise
                # Fresh dry run without live ids: plan with placeholder hrefs
                # (action descriptions stay identical to the live plan).
                fallback_lookups = dict(lookups)
                for field, _op, values in view.filters:
                    if field in LINK_VALUE_FIELDS and values:
                        mapping = dict(fallback_lookups.get(field) or {})
                        for wanted in values:
                            mapping.setdefault(
                                str(wanted),
                                "/api/v3/%s/%s" % (field, str(wanted)))
                        fallback_lookups[field] = mapping
                fallback_custom = dict(custom_ids)
                for field, _op, _values in view.filters:
                    fallback_custom.setdefault(field, field)
                for col in list(view.columns) + ([view.group_by] if view.group_by else []):
                    if col:
                        fallback_custom.setdefault(col, col)
                body = _query_body(view, fallback_custom, fallback_lookups)
            if pid is not None:
                body["_links"]["project"] = {"href": "/api/v3/projects/%s" % pid}
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
            _pin_my_page(client, settings, view, query_ids, write, dry_run,
                         custom_ids, lookups, repair_my_page)

    return actions


def _pin_my_page(client, settings, view, query_ids, write, dry_run,
                 custom_ids=None, lookups=None, repair_my_page=False):
    """Pin a global view to the owner's My page grid.

    Live v17 shape: the grid's scope is `_links.scope.href == "/my/page"`
    (the page exists once the owner has opened it); a table widget is
    `{"identifier": "work_packages_table", "options": {"queryId": "<id>"}}`
    placed by start/end row/column, and widgets are changed by PATCHing the
    whole grid (there is no widgets sub-resource).

    Existing widgets keep their `id` (OpenProject replaces id-less
    widgets, destroying the queries those work-package widgets own).
    After the write every work-package widget's `queryId` must still
    resolve via GET /api/v3/queries/<id>, else ApiError is raised.
    With `repair_my_page`, a widget whose query is gone gets a fresh
    hidden query built with _query_body before the pin PATCH.
    """
    owner = find_user(client, settings.openproject.owner_login)
    if owner is None:
        raise ApiError(0, "/api/v3/users",
                       "owner account %r not found"
                       % settings.openproject.owner_login)
    mine = None
    for grid in client.get_all("/api/v3/grids"):
        scope = ((grid.get("_links") or {}).get("scope") or {}).get("href", "")
        if scope == "/my/page":
            mine = grid
            break
    if mine is None:
        if dry_run:
            write("pin %s on My page" % view.name, None)
            return
        raise ApiError(0, "/api/v3/grids",
                       "owner My page grid not found: open My page once, then re-run")
    qid = query_ids.get((view.name, None))
    widgets = mine.get("widgets", [])
    # Keep each widget's `id` (plus everything except HAL links) so
    # OpenProject updates rather than replaces it. Dropping `id` makes
    # OpenProject destroy the old widgets and the queries they own.
    keep = [{k: w[k] for k in w if k != "_links"} for w in widgets]
    grid_href = (mine.get("_links") or {}).get("self", {}).get("href") or (
        "/api/v3/grids/%s" % _eid(mine))
    owner_login = settings.openproject.owner_login

    def _widget_query_id(widget):
        return (widget.get("options") or {}).get("queryId")

    # Repair pass (before pin): point broken work-package widgets at a
    # fresh hidden query built with the shared _query_body helper. Reads
    # are allowed in dry runs (only writes are skipped), so the plan
    # matches the live run.
    repaired = False
    if repair_my_page:
        live_lookups = dict(lookups or {})
        live_custom = dict(custom_ids or {})
        for widget in keep:
            if not _is_wp_widget(widget):
                continue
            current_qid = _widget_query_id(widget)
            if current_qid is None or not _query_is_missing(client, current_qid):
                continue
            desc = "repair My page widget (query %s gone)" % current_qid
            if dry_run:
                write(desc, None)
                widget.setdefault("options", {})["queryId"] = "repaired:%s" % current_qid
                repaired = True
                continue
            if "assignee" not in live_lookups or "author" not in live_lookups:
                users = _users_by_login(client)
                live_lookups.setdefault("assignee", users)
                live_lookups.setdefault("author", users)
            repair_view = _repair_view_for(widget, owner_login)
            body = _query_body(repair_view, live_custom, live_lookups)
            body["hidden"] = True
            body["public"] = False
            created = write(desc, client.post, "/api/v3/queries", body)
            new_id = _eid(created) if created is not None else None
            widget.setdefault("options", {})["queryId"] = str(new_id)
            repaired = True

    already_pinned = any(
        str(_widget_query_id(w)) == str(qid) for w in keep
    )
    if already_pinned and not repaired:
        _verify_my_page_queries(client, keep, dry_run)
        return
    if not already_pinned:
        rows = mine.get("rowCount") or max(
            [w.get("endRow", 1) - 1 for w in keep] or [0])
        cols = mine.get("columnCount") or 1
        keep.append({
            "identifier": "work_packages_table",
            "startRow": rows + 1, "endRow": rows + 2,
            "startColumn": 1, "endColumn": cols + 1,
            "options": {"name": view.name, "queryId": str(qid)},
        })
        rows += 1
    else:
        rows = mine.get("rowCount") or max(
            [w.get("endRow", 1) - 1 for w in keep] or [0])
        cols = mine.get("columnCount") or 1
    write(
        "pin %s on My page" % view.name,
        client.patch,
        grid_href,
        {"rowCount": rows, "columnCount": cols, "widgets": keep},
    )
    _verify_my_page_queries(client, keep, dry_run)


def _verify_my_page_queries(client, widgets, dry_run=False):
    """Read-back check: every work-package widget's query must resolve."""
    if dry_run:
        return
    for widget in widgets:
        if not _is_wp_widget(widget):
            continue
        qid = (widget.get("options") or {}).get("queryId")
        if qid is None:
            continue
        try:
            client.get("/api/v3/queries/%s" % qid)
        except ApiError as exc:
            raise ApiError(
                exc.status if isinstance(exc, ApiError) else 0,
                "/api/v3/queries/%s" % qid,
                "My page widget query %s no longer resolves after pin: %s"
                % (qid, exc),
            )
