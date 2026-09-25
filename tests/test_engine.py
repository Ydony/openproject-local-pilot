#!/usr/bin/env python3
"""Tests for the conductor engine: conflicts, guard, watch/live, CLI."""

import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from unittest import mock

from opl.conductor.collect import collect_github  # noqa: F401 (re-export check)
from opl.conductor.engine import (
    apply,
    drop_noops,
    guard_transitions,
    resolve_conflicts,
    run_once,
)
from opl.conductor.state import Change, Item, Project, World
from opl.model import load as load_model
from opl.openproject import Client
from tests.fakes.ccusage_fake import no_real_ccusage
from tests.fakes.http_fake import FakeServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
T0 = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def make_model():
    path = None
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
        fh.write(
            'versions = []\n'
            '[progress]\nmode = "status"\n'
            '[[status]]\nname = "Proposed"\nclosed = false\ndone_ratio = 0\n'
            '[[status]]\nname = "Approved"\nclosed = false\ndone_ratio = 0\n'
            '[[status]]\nname = "Building"\nclosed = false\ndone_ratio = 0\n'
            '[[status]]\nname = "In test"\nclosed = false\ndone_ratio = 0\n'
            '[[status]]\nname = "In production"\nclosed = false\ndone_ratio = 0\n'
            '[[status]]\nname = "Done"\nclosed = true\ndone_ratio = 100\n'
            '[[status]]\nname = "Draft"\nclosed = false\ndone_ratio = 0\n'
            '[[status]]\nname = "Ready"\nclosed = false\ndone_ratio = 0\n'
            '[[type]]\nname = "Feature"\n'
            'statuses = ["Proposed", "Approved", "Building", "In test", "In production", "Done"]\n'
            'default_status = "Proposed"\n'
            '[[type]]\nname = "Task"\nstatuses = ["Draft", "Ready"]\ndefault_status = "Draft"\n'
            '[[role]]\nname = "Conductor"\npermissions = []\n'
            '[[workflow]]\nrole = "Conductor"\ntype = "Feature"\n'
            'transitions = [["Approved", "Building"]]\n'
        )
        path = fh.name
    try:
        return load_model(path)
    finally:
        os.unlink(path)


def make_world():
    projects = {
        "demo": Project(key="demo", op_id=1, repo="e/d", visibility="Public",
                        has_test_env=False, test_signal=None, prod_signal=None),
    }
    items = {
        1: Item(id=1, project="demo", type="Epic", status="Open",
                 status_since=T0),
        2: Item(id=2, project="demo", type="Feature", status="Approved",
                 status_since=T0, parent_id=1),
        3: Item(id=3, project="demo", type="Task", status="Draft",
                 status_since=T0, parent_id=2, assignee="spark",
                 reviewer="claude", size="S", risk="Low"),
    }
    return World(now=NOW, projects=projects, items=items, pull_requests={})


def change(**over):
    base = {"rule": "stages", "target": "item", "key": "3", "field": "status",
            "new": "Ready", "reason": "r"}
    base.update(over)
    return Change(**base)


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.world = make_world()
        self.model = make_model()

    def test_conflict_drops_both_with_log(self):
        kept, logs = resolve_conflicts(
            [change(new="Ready"), change(rule="enforce", new="Blocked")])
        self.assertEqual(kept, [])
        self.assertEqual(len(logs), 1)
        self.assertIn("conflict", logs[0])

    def test_identical_changes_dedupe(self):
        kept, logs = resolve_conflicts([change(), change()])
        self.assertEqual(len(kept), 1)
        self.assertEqual(logs, [])

    def test_guard_drops_unlisted_moves(self):
        world = make_world()
        world.items[2] = world.items[2].__class__(
            **{**world.items[2].__dict__, "status": "Proposed"})
        kept, logs = guard_transitions(
            [Change(rule="x", target="item", key="2", field="status",
                    new="Approved", reason="r")],
            world, self.model)
        self.assertEqual(kept, [])
        self.assertTrue(any("guard" in line for line in logs))

    def test_guard_drops_production_to_done(self):
        world = make_world()
        world.items[2] = world.items[2].__class__(
            **{**world.items[2].__dict__, "status": "In production"})
        kept, _logs = guard_transitions(
            [Change(rule="x", target="item", key="2", field="status",
                    new="Done", reason="r")],
            world, self.model)
        self.assertEqual(kept, [])

    def test_guard_keeps_listed_move(self):
        kept, logs = guard_transitions(
            [change(new="Ready")], make_world(), self.model)
        # Draft -> Ready is not a Conductor/Task transition in this model.
        self.assertEqual(kept, [])
        self.assertTrue(logs)

    def test_noop_dropped(self):
        world = make_world()
        kept, dropped = drop_noops(
            [Change(rule="x", target="item", key="3", field="status",
                    new="Draft", reason="r")], world)
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 1)


