"""Shared lookups for the configure steps. Standard library only.

Every collection is read with get_all and schemas are read at their root
through opl.hal (TH.7).
"""

from __future__ import annotations

import json

from opl import hal
from opl.openproject import ApiError


def _tail(href):
    return hal.tail(href)


def _eid(element):
    if element.get("id") is not None:
        return element["id"]
    href = hal.link_href(element, "self")
    return hal.tail(href) if href else None


def project_ids(client, settings):
    """Map settings project key -> OpenProject id (all must exist)."""
    ids = {}
    for project in settings.projects:
        found = client.get_all(
            "/api/v3/projects",
            {"filters": json.dumps([{"name": {"operator": "=", "values": [project.name]}}])},
        )
        match = None
        for element in found:
            if element.get("name") == project.name:
                match = element
                break
        if match is None:
            raise ApiError(
                0, "/api/v3/projects",
                "project %r not found - run apply_api first" % project.name,
            )
        ids[project.key] = _eid(match)
    return ids


def find_user(client, login):
    """Return the user element with `login`, or None."""
    found = client.get_all(
        "/api/v3/users",
        {"filters": json.dumps([{"login": {"operator": "=", "values": [login]}}])},
    )
    for element in found:
        if element.get("login") == login:
            return element
    return None


def find_project(client, name):
    """Return the project element called `name`, or raise ApiError."""
    found = client.get_all(
        "/api/v3/projects",
        {"filters": json.dumps([{"name": {"operator": "=", "values": [name]}}])},
    )
    for element in found:
        if element.get("name") == name:
            return element
    raise ApiError(0, "/api/v3/projects", "project %r not found" % (name,))


def project_schema(client):
    """The project schema; custom-field definitions sit at its root."""
    return client.get("/api/v3/projects/schema") or {}


def project_field_ids(client, schema=None):
    """Map project custom field names (Repo, Visibility) to property ids."""
    return hal.schema_fields(project_schema(client) if schema is None else schema)
