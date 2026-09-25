"""Apply projects, users, memberships, versions and Maintenance items via API v3.

Runs after the admin rails script (roles, types, statuses and fields already
exist). Idempotent: every write is preceded by a read and PATCHes only fire
on value differences, so a second run against the same instance performs zero
POST/PATCH/DELETE calls. `dry_run=True` plans without writing and returns the
same action list.

Standard library only. Shapes assumed (field ids, nested version route,
membership/project links) follow long-standing API v3 patterns; the live run
in T1.L proves or adjusts them.
"""

from __future__ import annotations

import json
import secrets

from opl import hal
from opl.configure.common import (_eid, find_user as _find_user,
                                  project_field_ids, project_schema)
from opl.openproject import ApiError


def _tail(href):
    return hal.tail(href)


def _link(obj, name):
    return hal.link_href(obj, name)


def apply_api(client, model, settings, dry_run=False):
    """Ensure the model exists in OpenProject; return human-readable actions."""
    actions = []

    def write(desc, func, *args):
        actions.append(desc)
        if not dry_run:
            func(*args)

    roles = {}
    for element in client.get_all("/api/v3/roles"):
        if element.get("name") is not None:
            roles[element["name"]] = _eid(element)

    def need_role(name):
        if name not in roles:
            raise ApiError(
                0, "/api/v3/roles",
                "role %r not found - run the admin rails step first" % name,
            )
        return roles[name]

    types = {}
    for element in client.get_all("/api/v3/types"):
        if element.get("name") is not None:
            types[element["name"]] = _eid(element)

    def need_type(name):
        if name not in types:
            raise ApiError(
                0, "/api/v3/types",
                "type %r not found - run the admin rails step first" % name,
            )
        return types[name]

    statuses = {}
    for element in client.get_all("/api/v3/statuses"):
        if element.get("name") is not None:
            statuses[element["name"]] = _eid(element)

    def need_status(name):
        if name not in statuses:
            raise ApiError(
                0, "/api/v3/statuses",
                "status %r not found - run the admin rails step first" % name,
            )
        return statuses[name]

    schema = project_schema(client)
    field_ids = project_field_ids(client, schema)
    for fname in ("Repo", "Visibility"):
        if fname not in field_ids:
            raise ApiError(
                0, "/api/v3/projects/schema",
                "project field %r not exposed by the schema" % fname,
            )
    # Visibility is a list field: read as a link title, written as an
    # option link. Repo (format "link", a URL) is a plain root string.
    # A plain schema carries no allowed values (OpenProject fills them in
    # only on a form), so the options come from a project's edit form
    # when the schema has none (TH.23, Codex N3).
    visibility_options = hal.allowed_options(schema, field_ids["Visibility"])

    def options_for(pid):
        if not visibility_options and pid is not None:
            form = client.post("/api/v3/projects/%s/form" % pid, {}) or {}
            visibility_options.update(hal.allowed_options(
                (form.get("_embedded") or {}).get("schema") or {},
                field_ids["Visibility"]))
        return visibility_options

    # Users (global). Bot users are created active with a random password
    # that is discarded immediately and never stored, printed or returned:
    # the account exists so memberships and tokens can attach to it, but the
    # password is unusable by anyone. API tokens stay manual per T1.O.
    user_ids = {}
    for user in model.users:
        element = _find_user(client, user.login)
        if element is None:
            actions.append("create user %s" % user.login)
            created = None
            if not dry_run:
                created = client.post(
                    "/api/v3/users",
                    {
                        "login": user.login,
                        "firstName": user.name,
                        "lastName": user.login,
                        "email": "%s@%s" % (user.login, settings.users_email_domain),
                        "status": "active",
                        "password": secrets.token_urlsafe(24),
                    },
                )
            user_ids[user.login] = (created or {}).get("id")
        else:
            user_ids[user.login] = _eid(element)

    owner = _find_user(client, settings.openproject.owner_login)
    if owner is None:
        raise ApiError(
            0, "/api/v3/users",
            "owner account %r not found" % settings.openproject.owner_login,
        )
    user_ids[settings.openproject.owner_login] = _eid(owner)

    for project in settings.projects:
        found = client.get_all(
            "/api/v3/projects",
            {"filters": json.dumps([{"name": {"operator": "=", "values": [project.name]}}])},
        )
        current = None
        for element in found:
            if element.get("name") == project.name:
                current = element
                break
        if current is None:
            actions.append("create project %s" % project.name)
            if not dry_run:
                current = client.post(
                    "/api/v3/projects",
                    {"name": project.name, "identifier": project.key},
                )
        pid = _eid(current)
        self_href = _link(current, "self") or "/api/v3/projects/%s" % pid

        patch = {}
        patched_names = []
        repo_prop = field_ids["Repo"]
        if current.get(repo_prop) != project.repo:
            patch[repo_prop] = project.repo
            patched_names.append("Repo")
        vis_prop = field_ids["Visibility"]
        if hal.custom_value(current, vis_prop) != project.visibility:
            if not dry_run:
                # A dry run only plans the change; resolving the option
                # would need the (POST) project form.
                if project.visibility not in options_for(pid):
                    raise ApiError(
                        0, "/api/v3/projects/%s/form" % pid,
                        "Visibility option %r not offered" % project.visibility,
                    )
                patch["_links"] = {
                    vis_prop: {"href": visibility_options[project.visibility]}}
            patched_names.append("Visibility")
        # Plan from what differs, write only what a live run resolved: a
        # dry run lists "set Visibility" without the form (Codex final3 D2).
        if patched_names:
            actions.append("set %s on %s" % (", ".join(sorted(patched_names)), project.name))
            if not dry_run and patch:
                current = client.patch(self_href, patch) or current

        members = client.get_all(_link(current, "memberships") or "")
        existing = {}
        for membership in members:
            mid = _eid(membership)
            principal = _tail(_link(membership, "principal") or "")
            for role in (membership.get("_links") or {}).get("roles", []):
                existing[(str(principal), _tail(role.get("href", "")))] = mid

        # Desired memberships as (user id, role id) -> (login, role name), so
        # descriptions stay correct even on dry runs (ids may be unknown).
        owner_uid = str(user_ids[settings.openproject.owner_login])
        desired = {(owner_uid, str(need_role("Owner"))):
                   (settings.openproject.owner_login, "Owner")}
        for user in model.users:
            if user.projects == "all" or (
                user.projects == "public" and project.visibility == "Public"
            ):
                desired[(str(user_ids[user.login]), str(need_role(user.role)))] = (
                    user.login, user.role)
        for uid, rid in sorted(desired):
            if (uid, rid) not in existing:
                login, rname = desired[(uid, rid)]
                write(
                    "add %s to %s as %s" % (login, project.name, rname),
                    client.post,
                    "/api/v3/memberships",
                    {
                        "project": {"href": self_href},
                        "principal": {"href": "/api/v3/users/%s" % uid},
                        "roles": [{"href": "/api/v3/roles/%s" % rid}],
                    },
                )

        spark_ids = {str(user_ids[u.login]) for u in model.users
                     if u.projects == "public" and u.login in user_ids}
        login_of = {}
        for login, uid in user_ids.items():
            login_of.setdefault(str(uid), login)
        if project.visibility == "Private":
            doomed = {}
            for (uid, _rid), mid in existing.items():
                if uid in spark_ids:
                    doomed[mid] = login_of.get(uid, uid)
            for mid in sorted(doomed):
                write(
                    "remove %s from %s" % (doomed[mid], project.name),
                    client.delete,
                    "/api/v3/memberships/%s" % mid,
                )

        versions_href = _link(current, "versions")
        have_versions = set()
        if versions_href:
            for version in client.get_all(versions_href):
                if version.get("name"):
                    have_versions.add(version["name"])
        for wanted in model.versions:
            if wanted not in have_versions:
                write(
                    "create version %s in %s" % (wanted, project.name),
                    client.post,
                    (versions_href or "/api/v3/projects/%s/versions" % pid),
                    {"name": wanted},
                )
                have_versions.add(wanted)

        epic_id = need_type("Epic")
        feature_id = need_type("Feature")
        open_id = need_status("Open")
        approved_id = need_status("Approved")
        # An explicit filter replaces the API's default "open only" filter,
        # so a closed Maintenance item is still found (not duplicated).
        elements = client.get_all(
            "/api/v3/work_packages",
            {"filters": json.dumps([{"project": {"operator": "=", "values": [str(pid)]}}])},
        )
        epic = None
        for element in elements:
            if (
                element.get("subject") == "Maintenance"
                and _tail(_link(element, "type") or "") == str(epic_id)
                and hal.link_id(element, "parent") is None
            ):
                epic = element
                break
        if epic is None:
            actions.append("create Maintenance epic in %s" % project.name)
            if not dry_run:
                epic = client.post(
                    "/api/v3/work_packages",
                    {
                        "subject": "Maintenance",
                        "_links": {
                            "type": {"href": "/api/v3/types/%s" % epic_id},
                            "status": {"href": "/api/v3/statuses/%s" % open_id},
                            "project": {"href": self_href},
                        },
                    },
                )
            else:
                # Dry-run plan: the feature would be created under the
                # would-be epic, exactly as the live run does next.
                actions.append("create Maintenance feature in %s" % project.name)
                continue
        if epic:
            epic_eid = _eid(epic)
            feature = None
            for element in elements:
                if (
                    element.get("subject") == "Maintenance"
                    and _tail(_link(element, "type") or "") == str(feature_id)
                    and str(hal.link_id(element, "parent")) == str(epic_eid)
                ):
                    feature = element
                    break
            if feature is None:
                write(
                    "create Maintenance feature in %s" % project.name,
                    client.post,
                    "/api/v3/work_packages",
                    {
                        "subject": "Maintenance",
                        "_links": {
                            "type": {"href": "/api/v3/types/%s" % feature_id},
                            "status": {"href": "/api/v3/statuses/%s" % approved_id},
                            "project": {"href": self_href},
                            "parent": {"href": "/api/v3/work_packages/%s" % epic_eid},
                        },
                    },
                )

    return actions