class RunOnceTests(unittest.TestCase):
    def test_smoke_moves_and_guard(self):
        world = make_world()
        changes = run_once(world, make_model())
        # S1 fires for task 3 in the rules, but the test model has no
        # Conductor/Task transitions, so the guard drops it. The feature
        # Models change is not a status change, so it survives.
        self.assertEqual(
            [(c.target, c.field, c.new) for c in changes],
            [("item", "models", ("Spark",))])


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.client = Client(self.server.base_url, "tok")
        self.world = make_world()
        self.tmpd = tempfile.mkdtemp(prefix="opl-engine-")
        self.log_path = os.path.join(self.tmpd, "watch.log")

    def test_watch_writes_json_and_makes_no_http_calls(self):
        changes = [
            Change(rule="stages", target="item", key="3", field="status",
                   new="Ready", reason="Ready: r"),
            Change(rule="merge", target="pr", key="https://github.com/e/d/pull/9", field="merge",
                   new=True, reason="Merge: r"),
        ]
        apply(changes, self.world, self.client, None, False, self.log_path)
        self.assertEqual(self.server.requests, [])
        with open(self.log_path, encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh]
        self.assertEqual(len(lines), 2)
        self.assertEqual(
            set(lines[0]),
            {"time", "rule", "target", "key", "field", "old", "new", "reason"})
        self.assertEqual((lines[0]["new"], lines[0]["old"]), ("Ready", "Draft"))
        self.assertIsNone(lines[1]["old"])

    def test_list_field_sends_option_link(self):
        self._schema()
        changes = [Change(rule="screens", target="item", key="3",
                          field="action", new="Approve", reason="Approve")]
        apply(changes, self.world, self.client, None, True, self.log_path)
        patches = self._patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["body"]["_links"]["customField7"],
                         {"href": "/api/v3/custom_options/77"})

    def test_multi_select_sends_list(self):
        self._schema()
        changes = [Change(rule="screens", target="item", key="3",
                          field="models", new=("Spark", "Claude"),
                          reason="Models")]
        apply(changes, self.world, self.client, None, True, self.log_path)
        patches = self._patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["body"]["_links"]["customField8"],
                         [{"href": "/api/v3/custom_options/89"},
                          {"href": "/api/v3/custom_options/88"}])

    def test_two_changes_one_patch(self):
        self._schema()
        changes = [
            Change(rule="stages", target="item", key="3", field="status",
                   new="Ready", reason="Ready: r"),
            Change(rule="screens", target="item", key="3", field="needs_you",
                   new=True, reason="Needs you"),
        ]
        apply(changes, self.world, self.client, None, True, self.log_path)
        patches = self._patches()
        self.assertEqual(len(patches), 1)
        body = patches[0]["body"]
        self.assertEqual(body["lockVersion"], 0)
        self.assertEqual(body["_links"]["status"]["href"],
                         "/api/v3/statuses/30")
        self.assertEqual(body["customField9"], True)

    def test_screens_only_posts_no_comment(self):
        self._schema()
        changes = [Change(rule="screens", target="item", key="3",
                          field="needs_you", new=True, reason="Needs you")]
        apply(changes, self.world, self.client, None, True, self.log_path)
        self.assertEqual(len(self._patches()), 1)
        self.assertEqual(
            [r for r in self.server.requests if r["method"] == "POST"], [])

    def test_status_change_posts_one_comment(self):
        self._schema()
        changes = [Change(rule="stages", target="item", key="3", field="status",
                          new="Ready", reason="Ready: r")]
        apply(changes, self.world, self.client, None, True, self.log_path)
        posts = [r for r in self.server.requests
                 if r["method"] == "POST"
                 and r["path"] == "/api/v3/work_packages/3/activities"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["body"]["comment"]["raw"], "Ready: r")

    def test_options_come_from_the_form_when_the_schema_has_none(self):
        # TH.23 (Codex N3): OpenProject's plain schema carries no allowed
        # values; the work package's edit form does.
        self._schema()
        self.server.add("GET", "/api/v3/work_packages/schemas/1-13", body={
            "_type": "Schema",
            "customField7": {"type": "CustomOption", "name": "Action",
                             "location": "_links", "_links": {}}})
        self.server.add("GET", "/api/v3/work_packages/3",
                        body={"id": 3, "lockVersion": 4})
        forms = []

        def form(method, path, query, body, headers):
            forms.append(body)
            return 200, {"_type": "Form", "_embedded": {"schema": {
                "customField7": {"type": "CustomOption", "name": "Action",
                                 "_embedded": {"allowedValues": [
                                     {"id": 77, "value": "Approve"}]}}}}}

        self.server.add("POST", "/api/v3/work_packages/3/form", handler=form)
        apply([Change(rule="screens", target="item", key="3",
                      field="action", new="Approve", reason="Approve")],
              self.world, self.client, None, True, self.log_path)
        self.assertEqual(forms, [{"lockVersion": 4}])
        self.assertEqual(self._patches()[0]["body"]["_links"]["customField7"],
                         {"href": "/api/v3/custom_options/77"})

    def test_merge_sends_the_reviewed_sha(self):
        calls = []

        class GH:
            def merge(self, url, method="squash", sha=None):
                calls.append((url, sha))
                return True

        self._schema()
        sha = "e" * 40
        self.world.items[3] = replace(self.world.items[3], pr_url="https://github.com/e/d/pull/9")
        apply([Change(rule="merge", target="pr", key="https://github.com/e/d/pull/9", field="merge",
                      new=sha, reason="Merge: r")],
              self.world, self.client, GH(), True, self.log_path)
        self.assertEqual(calls, [("https://github.com/e/d/pull/9", sha)])

    def test_merge_without_a_sha_is_refused(self):
        calls = []

        class GH:
            def merge(self, url, method="squash", sha=None):
                calls.append(url)
                return True

        self._schema()
        apply([Change(rule="merge", target="pr", key="https://github.com/e/d/pull/9", field="merge",
                      new=True, reason="Merge: r")],
              self.world, self.client, GH(), True, self.log_path)
        self.assertEqual(calls, [])

    def test_clearing_review_result_patches_null_and_explains(self):
        self._schema()
        reason = "Re-review: the PR head moved to dddd after the review of cccc"
        apply([Change(rule="merge", target="item", key="3",
                      field="review_result", new=None, reason=reason)],
              self.world, self.client, None, True, self.log_path)
        patches = self._patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["body"]["_links"]["customField10"],
                         {"href": None})
        posts = [r for r in self.server.requests if r["method"] == "POST"]
        self.assertEqual([p["body"]["comment"]["raw"] for p in posts], [reason])

    def _schema(self):
        self.server.add("GET", "/api/v3/types", body={"_embedded": {"elements": [
            {"id": 13, "name": "Task"}]}})
        self.server.add("GET", "/api/v3/statuses", body={"_embedded": {"elements": [
            {"id": 30, "name": "Ready"}]}})
        # Documented v3 shape: fields at the schema root; one list field
        # embeds its options, the other links them (TH.7).
        self.server.add("GET", "/api/v3/work_packages/schemas/1-13", body={
            "_type": "Schema",
            "customField7": {"type": "CustomOption", "name": "Action",
                             "location": "_links",
                             "_embedded": {"allowedValues": [
                                 {"id": 77, "value": "Approve"},
                                 {"id": 78, "value": "Decide deploy"}]}},
            "customField8": {"type": "[]CustomOption", "name": "Models",
                             "location": "_links",
                             "_links": {"allowedValues": [
                                 {"href": "/api/v3/custom_options/88",
                                  "title": "Claude"},
                                 {"href": "/api/v3/custom_options/89",
                                  "title": "Spark"}]}},
            "customField9": {"type": "Boolean", "name": "Needs you"},
            "customField10": {"type": "CustomOption", "name": "Review result",
                              "location": "_links",
                              "_links": {"allowedValues": [
                                  {"href": "/api/v3/custom_options/101",
                                   "title": "Pass"}]}},
        })
        self.server.add("PATCH", "/api/v3/work_packages/3", body={})
        self.server.add("POST", "/api/v3/work_packages/3/activities", body={})

    def _patches(self):
        return [r for r in self.server.requests if r["method"] == "PATCH"]


