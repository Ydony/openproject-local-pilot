"""Read OpenProject API v3 HAL shapes in one place (TH.7). Standard library only.

Documented v3 shapes (OpenProject 17) this module relies on; TH.7a checks
them against sanitised live samples:

- Schemas (`/api/v3/work_packages/schemas/{project}-{type}`,
  `/api/v3/projects/schema`) carry field definitions at the ROOT, keyed by
  property name. There is no `properties` wrapper. Custom fields are
  `customFieldN`.
- List, user and version values live under `_links.<prop>` as
  {href, title}; multi-select values are a list of those. A cleared link is
  {href: null}. Scalars sit on the resource root; long text is a
  formattable {format, raw, html}.
- Allowed list options come either embedded (`_embedded.allowedValues`,
  CustomOption resources with `value` and a self link) or linked
  (`_links.allowedValues`, a list of {href, title}).
- Activity `details` are formattables, not structured properties. Their
  raw text reads "<Field> changed from <old> to <new>", "<Field> set to
  <new>" or "<Field> deleted (<old>)", in the requesting user's language
  (the bot accounts use English).
"""

from __future__ import annotations

import re
from datetime import datetime

_CUSTOM = re.compile(r"^customField\d+$")
_LINK_TYPES = frozenset({"CustomOption", "[]CustomOption", "User", "[]User",
                         "Version", "[]Version", "Principal"})


def tail(href):
    """Last path segment of an href, without any query string."""
    return str(href).split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]


def schema_fields(schema):
    """Custom-field name -> property ("customFieldN") from a schema root."""
    fields = {}
    for prop, definition in (schema or {}).items():
        if (_CUSTOM.match(str(prop)) and isinstance(definition, dict)
                and definition.get("name")):
            fields[definition["name"]] = prop
    return fields


def is_link_field(schema, prop):
    """True when the field's value is written and read under `_links`."""
    definition = (schema or {}).get(prop) or {}
    if definition.get("location") == "_links":
        return True
    return definition.get("type") in _LINK_TYPES


def allowed_options(schema, prop):
    """Option label -> href for a list field; {} when none are listed."""
    definition = (schema or {}).get(prop) or {}
    options = {}
    embedded = (definition.get("_embedded") or {}).get("allowedValues")
    if isinstance(embedded, list):
        for option in embedded:
            if not isinstance(option, dict):
                continue
            href = ((option.get("_links") or {}).get("self") or {}).get("href")
            if href is None and option.get("id") is not None:
                href = "/api/v3/custom_options/%s" % option["id"]
            label = option.get("value", option.get("name"))
            if label is not None and href:
                options[str(label)] = href
    linked = (definition.get("_links") or {}).get("allowedValues")
    if isinstance(linked, list):
        for option in linked:
            if (isinstance(option, dict) and option.get("href")
                    and option.get("title") is not None):
                options.setdefault(str(option["title"]), option["href"])
    return options


def link(resource, name):
    """The raw `_links.<name>` entry (dict, list, or None)."""
    try:
        return (resource.get("_links") or {}).get(name)
    except AttributeError:
        return None


def link_href(resource, name):
    entry = link(resource, name)
    return entry.get("href") if isinstance(entry, dict) else None


def link_title(resource, name):
    entry = link(resource, name)
    if isinstance(entry, dict) and entry.get("href"):
        return entry.get("title")
    return None


def link_id(resource, name):
    """Numeric id at the end of a link's href; None when unset or odd."""
    href = link_href(resource, name)
    if not href:
        return None
    try:
        return int(tail(href))
    except ValueError:
        return None


def _formattable(value):
    if isinstance(value, dict) and "raw" in value:
        return value.get("raw")
    return value


def custom_value(resource, prop):
    """Readable value of a custom field on a resource.

    Link-valued fields give the link title (a list of titles for
    multi-select); scalars give the root value; formattables give `raw`.
    None when unset.
    """
    entry = link(resource, prop)
    if isinstance(entry, list):
        return [e.get("title") for e in entry
                if isinstance(e, dict) and e.get("href")]
    if isinstance(entry, dict):
        return entry.get("title") if entry.get("href") else None
    return _formattable(resource.get(prop))


def _details_text(entry):
    for detail in entry.get("details") or []:
        text = _formattable(detail)
        if isinstance(text, str):
            yield text.strip()


def _change_pattern(field):
    return re.compile(r"^%s (?:changed from .* to (?P<changed>.*)|set to "
                      r"(?P<set>.*)|deleted \(.*\))$" % re.escape(field))


def _elements(activities):
    if isinstance(activities, dict):
        return (activities.get("_embedded") or {}).get("elements") or []
    return list(activities or [])


def parse_time(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def latest_change(activities, field):
    """The newest journal entry whose details change `field`, else None.

    `activities` is an activities collection or its list of elements.
    """
    pattern = _change_pattern(field)
    best = None
    best_at = None
    for entry in _elements(activities):
        if not isinstance(entry, dict):
            continue
        if not any(pattern.match(text) for text in _details_text(entry)):
            continue
        try:
            at = parse_time(entry.get("createdAt", ""))
        except (TypeError, ValueError):
            continue
        if best_at is None or at > best_at:
            best, best_at = entry, at
    return best


def comment_text(entry):
    """The raw comment of a journal entry, or ""."""
    text = _formattable((entry or {}).get("comment"))
    return text.strip() if isinstance(text, str) else ""


def new_value(entry, field):
    """The value `field` took in a journal entry; None when deleted/absent."""
    pattern = _change_pattern(field)
    for text in _details_text(entry or {}):
        match = pattern.match(text)
        if match:
            value = match.group("changed")
            if value is None:
                value = match.group("set")
            return value
    return None


def old_value(entry, field):
    """Previous value from an English journal detail; None for initial set."""
    pattern = re.compile(r"^%s (?:changed from (?P<old>.*?) to .*|"
                         r"deleted \((?P<deleted>.*)\))$" % re.escape(field))
    for text in _details_text(entry or {}):
        match = pattern.match(text)
        if match:
            return (match.group("old") if match.group("old") is not None
                    else match.group("deleted"))
    return None


def field_changes(activities, field):
    """Dated field changes, oldest first; reject unparseable affected details.

    Unlike latest_change, security provenance must not silently discard a
    malformed change. Equal timestamps need distinct numeric activity ids.
    """
    found = []
    for entry in _elements(activities):
        texts = list(_details_text(entry))
        affected = [text for text in texts if text.startswith(field + " ")]
        if not affected:
            continue
        if len(affected) != 1 or not _change_pattern(field).match(affected[0]):
            raise ValueError("unparseable field change")
        at = parse_time(entry.get("createdAt", ""))
        if at.tzinfo is None:
            raise ValueError("journal timestamp needs timezone")
        found.append((at, entry))
    found.sort(key=lambda pair: pair[0])
    ordered = []
    for at, entry in found:
        if ordered and at == ordered[-1][0]:
            # Refuse ambiguous ordering instead of accepting an API page's
            # arbitrary order. Activity ids provide a stable tie-breaker.
            if not isinstance(entry.get("id"), int) or not isinstance(
                    ordered[-1][1].get("id"), int):
                raise ValueError("ambiguous journal order")
        ordered.append((at, entry))
    ordered.sort(key=lambda pair: (pair[0], pair[1].get("id", 0)))
    return ordered
