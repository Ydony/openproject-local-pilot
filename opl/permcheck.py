"""Permission check: prove each model user can and cannot do its job.

For claude, codex and spark (every settings token except conductor), using
that user's own token:
1. read work packages in the sandbox project ([permcheck] project);
2. create a Draft task under the sandbox test feature, then delete it with
   the admin token;
3. fail to move the test feature to Approved (expects HTTP 422 or 403);
4. spark only: the project list contains no Private project.

Prints PASS/FAIL per check; exits non-zero on any FAIL. Standard library
only. The PATCH shape for status moves is best-available (live proof in
T1.L).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from opl import hal
from opl.configure.common import _eid, find_project, project_field_ids
from opl.openproject import ApiError, Client
from opl.settings import SettingsError, load as load_settings


@dataclass(frozen=True)
class Check:
    user: str
    name: str
    passed: bool
    detail: str = ""


def _named(elements, name):
    for element in elements:
        if element.get("name") == name:
            return element
    return None


def run_all(settings, client_for, admin_client):
    """Run every check; never raises ApiError (failures become Checks)."""
    checks = []
    sandbox = settings.permcheck.get("project", "")
    feature_name = settings.permcheck.get("feature", "Test feature")
    if not sandbox:
        return [Check("all", "config", False, "[permcheck] project is not configured")]
    try:
        project = find_project(admin_client, sandbox)
        pid = _eid(project)
        elements = admin_client.get_all(
            "/api/v3/work_packages",
            {"filters": json.dumps([{"project": {"operator": "=", "values": [str(pid)]}}])},
        )
        feature = next(
            (e for e in elements if e.get("subject") == feature_name), None)
        if feature is None:
            return [Check("all", "setup", False,
                           "test feature %r not found in %s" % (feature_name, sandbox))]
        fid = _eid(feature)
        types = {e.get("name"): _eid(e)
                 for e in admin_client.get_all("/api/v3/types") if e.get("name")}
        statuses = {e.get("name"): _eid(e)
                    for e in admin_client.get_all("/api/v3/statuses") if e.get("name")}
        for need, kind in (("Task", "type"), ("Draft", "status"), ("Approved", "status")):
            pool = types if kind == "type" else statuses
            if need not in pool:
                return [Check("all", "setup", False,
                               "%s %r not found - run opl-configure first" % (kind, need))]
        vis_prop = project_field_ids(admin_client).get("Visibility")
    except ApiError as exc:
        return [Check("all", "setup", False, str(exc))]

    for who in [w for w in settings.tokens if w != "conductor"]:
        try:
            user_client = client_for(who)
        except SettingsError as exc:
            checks.append(Check(who, "token", False, str(exc)))
            continue
        checks.extend(_user_checks(user_client, admin_client, settings, who,
                                   pid, fid, types["Task"], statuses["Draft"],
                                   statuses["Approved"], vis_prop))
    return checks


def _user_checks(user_client, admin_client, settings, who, pid, fid,
                 task_type, draft_status, approved_status, vis_prop):
    checks = []

    def fail(name, detail):
        checks.append(Check(who, name, False, detail))

    def ok(name):
        checks.append(Check(who, name, True))

    try:
        user_client.get(
            "/api/v3/work_packages",
            {"filters": json.dumps([{"project": {"operator": "=", "values": [str(pid)]}}])},
        )
        ok("read")
    except ApiError as exc:
        fail("read", str(exc))
        return checks

    created = None
    try:
        created = user_client.post(
            "/api/v3/work_packages",
            {
                "subject": "opl-permcheck probe",
                "_links": {
                    "type": {"href": "/api/v3/types/%s" % task_type},
                    "status": {"href": "/api/v3/statuses/%s" % draft_status},
                    "project": {"href": "/api/v3/projects/%s" % pid},
                    "parent": {"href": "/api/v3/work_packages/%s" % fid},
                },
            },
        )
    except ApiError as exc:
        fail("create-delete", "create failed: %s" % exc)
        return checks
    try:
        admin_client.delete("/api/v3/work_packages/%s" % _eid(created))
        ok("create-delete")
    except ApiError as exc:
        fail("create-delete", "admin delete failed: %s" % exc)

    try:
        user_client.patch(
            "/api/v3/work_packages/%s" % fid,
            {"_links": {"status": {"href": "/api/v3/statuses/%s" % approved_status}}},
        )
        fail("forbidden-move", "moving the feature to Approved was allowed")
    except ApiError as exc:
        if exc.status in (422, 403):
            ok("forbidden-move")
        else:
            fail("forbidden-move", "unexpected error: %s" % exc)

    if who == "spark":
        if vis_prop is None:
            fail("no-private", "cannot resolve the Visibility field")
        else:
            try:
                projects = user_client.get_all("/api/v3/projects")
            except ApiError as exc:
                fail("no-private", str(exc))
            else:
                # Visibility is a list field: its value is the link title.
                private = [p for p in projects
                           if hal.custom_value(p, vis_prop) == "Private"]
                if private:
                    fail("no-private", "%d Private project(s) visible" % len(private))
                else:
                    ok("no-private")
    return checks


def main(argv=None):
    """Entry point for bin/opl-permcheck (no arguments)."""
    try:
        settings = load_settings()
        admin = Client(settings.openproject.url, settings.token("admin"))
    except SettingsError as exc:
        print("opl-permcheck: error: %s" % exc)
        return 1

    def client_for(who):
        return Client(settings.openproject.url, settings.token(who))

    checks = run_all(settings, client_for, admin)
    failed = 0
    for check in checks:
        line = "permcheck %s %s: %s" % (
            check.user, check.name, "PASS" if check.passed else "FAIL")
        if check.detail and not check.passed:
            line += " (%s)" % check.detail
        print(line)
        failed += not check.passed
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
