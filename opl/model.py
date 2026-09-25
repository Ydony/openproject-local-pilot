"""Tracker model: load and validate config/pm-model.toml.

Standard library only (needs Python 3.11+ for tomllib). Names must match
docs/DESIGN.md sections 1-2; that file changes first.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field


class ModelError(Exception):
    """The model file is malformed or breaks a stated rule."""


@dataclass(frozen=True)
class Status:
    name: str
    closed: bool
    done_ratio: int


@dataclass(frozen=True)
class Type:
    name: str
    statuses: tuple
    default_status: str


@dataclass(frozen=True)
class Role:
    name: str
    # "all" or a tuple of permission identifiers.
    permissions: object


@dataclass(frozen=True)
class Workflow:
    role: str
    type: str
    # "all" or a tuple of (from, to) status pairs.
    transitions: object


@dataclass(frozen=True)
class Field:
    name: str
    on: tuple
    format: str
    values: tuple = ()
    multi: bool = False
    admin_only: bool = False


@dataclass(frozen=True)
class ProjectField:
    name: str
    format: str


@dataclass(frozen=True)
class User:
    login: str
    name: str
    role: str
    projects: str


@dataclass(frozen=True)
class View:
    name: str
    scope: str
    filters: tuple = ()
    sort: tuple = ()
    columns: tuple = ()
    group_by: str = ""
    timeline: bool = False
    starred: bool = False
    my_page: bool = False


@dataclass(frozen=True)
class Model:
    statuses: tuple
    types: tuple
    roles: tuple
    workflows: tuple
    fields: tuple
    project_fields: tuple
    users: tuple
    versions: tuple
    views: tuple
    # Status given to items created without one (top-level key). Optional so
    # older files still load; admin_ruby falls back to "Draft" when absent.
    global_default: object = None

    def transitions(self, role, type):
        """All (from, to) pairs `role` may move `type` through.

        A workflow row matches when its role equals `role` and its type
        equals `type` or `"*"`. `transitions = "all"` expands to every
        ordered pair over the type's statuses (self-pairs included: the
        engine never queries a no-op change, so they are harmless and the
        literal reading of "all pairs" stays simple). Unknown role/type
        combinations yield the empty set.
        """
        by_type = {t.name: t for t in self.types}
        known = by_type.get(type)
        found = set()
        for row in self.workflows:
            if row.role != role or row.type not in (type, "*"):
                continue
            if row.transitions == "all":
                if known is None:
                    continue
                found.update(
                    (a, b) for a in known.statuses for b in known.statuses
                )
            else:
                found.update(row.transitions)
        return found


# Built-in (non-custom) names views may reference, from DESIGN.md section 1:
# id/project/type/parent/subject/status are work-package identity, priority
# is built-in on features, progress is derived, assignee is built-in,
# version is the Bucket assignment.
BUILTIN_VIEW_FIELDS = frozenset(
    {
        "id", "project", "type", "parent", "subject", "status",
        "priority", "progress", "assignee", "version",
    }
)


def _req(mapping, key, where):
    if not isinstance(mapping, dict) or key not in mapping:
        raise ModelError("%s: missing required key %r" % (where, key))
    return mapping[key]


def _str_list(value, where):
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ModelError("%s: expected a list of strings" % where)
    return tuple(value)


def load(path):
    """Read a tracker model file, raising ModelError naming any problem."""
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ModelError("cannot load model file %s: %s" % (path, exc))
    if not isinstance(data, dict):
        raise ModelError("model file %s: top level must be a table" % path)

    # Bare keys after [[...]] tables parse INTO the last table (TOML rules),
    # so a misplaced `versions` would silently become user data. Reject it.
    for table in ("status", "type", "role", "workflow", "field",
                  "project_field", "user", "view"):
        entries = data.get(table, [])
        if not isinstance(entries, list):
            raise ModelError("model file %s: [[%s]] must be a list" % (path, table))
        for entry in entries:
            if isinstance(entry, dict) and "versions" in entry:
                raise ModelError(
                    "model file %s: `versions` belongs at the top level, "
                    "not inside [[%s]]" % (path, table))

    raw_statuses = data.get("status", [])
    statuses = tuple(
        Status(
            name=_req(s, "name", "status"),
            closed=bool(_req(s, "closed", "status %r" % s.get("name"))),
            done_ratio=_req(s, "done_ratio", "status %r" % s.get("name")),
        )
        for s in raw_statuses
    )
    status_names = {s.name for s in statuses}
    global_default = data.get("global_default")
    if global_default is not None and global_default not in status_names:
        raise ModelError(
            "global_default %r is not a known status" % (global_default,))

    types = tuple(
        Type(
            name=_req(t, "name", "type"),
            statuses=_str_list(_req(t, "statuses", "type"), "type statuses"),
            default_status=_req(t, "default_status", "type"),
        )
        for t in data.get("type", [])
    )
    for t in types:
        for name in t.statuses:
            if name not in status_names:
                raise ModelError(
                    "type %r lists unknown status %r" % (t.name, name)
                )
        if t.default_status not in t.statuses:
            raise ModelError(
                "type %r default status %r is not in its status list"
                % (t.name, t.default_status)
            )
    type_names = {t.name for t in types}

    def role_permissions(raw, rname):
        if isinstance(raw, str):
            if raw != "all":
                raise ModelError(
                    "role %r permissions string must be \"all\", got %r"
                    % (rname, raw)
                )
            return raw
        return _str_list(raw, "role %r permissions" % rname)

    roles = tuple(
        Role(
            name=_req(r, "name", "role"),
            permissions=role_permissions(
                _req(r, "permissions", "role"), _req(r, "name", "role")
            ),
        )
        for r in data.get("role", [])
    )
    role_names = {r.name for r in roles}

    all_status_names = set(status_names)
    workflows = []
    for w in data.get("workflow", []):
        role = _req(w, "role", "workflow")
        wtype = _req(w, "type", "workflow")
        raw = _req(w, "transitions", "workflow %s/%s" % (role, wtype))
        if isinstance(raw, str):
            if raw != "all":
                raise ModelError(
                    "workflow %s/%s: transitions string must be \"all\"" % (role, wtype)
                )
            transitions = "all"
        else:
            pairs = []
            for pair in raw:
                if (
                    not isinstance(pair, list)
                    or len(pair) != 2
                    or not all(isinstance(v, str) for v in pair)
                ):
                    raise ModelError(
                        "workflow %s/%s: transitions must be [from, to] pairs"
                        % (role, wtype)
                    )
                pairs.append((pair[0], pair[1]))
            transitions = tuple(pairs)
            if wtype == "*":
                allowed = set(all_status_names)
            elif wtype in type_names:
                allowed = set(next(t.statuses for t in types if t.name == wtype))
            else:
                allowed = set()
            for frm, to in transitions:
                if to not in allowed or (
                    frm not in allowed
                    and frm != global_default
                ):
                    raise ModelError(
                        "workflow %s/%s: transition (%r, %r) uses a status "
                        "not in that type's list" % (role, wtype, frm, to)
                    )
        workflows.append(Workflow(role=role, type=wtype, transitions=transitions))
    workflows = tuple(workflows)

    fields = tuple(
        Field(
            name=_req(f, "name", "field"),
            on=tuple(_str_list(_req(f, "on", "field"), "field on")),
            format=_req(f, "format", "field"),
            values=tuple(f.get("values", [])),
            multi=bool(f.get("multi", False)),
            admin_only=bool(f.get("admin_only", False)),
        )
        for f in data.get("field", [])
    )
    for f in fields:
        for tname in f.on:
            if tname not in type_names:
                raise ModelError(
                    "field %r is on unknown type %r" % (f.name, tname)
                )

    project_fields = tuple(
        ProjectField(
            name=_req(f, "name", "project field"),
            format=_req(f, "format", "project field"),
        )
        for f in data.get("project_field", [])
    )

    users = tuple(
        User(
            login=_req(u, "login", "user"),
            name=_req(u, "name", "user"),
            role=_req(u, "role", "user"),
            projects=_req(u, "projects", "user"),
        )
        for u in data.get("user", [])
    )
    for u in users:
        if u.role not in role_names:
            raise ModelError(
                "user %r has unknown role %r" % (u.login, u.role)
            )
        if u.projects not in ("all", "public"):
            raise ModelError(
                "user %r projects must be \"all\" or \"public\", got %r"
                % (u.login, u.projects)
            )

    versions_raw = data.get("versions", [])
    versions = tuple(_str_list(versions_raw, "versions"))

    known_view_fields = (
        {f.name for f in fields}
        | {f.name for f in project_fields}
        | set(BUILTIN_VIEW_FIELDS)
    )
    views = []
    for v in data.get("view", []):
        name = _req(v, "name", "view")

        def check_field(fname, where):
            if fname not in known_view_fields:
                raise ModelError(
                    "view %r %s uses unknown field %r" % (name, where, fname)
                )

        for f in v.get("filters", []):
            check_field(_req(f, "field", "view filter"), "filter")
        for entry in v.get("sort", []):
            if not isinstance(entry, list) or not entry:
                raise ModelError("view %r has a malformed sort entry" % name)
            check_field(entry[0], "sort")
        for col in v.get("columns", []):
            check_field(col, "column")
        group_by = v.get("group_by", "")
        if group_by:
            check_field(group_by, "group_by")
        views.append(
            View(
                name=name,
                scope=_req(v, "scope", "view"),
                filters=tuple(
                    (f.get("field"), f.get("op"), tuple(f.get("values", [])))
                    for f in v.get("filters", [])
                ),
                sort=tuple(tuple(e) for e in v.get("sort", [])),
                columns=tuple(v.get("columns", [])),
                group_by=group_by,
                timeline=bool(v.get("timeline", False)),
                starred=bool(v.get("starred", False)),
                my_page=bool(v.get("my_page", False)),
            )
        )
    views = tuple(views)

    return Model(
        statuses=statuses,
        types=types,
        roles=roles,
        workflows=workflows,
        fields=fields,
        project_fields=project_fields,
        users=users,
        versions=versions,
        views=views,
        global_default=global_default,
    )
