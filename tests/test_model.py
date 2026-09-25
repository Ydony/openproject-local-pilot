#!/usr/bin/env python3
"""Tests for opl.model: the tracker model file and its validation.

Run: python -m unittest discover -s tests   (needs Python 3.11+ for tomllib)
"""

import os
import unittest

from opl.model import ModelError, load

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIPPED = os.path.join(REPO, "config", "pm-model.toml")

BASE = """
global_default = "Draft"
versions = ["Now"]

[progress]
mode = "status"

[[status]]
name = "Draft"
closed = false
done_ratio = 0
[[status]]
name = "Ready"
closed = false
done_ratio = 0

[[type]]
name = "Task"
statuses = ["Draft", "Ready"]
default_status = "Draft"

[[role]]
name = "Model"
permissions = ["view_work_packages"]

[[workflow]]
role = "Model"
type = "Task"
transitions = [["Draft", "Ready"]]

[[field]]
name = "Risk"
on = ["Task"]
format = "list"
values = ["Low", "High"]

[[user]]
login = "spark"
name = "Spark"
role = "Model"
projects = "public"

[[view]]
name = "V"
scope = "global"
filters = [{ field = "Risk", op = "=", values = ["Low"] }]
sort = [["priority", "desc"]]
columns = ["id", "subject", "Risk"]
starred = true
my_page = true
"""


def load_text(text):
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
        fh.write(text)
        path = fh.name
    try:
        return load(path)
    finally:
        os.unlink(path)


class ShippedModelTests(unittest.TestCase):
    def test_counts(self):
        model = load(SHIPPED)
        self.assertEqual(len(model.statuses), 16)
        self.assertEqual(len(model.types), 3)
        self.assertEqual(len(model.roles), 3)
        self.assertEqual(len(model.fields), 14)
        self.assertEqual(len(model.project_fields), 2)
        self.assertEqual(len(model.users), 4)
        self.assertEqual(len(model.versions), 3)
        self.assertEqual(len(model.views), 5)

    def test_names_match_design_sections_1_and_2(self):
        model = load(SHIPPED)
        self.assertEqual(
            {s.name for s in model.statuses},
            {
                "Open", "Closed", "Proposed", "Approved", "Building",
                "In test", "In production", "Done", "Parked", "Rejected",
                "Draft", "Ready", "In progress", "Blocked", "In review",
                "Merged",
            },
        )
        by_type = {t.name: t for t in model.types}
        self.assertEqual(set(by_type["Epic"].statuses), {"Open", "Closed"})
        self.assertEqual(
            set(by_type["Feature"].statuses),
            {"Proposed", "Approved", "Building", "In test", "In production",
             "Done", "Parked", "Rejected"},
        )
        self.assertEqual(
            set(by_type["Task"].statuses),
            {"Draft", "Ready", "In progress", "In review", "Merged", "Blocked"},
        )
        self.assertEqual(
            {r.name for r in model.roles}, {"Owner", "Model", "Conductor"}
        )
        merged = next(s for s in model.statuses if s.name == "Merged")
        self.assertTrue(merged.closed)
        self.assertEqual(merged.done_ratio, 100)

    def test_shipped_owner_transitions_cover_everything(self):
        model = load(SHIPPED)
        pairs = model.transitions("Owner", "Task")
        statuses = ["Draft", "Ready", "In progress", "In review", "Merged", "Blocked"]
        self.assertEqual(pairs, {(a, b) for a in statuses for b in statuses})
        self.assertIn(("Ready", "In progress"),
                      model.transitions("Model", "Task"))

    def test_shipped_models_never_set_ready(self):
        # TH.5/TH.6: a model can leave Blocked only to In progress; Ready
        # is the conductor's (and the owner's) alone.
        model = load(SHIPPED)
        model_pairs = model.transitions("Model", "Task")
        self.assertEqual({b for (a, b) in model_pairs if b == "Ready"}, set())
        self.assertIn(("Blocked", "In progress"), model_pairs)

    def test_shipped_global_default_and_draft_escapes(self):
        model = load(SHIPPED)
        self.assertEqual(model.global_default, "Draft")
        feature_pairs = model.transitions("Owner", "Feature")
        self.assertIn(("Draft", "Proposed"), feature_pairs)
        self.assertEqual(len(feature_pairs), 8 * 8 + 1)
        self.assertIn(("Draft", "Open"), model.transitions("Owner", "Epic"))

    def test_shipped_cost_fields(self):
        model = load(SHIPPED)
        by_name = {f.name: f for f in model.fields}
        for name in ("Est. cost", "Actual cost"):
            self.assertEqual(set(by_name[name].on), {"Epic", "Feature", "Task"})
            self.assertEqual(by_name[name].format, "float")
        self.assertEqual(by_name["Actual tokens"].on, ("Task",))
        self.assertEqual(by_name["Actual tokens"].format, "int")

    def test_shipped_cost_columns(self):
        model = load(SHIPPED)
        by_name = {v.name: v for v in model.views}
        for name in ("Feature pipeline", "Feature pipeline (all projects)",
                     "All projects"):
            self.assertIn("Est. cost", by_name[name].columns)
            self.assertIn("Actual cost", by_name[name].columns)


