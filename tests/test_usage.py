#!/usr/bin/env python3
"""Tests for usage collection against real pinned shapes (TH.12).

Synthetic fixtures only: per-tool ccusage report shapes, fake transcript
trees, a throwaway SQLite opencode.db. Never the real ccusage/npx binary,
never real homes.
"""

import json
import os
import sqlite3
import tempfile
import unittest

from opl.usage import (
    CCUSAGE_VERSION,
    UsageError,
    collect_actuals,
    match_tasks,
    parse_sessions,
    price_sessions,
    run_ccusage,
    session_folders,
    task_for_folder,
)

# Claude/OpenCode row: modelBreakdowns[] with modelName + cost.
CLAUDE_REPORT = {
    "sessions": [{
        "sessionId": "aaa-111",
        "inputTokens": 1000, "outputTokens": 200,
        "cacheCreationTokens": 100, "cacheReadTokens": 400,
        "totalTokens": 1700, "totalCost": 99.9,
        "modelsUsed": ["claude-sonnet-4-6"],
        "modelBreakdowns": [
            {"modelName": "claude-sonnet-4-6", "inputTokens": 600,
             "outputTokens": 100, "cacheCreationTokens": 100,
             "cacheReadTokens": 400, "cost": 9.9},
            {"modelName": "claude-opus-4-6", "inputTokens": 400,
             "outputTokens": 100, "cacheCreationTokens": 0,
             "cacheReadTokens": 0, "cost": 4.5}],
        "firstActivity": "2026-09-24T10:00:00.000Z",
        "lastActivity": "2026-09-24T10:05:00.000Z",
    }],
}

# Codex row: models OBJECT keyed by model name plus sessionFile.
CODEX_REPORT = {
    "sessions": [{
        "sessionId": "ccc-333",
        "sessionFile": "/synth/.codex/sessions/2026/09/24/rollout-9-ccc-333.jsonl",
        "directory": "/WRONG/do-not-use",
        "models": {
            "gpt-5.3-codex": {
                "inputTokens": 2000, "outputTokens": 400,
                "cacheReadTokens": 200, "cacheCreationTokens": 0,
                "reasoningOutputTokens": 100, "totalTokens": 2700,
                "isFallback": False}},
        "firstActivity": "2026-09-24T10:00:00.000Z",
        "lastActivity": "2026-09-24T10:05:00.000Z",
    }],
}

MODELS = [
    {"match": "claude-sonnet-*", "input": 3.0, "output": 15.0,
     "cache_read": 0.3, "cache_write": 3.75, "billing": "subscription",
     "as_of": "2026-09-25"},
    {"match": "claude-opus-*", "input": 5.0, "output": 25.0,
     "cache_read": 0.5, "cache_write": 6.25, "billing": "subscription",
     "as_of": "2026-09-25"},
    {"match": "gpt-5*-codex*", "input": 1.75, "output": 14.0,
     "cache_read": 0.175, "cache_write": 1.75,
     "billing": "subscription", "as_of": "2026-09-25"},
    {"match": "*spark*", "input": 0.1, "output": 0.2,
     "cache_read": 0.002, "cache_write": 0.1, "billing": "api",
     "as_of": "2026-09-25"},
]


def make_world():
    from opl.conductor.state import Item, Project, World

    projects = {
        "demo": Project(key="demo", op_id=1, repo="example-owner/demo",
                        visibility="Public", has_test_env=False,
                        test_signal=None, prod_signal=None),
    }
    items = {
        2: Item(id=2, project="demo", type="Feature", status="Building",
                 status_since="2026-09-20T12:00:00Z", parent_id=1),
        5: Item(id=5, project="demo", type="Task", status="In progress",
                 status_since="2026-09-20T12:00:00Z", parent_id=2,
                 assignee="claude", size="S"),
        6: Item(id=6, project="demo", type="Task", status="Ready",
                 status_since="2026-09-20T12:00:00Z", parent_id=2,
                 assignee="spark", size="S"),
    }
    return World(now="2026-09-24T12:00:00Z", projects=projects, items=items,
                 pull_requests={})


