"""Stateful fake OpenProject and GitHub for the end-to-end pipeline test.

OpenProject side: users with their own API tokens and roles, the real
statuses/types/fields/workflows from config/pm-model.toml, documented v3
HAL shapes (schema-root fields, list/user values under `_links`, null
parent links, paged listings), a journal entry with author and
"<Field> changed from A to B" details for every change, lockVersion
conflicts, and 422 for status moves the author's role may not make.

GitHub side: one repo, PRs whose head SHA is read live from a local bare
repo, green check runs, no branch protection, and a merge that refuses
a SHA other than the current head.

Standard library only; plugs into tests.fakes.http_fake.FakeServer.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode

EPOCH = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)


class Router(dict):
    """FakeServer handler table with regex routes after exact ones."""

    def __init__(self):
        super().__init__()
        self.routes = []

    def route(self, method, pattern, handler):
        self.routes.append((method, re.compile("^%s$" % pattern), handler))

    def get(self, key, default=None):
        if key in self:
            return dict.get(self, key)
        method, path = key
        for want, pattern, handler in self.routes:
            match = pattern.match(path)
            if want == method and match:
                return (lambda m, p, q, b, h, _h=handler, _g=match.groups():
                        _h(m, p, q, b, h, *_g))
        return default


def _iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class OpenProjectWorld:
    PROJECT_ID = 1

    def __init__(self, model, tokens):
        """`tokens` maps login -> API token; user ids are fixed below."""
        self.model = model
        self.clock = 0
        names = [s.name for s in model.statuses]
        self.status_id = {name: i + 1 for i, name in enumerate(names)}
        self.status_name = {v: k for k, v in self.status_id.items()}
        self.type_id = {"Epic": 11, "Feature": 12, "Task": 13}
        self.type_name = {v: k for k, v in self.type_id.items()}
        self.users = {1: ("owner", "Pat Owner"), 2: ("claude", "Claude"),
                      3: ("codex", "Codex"), 4: ("spark", "Spark"),
                      5: ("conductor", "Conductor")}
        self.uid = {login: uid for uid, (login, _) in self.users.items()}
        self.role = {1: "Owner", 2: "Model", 3: "Model", 4: "Model",
                     5: "Conductor"}
        self.admin = {1}
        self.by_token = {tok: self.uid[login] for login, tok in tokens.items()}
        self.fields = {}
        self.option_id = {}
        self.option_name = {}
        next_field, next_option = 20, 100
        for field in model.fields:
            prop = "customField%d" % next_field
            next_field += 1
            self.fields[field.name] = (prop, field)
            for value in field.values:
                self.option_id[(field.name, value)] = next_option
                self.option_name[next_option] = value
                next_option += 1
        self.wps = {}
        self.journals = {}
        self.next_journal = 1000
        self.rejected = []
        self.conflicts = []
        self.relations = []     # (from id, to id): "from precedes to"
        self.forms = []         # work packages whose edit form was read

    # -- state helpers ------------------------------------------------------
    def tick(self, seconds=5):
        self.clock += seconds
        return EPOCH + timedelta(seconds=self.clock)

    def create(self, wid, subject, type_name, status, parent=None,
               author="owner", **values):
        at = self.tick()
        self.wps[wid] = {"id": wid, "subject": subject, "type": type_name,
                         "status": status, "parent": parent, "assignee": None,
                         "lock": 1, "created": at, "updated": at,
                         "values": {}}
        self.journals[wid] = []
        if "assignee" in values:
            self.wps[wid]["assignee"] = self.uid[values.pop("assignee")]
        for name, value in values.items():
            self.wps[wid]["values"][name.replace("_", " ")] = value
        self._journal(wid, self.uid[author], [], "", at)

    def act(self, login, wid, status=None, comment="", **values):
        """Change a work package as `login`, as the UI would (journaled)."""
        wp = self.wps[wid]
        details = []
        if status is not None and status != wp["status"]:
            details.append("Status changed from %s to %s" % (wp["status"], status))
            wp["status"] = status
        for name, value in values.items():
            details.append(self._change_text(name, wp["values"].get(name), value))
            wp["values"][name] = value
        wp["lock"] += 1
        at = self.tick()
        wp["updated"] = at
        self._journal(wid, self.uid[login], details, comment, at)

    def _journal(self, wid, uid, details, comment, at):
        self.next_journal += 1
        self.journals[wid].append({"id": self.next_journal, "user": uid,
                                   "details": list(details),
                                   "comment": comment, "at": at})

    def _display(self, name, value):
        _, field = self.fields[name]
        if value is None or value == "" or value == []:
            return None
        if field.format == "bool":
            return "Yes" if value else "No"
        if field.format == "user":
            return self.users[value][1]
        if isinstance(value, list):
            return ", ".join(value)
        return str(value)

    def _change_text(self, name, old, new):
        old_s, new_s = self._display(name, old), self._display(name, new)
        if old_s is None:
            return "%s set to %s" % (name, new_s)
        if new_s is None:
            return "%s deleted (%s)" % (name, old_s)
        return "%s changed from %s to %s" % (name, old_s, new_s)

    def status_of(self, wid):
        return self.wps[wid]["status"]

    def history(self, wid):
        """[(author login, details, comment)] oldest first."""
        return [(self.users[j["user"]][0], j["details"], j["comment"])
                for j in self.journals[wid]]

    # -- HAL representation -------------------------------------------------
    def _link(self, href, title=None):
        return {"href": href, "title": title} if href else {"href": None}

    def represent(self, wp):
        links = {
            "self": {"href": "/api/v3/work_packages/%d" % wp["id"],
                     "title": wp["subject"]},
            "type": self._link("/api/v3/types/%d" % self.type_id[wp["type"]],
                               wp["type"]),
            "status": self._link("/api/v3/statuses/%d"
                                 % self.status_id[wp["status"]], wp["status"]),
            "project": self._link("/api/v3/projects/%d" % self.PROJECT_ID, "Demo"),
            "parent": (self._link("/api/v3/work_packages/%d" % wp["parent"])
                       if wp["parent"] else {"href": None}),
            "assignee": (self._link("/api/v3/users/%d" % wp["assignee"],
                                    self.users[wp["assignee"]][1])
                         if wp["assignee"] else {"href": None}),
            "activities": {"href": "/api/v3/work_packages/%d/activities" % wp["id"]},
        }
        body = {"_type": "WorkPackage", "id": wp["id"], "subject": wp["subject"],
                "lockVersion": wp["lock"], "createdAt": _iso(wp["created"]),
                "updatedAt": _iso(wp["updated"]),
                "description": {"format": "markdown", "raw": "", "html": ""}}
        for name, (prop, field) in self.fields.items():
            if wp["type"] not in field.on:
                continue
            value = wp["values"].get(name)
            if field.format == "list":
                if field.multi:
                    links[prop] = [self._link("/api/v3/custom_options/%d"
                                              % self.option_id[(name, v)], v)
                                   for v in (value or [])]
                else:
                    links[prop] = (self._link("/api/v3/custom_options/%d"
                                              % self.option_id[(name, value)], value)
                                   if value else {"href": None})
            elif field.format == "user":
                links[prop] = (self._link("/api/v3/users/%d" % value,
                                          self.users[value][1])
                               if value else {"href": None})
            else:
                body[prop] = value
        body["_links"] = links
        return body

    def schema(self, type_name, form=False):
        """A type's schema. Like OpenProject, allowed values for list
        fields appear only in a form's schema (form_embedded), never on the
        plain /schemas endpoint."""
        body = {"_type": "Schema",
                "subject": {"type": "String", "name": "Subject"}}
        for name, (prop, field) in self.fields.items():
            if type_name not in field.on:
                continue
            if field.format == "list":
                body[prop] = {
                    "type": "[]CustomOption" if field.multi else "CustomOption",
                    "name": name, "location": "_links", "_links": {}}
                if form:
                    options = [(self.option_id[(name, v)], v) for v in field.values]
                    body[prop]["_links"]["allowedValues"] = [
                        {"href": "/api/v3/custom_options/%d" % oid, "title": v}
                        for oid, v in options]
                    body[prop]["_embedded"] = {"allowedValues": [
                        {"_type": "CustomOption", "id": oid, "value": v,
                         "_links": {"self": {"href": "/api/v3/custom_options/%d"
                                                     % oid, "title": v}}}
                        for oid, v in options]}
            elif field.format == "user":
                body[prop] = {"type": "User", "name": name, "location": "_links"}
            else:
                body[prop] = {"type": {"bool": "Boolean", "float": "Float",
                                       "int": "Integer", "link": "Link"}.get(
                                           field.format, "String"),
                              "name": name}
        return body

    # -- HTTP ---------------------------------------------------------------
    def who(self, headers):
        auth = headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return None
        user, _, token = base64.b64decode(auth[6:]).decode().partition(":")
        return self.by_token.get(token) if user == "apikey" else None

    def install(self, router):
        r = router.route
        r("GET", r"/api/v3/statuses", self._statuses)
        r("GET", r"/api/v3/types", self._types)
        r("GET", r"/api/v3/users", self._users)
        r("GET", r"/api/v3/projects", self._projects)
        r("GET", r"/api/v3/memberships", self._memberships)
        r("GET", r"/api/v3/relations", self._relations)
        r("GET", r"/api/v3/work_packages", self._list)
        r("GET", r"/api/v3/work_packages/schemas/(\d+)-(\d+)", self._schema)
        r("GET", r"/api/v3/work_packages/(\d+)", self._get)
        r("PATCH", r"/api/v3/work_packages/(\d+)", self._patch)
        r("GET", r"/api/v3/work_packages/(\d+)/activities", self._activities)
        r("POST", r"/api/v3/work_packages/(\d+)/form", self._form)
        r("POST", r"/api/v3/work_packages/(\d+)/activities", self._comment)

    def _auth(self, headers):
        uid = self.who(headers)
        if uid is None:
            return None, (401, {"_type": "Error", "message": "unauthenticated"})
        return uid, None

    @staticmethod
    def _collection(elements):
        def handler(method, path, query, body, headers):
            return 200, {"_type": "Collection", "total": len(elements),
                         "_embedded": {"elements": elements}, "_links": {}}
        return handler

    def _statuses(self, method, path, query, body, headers):
        return self._collection([{"id": i, "name": n}
                                 for n, i in self.status_id.items()])(
                                     method, path, query, body, headers)

    def _types(self, method, path, query, body, headers):
        return self._collection([{"id": i, "name": n}
                                 for n, i in self.type_id.items()])(
                                     method, path, query, body, headers)

    def _users(self, method, path, query, body, headers):
        uid, err = self._auth(headers)
        if err:
            return err
        if uid not in self.admin:
            return 403, {"_type": "Error", "message": "admin only"}
        return self._collection([{"id": u, "login": login, "name": name}
                                 for u, (login, name) in self.users.items()])(
                                     method, path, query, body, headers)

    def _projects(self, method, path, query, body, headers):
        return self._collection([{
            "id": self.PROJECT_ID, "identifier": "demo", "name": "Demo",
            "_links": {"self": {"href": "/api/v3/projects/%d" % self.PROJECT_ID}}}])(
                method, path, query, body, headers)

    def _memberships(self, method, path, query, body, headers):
        return self._collection([{
            "_type": "Membership",
            "_links": {"principal": {"href": "/api/v3/users/%d" % uid,
                                     "title": self.users[uid][1]},
                       "roles": [{"href": "/api/v3/roles/%d" % len(role),
                                  "title": role}]}}
            for uid, role in self.role.items()])(method, path, query, body, headers)

    def _list(self, method, path, query, body, headers):
        uid, err = self._auth(headers)
        if err:
            return err
        params = {k: v[0] for k, v in parse_qs(query).items()}
        # The server caps pages at 2, so every listing really pages.
        size = min(int(params.get("pageSize", "20")), 2)
        page = int(params.get("offset", "1"))
        wps = [self.represent(w) for _, w in sorted(self.wps.items())]
        chunk = wps[(page - 1) * size: page * size]
        links = {}
        if page * size < len(wps):
            nxt = dict(params, offset=str(page + 1), pageSize=str(size))
            # Like the real API, the next link repeats every parameter,
            # URL-encoded.
            links["nextByOffset"] = {
                "href": "/api/v3/work_packages?" + urlencode(sorted(nxt.items()))}
        return 200, {"_type": "Collection", "total": len(wps),
                     "_embedded": {"elements": chunk}, "_links": links}

    def _relations(self, method, path, query, body, headers):
        return self._collection([
            {"_type": "Relation", "type": "precedes",
             "_links": {"from": {"href": "/api/v3/work_packages/%d" % a},
                        "to": {"href": "/api/v3/work_packages/%d" % b}}}
            for a, b in self.relations])(method, path, query, body, headers)

    def _schema(self, method, path, query, body, headers, pid, tid):
        return 200, self.schema(self.type_name[int(tid)])

    def _get(self, method, path, query, body, headers, wid):
        uid, err = self._auth(headers)
        if err:
            return err
        wp = self.wps.get(int(wid))
        if wp is None:
            return 404, {"_type": "Error", "message": "not found"}
        return 200, self.represent(wp)

    def _patch(self, method, path, query, body, headers, wid):
        uid, err = self._auth(headers)
        if err:
            return err
        wp = self.wps[int(wid)]
        if body.get("lockVersion") != wp["lock"]:
            self.conflicts.append((self.users[uid][0], wp["id"]))
            return 409, {"_type": "Error", "message": "stale lockVersion"}
        links = body.get("_links") or {}
        new_status = wp["status"]
        if "status" in links:
            new_status = self.status_name[int(links["status"]["href"].rsplit("/", 1)[-1])]
            allowed = self.model.transitions(self.role[uid], wp["type"])
            if new_status != wp["status"] and (wp["status"], new_status) not in allowed:
                self.rejected.append((self.users[uid][0], wp["id"],
                                      wp["status"], new_status))
                return 422, {"_type": "Error",
                             "message": "status transition not allowed"}
        changes = {}
        for name, (prop, field) in self.fields.items():
            if prop in links:
                value = links[prop]
                if isinstance(value, list):
                    changes[name] = [self.option_name[int(v["href"].rsplit("/", 1)[-1])]
                                     for v in value]
                elif not value.get("href"):
                    changes[name] = None
                elif field.format == "user":
                    changes[name] = int(value["href"].rsplit("/", 1)[-1])
                else:
                    changes[name] = self.option_name[int(value["href"].rsplit("/", 1)[-1])]
            elif prop in body:
                changes[name] = body[prop]
        changes = {k: v for k, v in changes.items() if wp["values"].get(k) != v}
        self.act(self.users[uid][0], wp["id"],
                 status=new_status if new_status != wp["status"] else None,
                 **changes)
        return 200, self.represent(wp)

    def _form(self, method, path, query, body, headers, wid):
        uid, err = self._auth(headers)
        if err:
            return err
        self.forms.append(int(wid))
        wp = self.wps[int(wid)]
        return 200, {"_type": "Form",
                     "_embedded": {"payload": {"lockVersion": wp["lock"]},
                                   "schema": self.schema(wp["type"], form=True),
                                   "validationErrors": {}}}

    def _activities(self, method, path, query, body, headers, wid):
        elements = []
        for j in self.journals[int(wid)]:
            elements.append({
                "_type": "Activity::Comment" if j["comment"] else "Activity",
                "id": j["id"], "createdAt": _iso(j["at"]),
                "comment": {"format": "markdown", "raw": j["comment"],
                            "html": ""},
                "details": [{"format": "custom", "raw": d, "html": d}
                            for d in j["details"]],
                "_links": {"user": {"href": "/api/v3/users/%d" % j["user"],
                                    "title": self.users[j["user"]][1]}}})
        return 200, {"_type": "Collection", "total": len(elements),
                     "_embedded": {"elements": elements}, "_links": {}}

    def _comment(self, method, path, query, body, headers, wid):
        uid, err = self._auth(headers)
        if err:
            return err
        raw = ((body or {}).get("comment") or {}).get("raw", "")
        self._journal(int(wid), uid, [], raw, self.tick())
        return 201, {"_type": "Activity::Comment"}


class GitHubWorld:
    OWNER, REPO = "example-owner", "demo"

    def __init__(self, bare):
        self.bare = bare
        self.pulls = {}
        self.next_number = 9
        self.merges = []

    def head_sha(self, ref):
        out = subprocess.run(["git", "--git-dir", self.bare, "rev-parse",
                              "refs/heads/%s" % ref], capture_output=True,
                             text=True, timeout=60)
        return out.stdout.strip() if out.returncode == 0 else ""

    def install(self, router):
        base = r"/repos/%s/%s" % (self.OWNER, self.REPO)
        r = router.route
        r("GET", base, lambda *a: (200, {"private": False,
                                          "full_name": "%s/%s" % (self.OWNER, self.REPO)}))
        r("GET", base + r"/pulls", self._list)
        r("POST", base + r"/pulls", self._create)
        r("GET", base + r"/pulls/(\d+)", self._get)
        r("PUT", base + r"/pulls/(\d+)/merge", self._merge)
        r("GET", base + r"/branches/([^/]+)/protection",
          lambda *a: (404, {"message": "Branch not protected"}))
        r("GET", base + r"/commits/([0-9a-f]+)/check-runs",
          lambda *a: (200, {"total_count": 1, "check_runs": [
              {"name": "ci", "status": "completed", "conclusion": "success",
               "app": {"id": 15368}}]}))
        # No legacy statuses: GitHub answers 200, state "pending", empty.
        r("GET", base + r"/commits/([0-9a-f]+)/status",
          lambda *a: (200, {"state": "pending", "total_count": 0,
                            "statuses": []}))
        r("GET", base + r"/deployments", lambda *a: (200, []))
        r("GET", base + r"/actions/workflows",
          lambda *a: (200, {"total_count": 0, "workflows": []}))

    def url(self, number):
        return "https://github.com/%s/%s/pull/%d" % (self.OWNER, self.REPO, number)

    def _list(self, method, path, query, body, headers):
        params = {k: v[0] for k, v in parse_qs(query).items()}
        head = params.get("head", "").split(":", 1)[-1]
        found = [{"html_url": self.url(n), "number": n,
                  "head": {"ref": p["head"]}, "base": {"ref": p["base"]}}
                 for n, p in sorted(self.pulls.items())
                 if not p["merged"] and p["head"] == head
                 and p["base"] == params.get("base", p["base"])]
        return 200, found

    def _create(self, method, path, query, body, headers):
        for p in self.pulls.values():
            if not p["merged"] and p["head"] == body["head"] and p["base"] == body["base"]:
                return 422, {"message": "A pull request already exists"}
        number = self.next_number
        self.next_number += 1
        self.pulls[number] = {"head": body["head"], "base": body["base"],
                              "merged": False, "merged_at": None, "merged_sha": None}
        return 201, {"html_url": self.url(number), "number": number}

    def _get(self, method, path, query, body, headers, number):
        p = self.pulls[int(number)]
        return 200, {"merged": p["merged"], "merged_at": p["merged_at"],
                     "head": {"sha": self.head_sha(p["head"]), "ref": p["head"],
                              "repo": {"full_name": "%s/%s" % (self.OWNER, self.REPO)}},
                     "base": {"ref": p["base"],
                              "repo": {"full_name": "%s/%s" % (self.OWNER, self.REPO)}}}

    def _merge(self, method, path, query, body, headers, number):
        p = self.pulls[int(number)]
        head = self.head_sha(p["head"])
        if body.get("sha") != head:
            return 409, {"message": "Head branch was modified. Review and try the merge again."}
        p.update(merged=True, merged_at="2026-09-25T09:00:00Z", merged_sha=head)
        self.merges.append((int(number), head))
        return 200, {"merged": True, "sha": head}
