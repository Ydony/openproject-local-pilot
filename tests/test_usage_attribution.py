#!/usr/bin/env python3
"""Cost tracking correctness (TH.19): one attribution rule, counter
conventions, incomplete means unknown, token-free ccusage, Spark's data.

Synthetic data only; never the real ccusage, npx or homes.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from opl.usage import (
    attributor,
    codex_join,
    collect_actuals,
    manifest_targets,
    parse_sessions,
    price_sessions,
    run_ccusage,
    session_folders,
)
from tests.test_usage import CLAUDE_REPORT, MODELS, make_world


def write_runs(state_dir, rows):
    with open(os.path.join(state_dir, "runs.jsonl"), "w", encoding="utf-8",
              newline="\n") as fh:
        for task, kind, worktree in rows:
            fh.write(json.dumps({
                "task": task, "kind": kind, "size": "S",
                "started": "2026-09-24T10:00:00+00:00",
                "ended": "2026-09-24T10:01:00+00:00", "duration_s": 60.0,
                "outcome": "success", "cost_usd": 0.0,
                "worktree": worktree, "commit": "abc"}) + "\n")


def codex_row(sid, model="gpt-5.3-codex", **metrics):
    return {"sessionId": sid, "models": {model: dict(
        {"inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0,
         "cacheCreationTokens": 0, "reasoningOutputTokens": 0,
         "isFallback": False}, **metrics)}}


class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="opl-attr-")
        self.world = make_world()

    def test_registered_worktree_wins_even_without_prefix(self):
        write_runs(self.state, [(5, "build", "/w/run-a")])
        attribute = attributor(self.world, self.state)
        self.assertEqual(attribute("/w/run-a"), ("task", 5))
        self.assertEqual(attribute("/w/run-a/sub/dir"), ("task", 5))

    def test_test_run_belongs_to_the_feature(self):
        write_runs(self.state, [(2, "test", "/w/run-t")])
        self.assertEqual(attributor(self.world, self.state)("/w/run-t"),
                         ("feature", 2))
        self.assertEqual(manifest_targets(self.state),
                         {"/w/run-t": {("feature", 2)}})

    def test_ambiguous_registration_stays_unknown_without_fallback(self):
        # Same path recorded for tasks 5 and 6: no last-wins, and the
        # task-T5- prefix in the name must not rescue it.
        write_runs(self.state, [(5, "build", "/w/task-T5-x"),
                                (6, "build", "/w/task-T5-x")])
        self.assertIsNone(attributor(self.world, self.state)("/w/task-T5-x"))

    def test_unregistered_folder_uses_the_prefix_rule(self):
        write_runs(self.state, [(6, "build", "/w/run-b")])
        attribute = attributor(self.world, self.state)
        self.assertEqual(attribute("/home-ish/task-T5-owner-session"), ("task", 5))
        self.assertIsNone(attribute("/w/elsewhere"))

    def test_type_must_match(self):
        # Item 2 is a Feature: a task-T2- folder never counts for it.
        self.assertIsNone(attributor(self.world, None)("/w/task-T2-x"))
        self.assertEqual(attributor(self.world, None)("/w/test-2-x"),
                         ("feature", 2))

    def test_codex_rows_use_the_same_rule(self):
        write_runs(self.state, [(6, "build", "/w/run-c")])
        rows = parse_sessions({"sessions": [
            dict(codex_row("ccc-1", inputTokens=10, totalTokens=10),
                 sessionFile="/synth/rollout-1-ccc-1.jsonl")]})
        matched = codex_join(rows, {"ccc-1": "/w/run-c"}, self.world, self.state)
        self.assertEqual(matched, {"ccc-1": ("task", 6)})


class CounterTests(unittest.TestCase):
    def price(self, report):
        return price_sessions(make_world(), MODELS, parse_sessions(report),
                              {"s-1": ("task", 5)})

    def test_inclusive_counters_are_not_billed_twice(self):
        # OpenAI style: cache inside input, reasoning inside output,
        # total = input + output.
        slot = self.price({"sessions": [codex_row(
            "s-1", inputTokens=2000, outputTokens=400, cacheReadTokens=200,
            reasoningOutputTokens=100, totalTokens=2400)]})[("task", 5)]
        expected = (1800 / 1e6 * 1.75 + 200 / 1e6 * 0.175
                    + 400 / 1e6 * 14.0)
        self.assertAlmostEqual(slot["cost"], expected)
        self.assertEqual(slot["tokens"], 2400)

    def test_exclusive_counters_bill_reasoning_on_top(self):
        slot = self.price({"sessions": [codex_row(
            "s-1", inputTokens=2000, outputTokens=400, cacheReadTokens=200,
            reasoningOutputTokens=100, totalTokens=2700)]})[("task", 5)]
        expected = (2000 / 1e6 * 1.75 + 200 / 1e6 * 0.175
                    + 500 / 1e6 * 14.0)
        self.assertAlmostEqual(slot["cost"], expected)
        self.assertEqual(slot["tokens"], 2700)

    def test_reasoning_inside_output_exclusive_cache(self):
        slot = self.price({"sessions": [codex_row(
            "s-1", inputTokens=2000, outputTokens=400, cacheReadTokens=200,
            reasoningOutputTokens=100, totalTokens=2600)]})[("task", 5)]
        expected = (2000 / 1e6 * 1.75 + 200 / 1e6 * 0.175
                    + 400 / 1e6 * 14.0)
        self.assertAlmostEqual(slot["cost"], expected)

    def test_conventions_match_exactly_without_tolerance(self):
        # Codex TH.R F3: input 10, output 1 (reasoning 1 inside), total 11.
        # A one-token tolerance picked "exclusive" and counted 12.
        slot = self.price({"sessions": [codex_row(
            "s-1", inputTokens=10, outputTokens=1, reasoningOutputTokens=1,
            totalTokens=11)]})[("task", 5)]
        self.assertEqual(slot["tokens"], 11)
        self.assertAlmostEqual(slot["cost"], 10 / 1e6 * 1.75 + 1 / 1e6 * 14.0)

    def test_session_total_must_match_its_model_rows(self):
        # Codex TH.R F3: the session says 9999 tokens, its only model row
        # accounts for 110: pricing the 110 would hide the rest.
        report = {"sessions": [dict(codex_row(
            "s-1", inputTokens=100, outputTokens=10, totalTokens=110),
            totalTokens=9999)]}
        with self.assertLogs("opl.usage", level="WARNING") as logs:
            actuals = self.price(report)
        self.assertEqual(actuals, {})
        self.assertTrue(any("does not match its model rows" in line
                            for line in logs.output))

    def test_unexplained_total_is_unknown(self):
        with self.assertLogs("opl.usage", level="WARNING") as logs:
            actuals = self.price({"sessions": [codex_row(
                "s-1", inputTokens=2000, outputTokens=400,
                cacheReadTokens=200, totalTokens=9999)]})
        self.assertEqual(actuals, {})
        self.assertTrue(any("no known convention" in line for line in logs.output))


class IncompleteTests(unittest.TestCase):
    def test_one_unpriceable_session_makes_the_item_unknown(self):
        report = {"sessions": [
            codex_row("s-1", inputTokens=10, totalTokens=10),
            codex_row("s-2", model="mystery-9", inputTokens=10, totalTokens=10)]}
        incomplete = set()
        with self.assertLogs("opl.usage", level="WARNING"):
            actuals = price_sessions(make_world(), MODELS, parse_sessions(report),
                                     {"s-1": ("task", 5), "s-2": ("task", 5)},
                                     incomplete)
        self.assertEqual(actuals, {})
        self.assertEqual(incomplete, {("task", 5)})


def fake_ccusage(reports, record_env_to=None):
    """A python -c command printing one report per tool; optionally dumps
    the environment it saw (names only, plus OPENCODE_DATA_DIR)."""
    script = (
        "import sys, json, os\n"
        "tool = sys.argv[1]\n"
        "rec = %r\n"
        "if rec:\n"
        "    with open(rec + '.' + tool, 'w') as fh:\n"
        "        json.dump({'names': sorted(os.environ),\n"
        "                   'opencode': os.environ.get('OPENCODE_DATA_DIR')}, fh)\n"
        "print(json.dumps(%r[tool]))\n" % (record_env_to or "", reports))
    return [sys.executable, "-c", script]


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opl-th19-")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        self.homes = {"claude": os.path.join(self.tmp, "claude"),
                      "codex": os.path.join(self.tmp, "codex"),
                      "opencode": os.path.join(self.tmp, "owner-opencode")}
        claude = os.path.join(self.homes["claude"], "projects", "slug")
        os.makedirs(claude)
        with open(os.path.join(claude, "aaa-111.jsonl"), "w",
                  encoding="utf-8") as fh:
            fh.write(json.dumps({"cwd": "/w/task-T5-owner"}) + "\n")
        # Spark's shared OpenCode data (TH.18: XDG_DATA_HOME=<state>/spark-data).
        self.spark_data = os.path.join(self.state, "spark-data")
        db_dir = os.path.join(self.spark_data, "opencode")
        os.makedirs(db_dir)
        conn = sqlite3.connect(os.path.join(db_dir, "opencode.db"))
        conn.execute("CREATE TABLE session (id TEXT, directory TEXT)")
        conn.execute("INSERT INTO session VALUES ('ooo-1', '/w/run-spark')")
        conn.commit()
        conn.close()
        write_runs(self.state, [(6, "build", "/w/run-spark")])
        self.reports = {
            "claude": CLAUDE_REPORT,
            "codex": {"sessions": []},
            "opencode": {"sessions": [{
                "sessionId": "ooo-1",
                "modelBreakdowns": [{"modelName": "meta-spark-1.3",
                                     "inputTokens": 100, "outputTokens": 10,
                                     "cacheReadTokens": 0,
                                     "cacheCreationTokens": 0}]}]},
        }

    def test_spark_opencode_usage_is_found_and_ccusage_is_pointed_at_it(self):
        record = os.path.join(self.tmp, "env")
        env = {"PATH": os.environ.get("PATH", ""),
               "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
        actuals = collect_actuals(make_world(), {"models": MODELS},
                                  command=fake_ccusage(self.reports, record),
                                  homes=self.homes, state_dir=self.state,
                                  env=env, spark_data=self.spark_data)
        self.assertIn(("task", 6), actuals)       # Spark, via the manifest
        self.assertIn(("task", 5), actuals)       # owner Claude, via prefix
        with open(record + ".opencode", encoding="utf-8") as fh:
            seen = json.load(fh)
        self.assertEqual(seen["opencode"].split(","), [
            self.homes["opencode"], os.path.join(self.spark_data, "opencode")])

    def test_ccusage_sees_no_token_variable(self):
        record = os.path.join(self.tmp, "env")
        env = {"PATH": os.environ.get("PATH", ""),
               "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
        collect_actuals(make_world(), {"models": MODELS},
                        command=fake_ccusage(self.reports, record),
                        homes=self.homes, state_dir=self.state, env=env)
        for tool in ("claude", "codex", "opencode"):
            with open(record + "." + tool, encoding="utf-8") as fh:
                names = json.load(fh)["names"]
            self.assertFalse([n for n in names if n.startswith("OPL_")], tool)

    def test_incomplete_in_one_tool_drops_the_item_everywhere(self):
        self.reports["opencode"]["sessions"].append({
            "sessionId": "ooo-2",
            "modelBreakdowns": [{"modelName": "mystery-9", "inputTokens": 1,
                                 "outputTokens": 1}]})
        claude = os.path.join(self.homes["claude"], "projects", "slug")
        with open(os.path.join(claude, "bbb-222.jsonl"), "w",
                  encoding="utf-8") as fh:
            fh.write(json.dumps({"cwd": "/w/run-spark"}) + "\n")
        self.reports["claude"] = {"sessions": CLAUDE_REPORT["sessions"] + [
            dict(CLAUDE_REPORT["sessions"][0], sessionId="bbb-222")]}
        conn = sqlite3.connect(os.path.join(self.spark_data, "opencode",
                                            "opencode.db"))
        conn.execute("INSERT INTO session VALUES ('ooo-2', '/w/run-spark')")
        conn.commit()
        conn.close()
        with self.assertLogs("opl.usage", level="INFO") as logs:
            actuals = collect_actuals(make_world(), {"models": MODELS},
                                      command=fake_ccusage(self.reports),
                                      homes=self.homes, state_dir=self.state,
                                      spark_data=self.spark_data)
        # Claude priced it, OpenCode could not: explicitly unknown.
        self.assertEqual(actuals[("task", 6)], {"unknown": True})
        self.assertIn(("task", 5), actuals)
        self.assertTrue(any('"incomplete": 1' in line for line in logs.output))


class RunCcusageTests(unittest.TestCase):
    def test_env_is_passed_whole(self):
        script = ("import os, json; print(json.dumps({'sessions': [], "
                  "'seen': os.environ.get('OPL_TOKEN_X', 'absent')}))")
        with mock.patch.dict(os.environ, {"OPL_TOKEN_X": "secret-value"}):
            out = run_ccusage([sys.executable, "-c", script], "claude",
                              timeout=60,
                              env={"PATH": os.environ.get("PATH", ""),
                                   "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")})
        self.assertEqual(out["seen"], "absent")


class SqliteUriTests(unittest.TestCase):
    def test_path_with_hash_percent_and_question_mark(self):
        root = os.path.join(tempfile.mkdtemp(prefix="opl-uri-"), "a#b%c d")
        os.makedirs(root)
        conn = sqlite3.connect(os.path.join(root, "opencode.db"))
        conn.execute("CREATE TABLE session (id TEXT, directory TEXT)")
        conn.execute("INSERT INTO session VALUES ('s', '/w/x')")
        conn.commit()
        conn.close()
        self.assertEqual(session_folders("opencode", root), {"s": "/w/x"})


class WiringTests(unittest.TestCase):
    def test_cost_changes_strip_tokens_and_pass_state(self):
        import opl.conductor.__main__ as conductor_main
        from opl.settings import Conductor, OpenProject, Runner, Settings

        settings = Settings(
            openproject=OpenProject("http://127.0.0.1:8080", "OPL_TOKEN_ADMIN",
                                    "admin"),
            tokens={"spark": "OPL_TOKEN_SPARK", "conductor": "OPL_TOKEN_CONDUCTOR"},
            github_token_env="OPL_GITHUB_TOKEN",
            users_email_domain="example.invalid",
            conductor=Conductor(False, 60, "/tmp/opl-state-x"),
            # A custom worker credential name (TH.23, Codex F2).
            runner=Runner((), 1, {}, worker_env=("ACME_MODEL_KEY",)),
            projects=())
        seen = {}

        def fake_collect(world, prices, **kwargs):
            seen.update(kwargs)
            return {}

        env = {"OPL_TOKEN_ADMIN": "a1a1", "OPL_TOKEN_SPARK": "s1s1",
               "OPL_TOKEN_CONDUCTOR": "c1c1", "OPL_GITHUB_TOKEN": "g1g1",
               "ACME_MODEL_KEY": "w1w1", "KEEP_ME": "1"}
        with mock.patch.dict(os.environ, env), \
                mock.patch("opl.usage.collect_actuals", fake_collect):
            conductor_main._cost_changes(make_world(), settings)
        self.assertEqual(seen["state_dir"], "/tmp/opl-state-x")
        self.assertEqual(seen["spark_data"],
                         os.path.join("/tmp/opl-state-x", "spark-data"))
        for name in env:
            if name != "KEEP_ME":
                self.assertNotIn(name, seen["env"])
        self.assertEqual(seen["env"]["KEEP_ME"], "1")

    def test_worker_credential_names_compare_case_insensitively(self):
        import opl.conductor.__main__ as conductor_main
        from opl.settings import Conductor, OpenProject, Runner, Settings

        settings = Settings(
            openproject=OpenProject("http://127.0.0.1:8080", "OPL_TOKEN_ADMIN",
                                    "admin"),
            tokens={}, github_token_env="OPL_GITHUB_TOKEN",
            users_email_domain="example.invalid",
            conductor=Conductor(False, 60, "/tmp/opl-state-y"),
            runner=Runner((), 1, {}, worker_env=("Acme_Model_Key",)),
            projects=())
        with mock.patch.dict(os.environ, {"ACME_MODEL_KEY": "w2w2"}):
            env = conductor_main._ccusage_env(settings)
        self.assertNotIn("ACME_MODEL_KEY", {n.upper() for n in env})


if __name__ == "__main__":
    unittest.main(verbosity=2)