class ParseTests(unittest.TestCase):
    def test_claude_breakdowns_use_model_name(self):
        (row,) = parse_sessions(CLAUDE_REPORT)
        self.assertEqual(row["id"], "aaa-111")
        self.assertEqual([b["model"] for b in row["breakdowns"]],
                         ["claude-sonnet-4-6", "claude-opus-4-6"])
        self.assertEqual(row["breakdowns"][0]["input"], 600)

    def test_codex_models_object(self):
        (row,) = parse_sessions(CODEX_REPORT)
        self.assertEqual(row["id"], "ccc-333")
        self.assertEqual(row["session_file"],
                         "/synth/.codex/sessions/2026/09/24/rollout-9-ccc-333.jsonl")
        self.assertEqual(row["breakdowns"][0]["model"], "gpt-5.3-codex")
        self.assertEqual(row["breakdowns"][0]["reasoning"], 100)

    def test_legacy_shapes_still_parse(self):
        payload = {"type": "session", "data": [{
            "session": "bbb-222", "models": ["m"],
            "inputTokens": 10, "outputTokens": 20,
            "cacheCreationTokens": 0, "cacheReadTokens": 5,
            "totalTokens": 35, "costUSD": 1.0,
            "firstActivity": "2026-09-24T10:00:00.000Z",
            "lastActivity": "2026-09-24T10:05:00.000Z"}]}
        (row,) = parse_sessions(payload)
        self.assertEqual(row["id"], "bbb-222")
        self.assertEqual(row["cache_read"], 5)

    def test_not_json_is_usage_error(self):
        with self.assertRaises(UsageError):
            parse_sessions("this is not json{")


class FolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opl-usage-")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _claude_home(self):
        proj = os.path.join(self.tmp, "projects", "-tmp-opl-usage-T5-x")
        os.makedirs(proj)
        with open(os.path.join(proj, "aaa-111.jsonl"), "w",
                  encoding="utf-8") as fh:
            fh.write(json.dumps({"cwd": "/x/opl/task-T5-20260924",
                                 "sessionId": "other-id"}) + "\n")
        with open(os.path.join(proj, "aaa-111.orphaned-1-2.jsonl"), "w",
                  encoding="utf-8") as fh:
            fh.write(json.dumps({"cwd": "/x/elsewhere"}) + "\n")
        return self.tmp

    def _codex_home(self, archive_too=True):
        day = os.path.join(self.tmp, "sessions", "2026", "09", "24")
        os.makedirs(os.path.join(day, "nested"))
        meta = {"type": "session_meta", "payload": {
            "id": "ccc-333", "cwd": "/x/opl/task-T5-20260924",
            "model": "gpt-5.3-codex"}}
        with open(os.path.join(day, "nested",
                               "rollout-9-ccc-333.jsonl"), "w",
                  encoding="utf-8") as fh:
            fh.write(json.dumps(meta) + "\n")
        if archive_too:
            arch = os.path.join(self.tmp, "archived_sessions")
            os.makedirs(arch)
            with open(os.path.join(arch, "rollout-9-ccc-333.jsonl"), "w",
                      encoding="utf-8") as fh:
                fh.write(json.dumps(meta) + "\n")
        return self.tmp

    def _opencode_home(self):
        db = os.path.join(self.tmp, "opencode.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE session (id TEXT, directory TEXT)")
        conn.execute("CREATE TABLE message (id TEXT, session_id TEXT)")
        conn.execute("CREATE TABLE part (id TEXT, message_id TEXT)")
        conn.execute("INSERT INTO session VALUES (?, ?)",
                     ("ses_1", "/x/opl/task-T5-20260924"))
        conn.execute("INSERT INTO message VALUES (?, ?)", ("m1", "ses_1"))
        conn.execute("INSERT INTO message VALUES (?, ?)", ("m2", "ses_1"))
        conn.execute("INSERT INTO part VALUES (?, ?)", ("p1", "m1"))
        conn.execute("INSERT INTO part VALUES (?, ?)", ("p2", "m1"))
        conn.execute("INSERT INTO part VALUES (?, ?)", ("p3", "m2"))
        conn.commit()
        conn.close()
        return self.tmp

    def test_claude_uses_filename_stem_not_slug_or_inner_id(self):
        folders = session_folders("claude", self._claude_home())
        # Report id = filename stem; the file's cwd wins over the slug,
        # the inner sessionId, and the orphaned duplicate is skipped.
        self.assertEqual(folders, {"aaa-111": "/x/opl/task-T5-20260924"})

    def test_codex_nested_plus_archive_duplicate(self):
        folders = session_folders("codex", self._codex_home())
        # Nested transcript found; the archived duplicate of the same
        # session is folded in, not dropped and not double counted.
        self.assertEqual(folders, {"ccc-333": "/x/opl/task-T5-20260924"})

    def test_codex_ignores_report_directory(self):
        # The join reads cwd from the transcript, never the report row.
        folders = session_folders("codex", self._codex_home(
            archive_too=False))
        self.assertNotIn("/WRONG/do-not-use", folders.values())

    def test_codex_missing_cwd_skipped(self):
        day = os.path.join(self.tmp, "sessions", "2026", "09", "24")
        os.makedirs(day)
        with open(os.path.join(day, "rollout-9-zzz-999.jsonl"), "w",
                  encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "response_item"}) + "\n")
        self.assertEqual(session_folders("codex", self.tmp), {})

    def test_opencode_sqlite_session_table(self):
        folders = session_folders("opencode", self._opencode_home())
        # One session row → one folder, however many messages/parts join it.
        self.assertEqual(folders, {"ses_1": "/x/opl/task-T5-20260924"})

    def test_opencode_missing_db_is_empty(self):
        self.assertEqual(session_folders("opencode", self.tmp), {})

    def test_unknown_tool_rejected(self):
        with self.assertRaises(UsageError):
            session_folders("watson", self.tmp)