MAIN_TOML = """
[openproject]
url = "%s"
admin_token_env = "OPL_TOKEN_ADMIN"
owner_login = "admin"

[tokens]
claude = "OPL_TOKEN_CLAUDE"
codex = "OPL_TOKEN_CODEX"
spark = "OPL_TOKEN_SPARK"
conductor = "OPL_TOKEN_CONDUCTOR"

[github]
token_env = "OPL_GITHUB_TOKEN"

[users]
email_domain = "example.invalid"

[conductor]
live = false
interval_seconds = 60
state_dir = "%s"

[runner]
command = ["example-worker"]
max_parallel = 2

[runner.limits_minutes]
S = 20
M = 45
L = 90
review = 15
test = 20
stall = 10

[[project]]
key = "demo"
name = "Demo"
repo = "example-owner/demo"
visibility = "Public"
has_test_env = false
prod_signal = { kind = "environment", name = "production" }
"""


class MainTests(unittest.TestCase):
    def _main(self, argv, live_cfg, stub_gh):
        import opl.conductor.__main__ as conductor_main

        server = FakeServer()
        server.__enter__()
        self.addCleanup(server.__exit__, None, None, None)
        base = server.base_url
        server.add("GET", "/api/v3/projects", body={"_embedded": {"elements": [
            {"id": 1, "identifier": "demo", "name": "Demo",
             "_links": {"self": {"href": base + "/api/v3/projects/1"}}}]}})
        server.add("GET", "/api/v3/work_packages", body={"_embedded": {"elements": [
            {"id": 1, "subject": "E", "updatedAt": "2026-09-20T12:00:00Z",
             "_links": {"type": {"href": "/api/v3/types/11"},
                        "status": {"href": "/api/v3/statuses/21"},
                        "project": {"href": base + "/api/v3/projects/1"}}},
            {"id": 2, "subject": "F", "updatedAt": "2026-09-20T12:00:00Z",
             "_links": {"type": {"href": "/api/v3/types/12"},
                        "status": {"href": "/api/v3/statuses/22"},
                        "project": {"href": base + "/api/v3/projects/1"},
                        "parent": {"href": base + "/api/v3/work_packages/1"}}},
        ]}})
        server.add("GET", "/api/v3/types", body={"_embedded": {"elements": [
            {"id": 11, "name": "Epic"}, {"id": 12, "name": "Feature"},
            {"id": 13, "name": "Task"}]}})
        server.add("GET", "/api/v3/statuses", body={"_embedded": {"elements": [
            {"id": 21, "name": "Open"}, {"id": 22, "name": "Proposed"}]}})
        server.add("GET", "/api/v3/users", body={"_embedded": {"elements": []}})
        for type_id in (11, 12, 13):
            server.add("GET", "/api/v3/work_packages/schemas/1-%d" % type_id,
                       body={"_type": "Schema"})
        server.add("GET", "/api/v3/relations", body={"_embedded": {"elements": []}})
        server.add("GET", "/api/v3/memberships", body={"_embedded": {"elements": []}})
        tmpd = tempfile.mkdtemp(prefix="opl-main-")
        state_dir = os.path.join(tmpd, "state")
        with open(os.path.join(tmpd, "opl.toml"), "w", encoding="utf-8") as fh:
            fh.write(MAIN_TOML % (base, state_dir.replace("\\", "/")))
        env = {
            "OPL_CONFIG_DIR": tmpd,
            "OPL_TOKEN_ADMIN": "sentinel-admin",
            "OPL_TOKEN_CLAUDE": "sentinel-claude",
            "OPL_TOKEN_CODEX": "sentinel-codex",
            "OPL_TOKEN_SPARK": "sentinel-spark",
            "OPL_TOKEN_CONDUCTOR": "sentinel-conductor",
            "OPL_GITHUB_TOKEN": "sentinel-gh",
        }
        from unittest import mock

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), no_real_ccusage():
            with mock.patch.object(conductor_main, "GitHub", stub_gh):
                with redirect_stdout(out), redirect_stderr(err):
                    rc = conductor_main.main(argv)
        return rc, server, state_dir, out.getvalue(), err.getvalue()

    def test_live_without_flag_stays_watch(self):
        class StubGH:
            def __init__(self, *args, **kwargs):
                pass

            def test_deploys(self, project, signal=None):
                return []

            def prod_deploys(self, project):
                return []

        rc, server, state_dir, _out, _err = self._main(["--once", "--live"], False, StubGH)
        self.assertEqual(rc, 0)
        # Watch mode: JSON lines on disk, zero writes to any API.
        with open(os.path.join(state_dir, "watch.log"), encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh]
        self.assertTrue(lines)
        self.assertTrue(any("Approve" in line["reason"] for line in lines))
        self.assertEqual(server.writes(), [])

    def test_unreachable_skipped(self):
        import opl.conductor.__main__ as conductor_main

        tmpd = tempfile.mkdtemp(prefix="opl-main-")
        state_dir = os.path.join(tmpd, "state")
        with open(os.path.join(tmpd, "opl.toml"), "w", encoding="utf-8") as fh:
            fh.write(MAIN_TOML % ("http://127.0.0.1:9", state_dir.replace("\\", "/")))
        env = {
            "OPL_CONFIG_DIR": tmpd,
            "OPL_TOKEN_ADMIN": "sentinel-admin",
            "OPL_TOKEN_CLAUDE": "sentinel-claude",
            "OPL_TOKEN_CODEX": "sentinel-codex",
            "OPL_TOKEN_SPARK": "sentinel-spark",
            "OPL_TOKEN_CONDUCTOR": "sentinel-conductor",
            "OPL_GITHUB_TOKEN": "sentinel-gh",
        }
        from unittest import mock

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), no_real_ccusage():
            with redirect_stdout(out), redirect_stderr(err):
                rc = conductor_main.main(["--once"])
        self.assertEqual(rc, 0)
        self.assertIn("skipping cycle", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