class TransitionsTests(unittest.TestCase):
    def test_all_expands_to_every_pair(self):
        text = BASE.replace('transitions = [["Draft", "Ready"]]',
                            'transitions = "all"')
        model = load_text(text)
        self.assertEqual(
            model.transitions("Model", "Task"),
            {("Draft", "Draft"), ("Draft", "Ready"),
             ("Ready", "Draft"), ("Ready", "Ready")},
        )

    def test_unknown_combo_is_empty(self):
        model = load_text(BASE)
        self.assertEqual(model.transitions("Nobody", "Task"), set())
        self.assertEqual(model.transitions("Model", "Epic"), set())


class ValidationTests(unittest.TestCase):
    def assert_bad(self, text, fragment):
        with self.assertRaises(ModelError) as ctx:
            load_text(text)
        self.assertIn(fragment, str(ctx.exception))

    def test_valid_base_loads(self):
        model = load_text(BASE)
        self.assertEqual(model.transitions("Model", "Task"), {("Draft", "Ready")})
        self.assertEqual(model.versions, ("Now",))
        self.assertEqual(model.global_default, "Draft")

    def test_second_workflow_row_merges_with_all(self):
        # Owner gets "all" plus an explicit row: the union must hold both
        # without duplication.
        text = BASE.replace('[[role]]\nname = "Model"',
                            '[[role]]\nname = "Owner"\npermissions = "all"\n'
                            '[[role]]\nname = "Model"')
        text += ('[[workflow]]\nrole = "Owner"\ntype = "*"\ntransitions = "all"\n'
                 '[[workflow]]\nrole = "Owner"\ntype = "Task"\n'
                 'transitions = [["Draft", "Ready"]]\n')
        model = load_text(text)
        self.assertEqual(model.transitions("Owner", "Task"),
                         {("Draft", "Draft"), ("Draft", "Ready"),
                          ("Ready", "Draft"), ("Ready", "Ready")})

    def test_global_default_allows_foreign_from_status(self):
        text = BASE
        text += '[[workflow]]\nrole = "Model"\ntype = "Other"\ntransitions = [["Draft", "Ready"]]\n'
        text = text.replace('[[type]]\nname = "Task"',
                            '[[type]]\nname = "Other"\nstatuses = ["Ready"]\ndefault_status = "Ready"\n[[type]]\nname = "Task"')
        model = load_text(text)
        self.assertEqual(model.global_default, "Draft")
        self.assertIn(("Draft", "Ready"), model.transitions("Model", "Other"))

    def test_global_default_must_name_a_status(self):
        self.assert_bad(
            BASE.replace('global_default = "Draft"', 'global_default = "Nope"'),
            "Nope",
        )

    def test_versions_inside_a_table_is_rejected(self):
        self.assert_bad(
            BASE.replace('projects = "public"\n\n[[view]]',
                         'projects = "public"\nversions = ["Later"]\n\n[[view]]'),
            "versions",
        )

    def test_type_lists_unknown_status(self):
        self.assert_bad(
            BASE.replace('statuses = ["Draft", "Ready"]',
                         'statuses = ["Draft", "Nope"]'),
            "Nope",
        )

    def test_transition_uses_status_outside_type(self):
        self.assert_bad(
            BASE.replace('["Draft", "Ready"]', '["Draft", "Elsewhere"]'),
            "Elsewhere",
        )

    def test_field_on_unknown_type(self):
        self.assert_bad(BASE.replace('on = ["Task"]', 'on = ["Epic2"]'), "Epic2")

    def test_user_with_unknown_role(self):
        self.assert_bad(
            BASE.replace('role = "Model"\nprojects = "public"',
                         'role = "Nobody"\nprojects = "public"'),
            "Nobody",
        )

    def test_user_with_bad_projects(self):
        self.assert_bad(
            BASE.replace('projects = "public"', 'projects = "some"'), "some"
        )

    def test_view_uses_unknown_field(self):
        self.assert_bad(
            BASE.replace('{ field = "Risk"', '{ field = "Nope"'), "Nope"
        )

    def test_view_column_must_be_known(self):
        self.assert_bad(
            BASE.replace('"id", "subject", "Risk"', '"id", "Nope"'), "Nope"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