class FolderMatchTests(unittest.TestCase):
    def test_prefixes_map_to_kind_and_id(self):
        from opl.usage import task_for_folder

        self.assertEqual(task_for_folder("/s/wt/opl/task-T5-20260924"),
                         ("task", 5))
        self.assertEqual(task_for_folder("/s/wt/opl/fix-5-20260924"),
                         ("task", 5))
        self.assertEqual(task_for_folder("/s/wt/opl/review-5-20260924"),
                         ("task", 5))
        self.assertEqual(task_for_folder("/s/wt/opl/test-2-20260924"),
                         ("feature", 2))

    def test_t5_does_not_match_t50(self):
        from opl.usage import task_for_folder

        self.assertEqual(task_for_folder("/s/wt/opl/task-T50-20260924"),
                         ("task", 50))

    def test_dated_scratch_matches_nothing(self):
        from opl.usage import task_for_folder

        self.assertIsNone(task_for_folder("/srv/u/scratch-2026-09-18"))
        self.assertIsNone(task_for_folder("/srv/u/latest-build"))
        self.assertIsNone(task_for_folder("/srv/u/work"))


class MatchTests(unittest.TestCase):
    def test_folder_matches_task(self):
        matched = match_tasks(make_world(),
                              {"aaa-111": "/x/wt/opl/task-T5-20260924"})
        self.assertEqual(matched, {"aaa-111": ("task", 5)})

    def test_test_prefix_matches_feature(self):
        matched = match_tasks(make_world(),
                              {"bbb-222": "/x/wt/opl/test-2-20260924"})
        self.assertEqual(matched, {"bbb-222": ("feature", 2)})

    def test_unknown_id_ignored(self):
        matched = match_tasks(make_world(),
                              {"aaa-111": "/x/wt/opl/task-T99-20260924"})
        self.assertEqual(matched, {})

    def test_unknown_report_session_ignored(self):
        matched = match_tasks(make_world(), {})
        self.assertEqual(matched, {})

    def test_price_session_per_model(self):
        world = make_world()
        sessions = parse_sessions(CLAUDE_REPORT)
        actuals = price_sessions(world, MODELS, sessions,
                                 {"aaa-111": ("task", 5)})
        self.assertIn(("task", 5), actuals)
        sonnet = (600 / 1e6 * 3.0 + 100 / 1e6 * 15.0
                  + 400 / 1e6 * 0.3 + 100 / 1e6 * 3.75)
        opus = (400 / 1e6 * 5.0 + 100 / 1e6 * 25.0)
        self.assertAlmostEqual(actuals[("task", 5)]["cost"], sonnet + opus)
        self.assertEqual(actuals[("task", 5)]["tokens"], 1700)

    def test_codex_reasoning_kept_as_reported(self):
        world = make_world()
        sessions = parse_sessions(CODEX_REPORT)
        actuals = price_sessions(world, MODELS, sessions,
                                 {"ccc-333": ("task", 5)})
        slot = actuals[("task", 5)]
        self.assertEqual(slot["tokens"], 2000 + 400 + 200 + 0 + 100)
        expected = (2000 / 1e6 * 1.75 + (400 + 100) / 1e6 * 14.0
                    + 200 / 1e6 * 0.175)
        self.assertAlmostEqual(slot["cost"], expected)

    def test_unknown_model_leaves_empty_and_logs(self):
        world = make_world()
        sessions = parse_sessions({
            "sessions": [{
                "sessionId": "ddd-444",
                "models": {"mystery-9": {
                    "inputTokens": 100, "outputTokens": 10,
                    "cacheReadTokens": 0, "cacheCreationTokens": 0,
                    "reasoningOutputTokens": 0, "totalTokens": 110,
                    "isFallback": False}},
                "firstActivity": "2026-09-24T10:00:00.000Z",
                "lastActivity": "2026-09-24T10:05:00.000Z"}]})
        with self.assertLogs("opl.usage", level="WARNING") as logs:
            actuals = price_sessions(world, MODELS, sessions,
                                     {"ddd-444": ("task", 5)})
        self.assertEqual(actuals, {})
        self.assertTrue(any("mystery-9" in line for line in logs.output))

    def test_session_without_breakdowns_left_empty(self):
        world = make_world()
        sessions = parse_sessions({
            "sessions": [{"sessionId": "eee-555", "inputTokens": 5,
                          "outputTokens": 5}]})
        with self.assertLogs("opl.usage", level="WARNING"):
            actuals = price_sessions(world, MODELS, sessions,
                                     {"eee-555": ("task", 5)})
        self.assertEqual(actuals, {})


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opl-manifest-")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _manifest(self, rows):
        import json as _json

        with open(os.path.join(self.tmp, "runs.jsonl"), "w",
                  encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(_json.dumps(row) + "\n")
        return self.tmp

    def test_manifest_match_beats_prefix(self):
        from opl.usage import manifest_tasks

        state = self._manifest([
            {"task": 5, "kind": "build", "size": "S",
             "started": "2026-09-24T10:00:00+00:00",
             "ended": "2026-09-24T10:01:00+00:00", "duration_s": 60.0,
             "outcome": "success", "cost_usd": 0.1,
             "worktree": "/x/wt/opl/task-T5-20260924", "commit": "abc"}])
        self.assertEqual(manifest_tasks(state),
                         {"/x/wt/opl/task-T5-20260924": 5})

    def test_session_matched_only_by_registered_worktree(self):
        from opl.usage import match_manifest

        state = self._manifest([
            {"task": 5, "kind": "build", "size": "S",
             "started": "2026-09-24T10:00:00+00:00",
             "ended": "2026-09-24T10:01:00+00:00", "duration_s": 60.0,
             "outcome": "success", "cost_usd": 0.1,
             "worktree": "/x/wt/opl/task-T5-20260924", "commit": "abc"}])
        matched = match_manifest(make_world(), state,
                                 {"aaa-111": "/x/wt/opl/task-T5-20260924",
                                  "zzz-999": "/x/elsewhere"})
        self.assertEqual(matched, {"aaa-111": ("task", 5)})


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opl-collect-")
        claude = os.path.join(self.tmp, "claude", "projects", "slug")
        os.makedirs(claude)
        with open(os.path.join(claude, "aaa-111.jsonl"), "w",
                  encoding="utf-8") as fh:
            fh.write(json.dumps({"cwd": "/x/wt/opl/task-T5-20260924"}) + "\n")
        codex = os.path.join(self.tmp, "codex", "sessions", "2026", "09")
        os.makedirs(codex)
        meta = {"type": "session_meta", "payload": {
            "id": "ccc-333", "cwd": "/x/wt/opl/test-2-20260924",
            "model": "gpt-5.3-codex"}}
        with open(os.path.join(codex, "rollout-9-ccc-333.jsonl"), "w",
                  encoding="utf-8") as fh:
            fh.write(json.dumps(meta) + "\n")
        self.homes = {"claude": os.path.join(self.tmp, "claude"),
                      "codex": os.path.join(self.tmp, "codex"),
                      "opencode": os.path.join(self.tmp, "opencode")}

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _command(self):
        import sys

        script = ("import sys, json; print(json.dumps("
                  "{'claude': %r, 'codex': %r, 'opencode': "
                  "{'sessions': []}}[sys.argv[1]]))" % (CLAUDE_REPORT,
                                                        CODEX_REPORT))
        return [sys.executable, "-c", script]

    def test_collect_prices_and_logs_coverage(self):
        from opl.usage import collect_actuals

        prices = {"models": MODELS, "defaults": {}}
        with self.assertLogs("opl.usage", level="INFO") as logs:
            actuals = collect_actuals(make_world(), prices,
                                      command=self._command(),
                                      homes=self.homes)
        self.assertIn(("task", 5), actuals)
        self.assertIn(("feature", 2), actuals)
        self.assertTrue(any("usage coverage" in line for line in logs.output))
        self.assertTrue(any("20.0.24" in line for line in logs.output))


class CcusageTests(unittest.TestCase):
    def test_failure_is_usage_error(self):
        import sys

        with self.assertRaises(UsageError):
            run_ccusage([sys.executable, "-c", "raise SystemExit(3)"],
                        "claude", timeout=60)

    def test_stdout_parsed(self):
        import sys

        payload = json.dumps(CLAUDE_REPORT)
        out = run_ccusage(
            [sys.executable, "-c", "print(%r)" % payload], "claude",
            timeout=60)
        self.assertEqual(out["sessions"][0]["sessionId"], "aaa-111")

    def test_version_constant_pinned(self):
        self.assertEqual(CCUSAGE_VERSION, "20.0.24")


if __name__ == "__main__":
    unittest.main(verbosity=2)
