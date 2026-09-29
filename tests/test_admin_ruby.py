#!/usr/bin/env python3
"""Tests for opl.configure.admin_ruby: the idempotent Rails-runner script.

Run: python -m unittest discover -s tests   (needs Python 3.11+ for tomllib)
"""

import os
import shutil
import subprocess
import unittest

from opl.configure.admin_ruby import render_admin_script
from opl.model import load

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN = os.path.join(REPO, "tests", "golden", "admin_small.rb")
SHIPPED = os.path.join(REPO, "config", "pm-model.toml")


def _write_tmp(text):
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
        fh.write(text)
        return fh.name


def load_text(text):
    path = _write_tmp(text)
    try:
        return load(path)
    finally:
        os.unlink(path)


def small_model():
    return load_text(
        'global_default = "A"\nversions = []\n'
        '[progress]\nmode = "status"\n'
        '[[status]]\nname = "A"\nclosed = false\ndone_ratio = 0\n'
        '[[status]]\nname = "B"\nclosed = true\ndone_ratio = 100\n'
        '[[type]]\nname = "T1"\nstatuses = ["A", "B"]\ndefault_status = "A"\n'
        '[[role]]\nname = "Owner"\npermissions = "all"\n'
        '[[role]]\nname = "R1"\npermissions = ["view_work_packages"]\n'
        '[[workflow]]\nrole = "Owner"\ntype = "T1"\ntransitions = "all"\n'
        '[[workflow]]\nrole = "R1"\ntype = "T1"\n'
        'transitions = [["A", "B"]]\n'
        '[[field]]\nname = "F1"\non = ["T1"]\nformat = "list"\n'
        'values = ["x", "y"]\n'
    )


class GoldenTests(unittest.TestCase):
    def test_small_model_matches_golden(self):
        rendered = render_admin_script(small_model())
        with open(GOLDEN, encoding="utf-8") as fh:
            # Exact match: the renderer ends with a single newline (TH.13).
            self.assertEqual(rendered, fh.read())


class ContractTests(unittest.TestCase):
    def test_escaping(self):
        model = load_text(
            'versions = []\n'
            '[progress]\nmode = "status"\n'
            '[[status]]\nname = "It\'s \\"odd\\""\nclosed = false\ndone_ratio = 0\n'
            '[[type]]\nname = "T1"\nstatuses = ["It\'s \\"odd\\""]\n'
            'default_status = "It\'s \\"odd\\""\n'
            '[[role]]\nname = "R1"\npermissions = []\n'
        )
        rendered = render_admin_script(model)
        # Valid Ruby single-quoted literal: backslash and quote escaped.
        self.assertIn("'It\\'s \"odd\"'", rendered)

    def test_never_deletes_except_scoped_workflows(self):
        model = load(SHIPPED)
        rendered = render_admin_script(model)
        for i, line in enumerate(rendered.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            if "delete" in stripped or "destroy" in stripped:
                with self.subTest(line=i):
                    self.assertRegex(
                        stripped,
                        r"(Workflow\.where\(role_id: .*?, type_id: .*?\)\.delete_all"
                        r"|stray\.delete_all)",
                        "only scoped workflow deletes allowed, got: %s" % stripped,
                    )

    def test_reports_created_or_changed_only(self):
        rendered = render_admin_script(small_model())
        self.assertIn("previous_changes", rendered)
        self.assertIn("created or changed", rendered)

    def test_progress_mode_present(self):
        rendered = render_admin_script(small_model())
        self.assertIn("work_package_done_ratio", rendered)

    def test_types_enabled_in_projects(self):
        rendered = render_admin_script(small_model())
        self.assertIn("ProjectType", rendered)

    def test_owner_renders_registry_permissions(self):
        rendered = render_admin_script(small_model())
        self.assertIn(
            "OpenProject::AccessControl.permissions.reject(&:global?).map(&:name)",
            rendered,
        )
        self.assertNotIn("wanted = []", rendered)

    def test_unknown_permission_guard(self):
        rendered = render_admin_script(small_model())
        self.assertIn('raise "unknown permission', rendered)
        self.assertIn("OpenProject::AccessControl.permission(pname.to_sym).nil?",
                      rendered)

    def test_draft_escapes_render(self):
        rendered = render_admin_script(load(SHIPPED))
        self.assertIn("workflows Owner/Feature: 65 transitions", rendered)
        self.assertIn("workflows Owner/Epic: 5 transitions", rendered)
        self.assertIn('is_default: true', rendered)

    def test_project_fields_rendered(self):
        model = load_text(
            'versions = []\n'
            '[progress]\nmode = "status"\n'
            '[[status]]\nname = "A"\nclosed = false\ndone_ratio = 0\n'
            '[[type]]\nname = "T1"\nstatuses = ["A"]\ndefault_status = "A"\n'
            '[[role]]\nname = "R1"\npermissions = []\n'
            '[[project_field]]\nname = "Repo"\nformat = "link"\n'
        )
        rendered = render_admin_script(model)
        self.assertIn("ProjectCustomFieldSection.first_or_create!", rendered)
        self.assertIn(
            "pf = ProjectCustomField.find_or_initialize_by(name: 'Repo')",
            rendered,
        )

    def test_live_permission_name(self):
        # Live v17 (d628d3a): add_work_package_notes is
        # add_work_package_comments.
        rendered = render_admin_script(load(SHIPPED))
        self.assertIn("add_work_package_comments", rendered)
        self.assertNotIn("add_work_package_notes", rendered)

    def test_types_marked_default(self):
        # Live v17 (d628d3a): model types are default so projects created
        # later get them too.
        rendered = render_admin_script(small_model())
        self.assertIn("t.is_default = true", rendered)
        self.assertIn("if t.new_record? || t.changed?", rendered)

    def test_project_list_field_options_created(self):
        # Live v17 (d628d3a): project list fields (Visibility) get their
        # options built when missing.
        model = load_text(
            'versions = []\n'
            '[progress]\nmode = "status"\n'
            '[[status]]\nname = "A"\nclosed = false\ndone_ratio = 0\n'
            '[[type]]\nname = "T1"\nstatuses = ["A"]\ndefault_status = "A"\n'
            '[[role]]\nname = "R1"\npermissions = []\n'
            '[[project_field]]\nname = "Visibility"\nformat = "list"\n'
            'values = ["Public", "Private"]\n'
        )
        rendered = render_admin_script(model)
        self.assertIn("missing = ['Public', 'Private'] - pf.custom_options.map(&:value)",
                      rendered)
        self.assertIn("missing.each { |v| pf.custom_options.build(value: v) }",
                      rendered)
        self.assertIn("pf.custom_options.any?(&:new_record?)", rendered)

    def test_unmanaged_cleanup_covers_epic_and_task(self):
        # Issue #35: managed types' workflows must be exact for every role,
        # not only managed roles. Epic and Task (plus Feature) each get a
        # scoped cleanup; the seeded statuses leak otherwise.
        from opl.configure.admin_ruby import describe_workflow_cleanup

        model = load(SHIPPED)
        rendered = render_admin_script(model)
        for tname in ("Epic", "Feature", "Task"):
            with self.subTest(type=tname):
                self.assertIn(
                    "cleanup_type = Type.find_by!(name: '%s')" % tname,
                    rendered,
                )
                self.assertIn(
                    "workflows cleanup %s: removed" % tname,
                    rendered,
                )
        # Managed roles are the keep-list; seeded roles fall into stray.
        self.assertIn(
            "managed_ids = Role.where(name: ['Owner', 'Model', 'Conductor'])",
            rendered,
        )
        self.assertIn(
            "stray = Workflow.where(type_id: cleanup_type.id)"
            ".where.not(role_id: managed_ids)",
            rendered,
        )
        # Idempotent: guarded so a second run changes nothing.
        self.assertIn("if stray.exists?", rendered)
        # Dry-run helper reuses the same managed-type scope.
        self.assertEqual(
            describe_workflow_cleanup(model),
            [
                "remove workflows for unmanaged roles on type Epic",
                "remove workflows for unmanaged roles on type Feature",
                "remove workflows for unmanaged roles on type Task",
            ],
        )

    def test_unmanaged_cleanup_touches_only_managed_types(self):
        # The cleanup must never touch other types: every cleanup Type
        # lookup names a managed type, and every Workflow filter carries
        # a type scope.
        model = load(SHIPPED)
        rendered = render_admin_script(model)
        managed = {t.name for t in model.types}
        section = rendered.split("# --- Workflow cleanup (unmanaged roles) ---", 1)[1]
        section = section.split("# --- Work package custom fields ---", 1)[0]
        for line in section.splitlines():
            stripped = line.strip()
            if stripped.startswith("cleanup_type = Type.find_by!"):
                self.assertIn(stripped, [
                    "cleanup_type = Type.find_by!(name: '%s')" % name
                    for name in managed
                ])
            if stripped.startswith("stray = Workflow.where"):
                self.assertIn("type_id: cleanup_type.id", stripped)
            if "delete_all" in stripped:
                # Scoped variable delete only; the per-pair form lives
                # outside this section.
                self.assertEqual(stripped, "stray.delete_all")


    def test_priorities_renamed_in_place_to_p0_p3(self):
        # Issue #39: configure provisions the DESIGN.md P0-P3 scheme by
        # renaming OpenProject's four seeded priorities in place. Records
        # keep their ids (existing work keeps its priority): find_by only,
        # never find_or_initialize/create, and only `name` is assigned, so
        # positions, colours and the default flag survive untouched.
        from opl.configure.admin_ruby import PRIORITY_RENAMES, describe_priorities

        self.assertEqual(
            tuple(PRIORITY_RENAMES),
            (("Immediate", "P0"), ("High", "P1"),
             ("Normal", "P2"), ("Low", "P3")),
        )
        rendered = render_admin_script(small_model())
        section = rendered.split("# --- Priorities ---", 1)[1]
        section = section.split("# --- Types ---", 1)[0]
        for old, new in PRIORITY_RENAMES:
            with self.subTest(pair=(old, new)):
                self.assertIn(
                    "IssuePriority.find_by(name: '%s')" % old, section)
                self.assertIn("pr.name = '%s'" % new, section)
                self.assertIn("opl_report('priority %s', pr)" % new, section)
        self.assertNotIn("find_or_initialize", section)
        self.assertNotIn("create", section)
        for attr in ("is_default", "color", "position", "active"):
            self.assertNotIn(attr, section,
                             "priorities section must not touch %s" % attr)
        # Idempotent and quiet on re-run: a missing old name is skipped,
        # and a write only happens (and prints) when the name changed.
        self.assertIn("if pr", section)
        self.assertIn("if pr.changed?", section)
        # The dry-run helper lists the same four renames the script makes.
        self.assertEqual(
            describe_priorities(),
            ["rename priority %s -> %s" % pair for pair in PRIORITY_RENAMES],
        )

    def test_priorities_section_comes_from_fixed_mapping(self):
        # The mapping is the documented scheme, not model data: any model
        # renders the same four renames.
        rendered = render_admin_script(load(SHIPPED))
        for old, new in (("Immediate", "P0"), ("High", "P1"),
                         ("Normal", "P2"), ("Low", "P3")):
            self.assertIn(
                "IssuePriority.find_by(name: '%s')" % old, rendered)
            self.assertIn("pr.name = '%s'" % new, rendered)


@unittest.skipUnless(shutil.which("ruby"), "ruby required for syntax check")
class RubySyntaxTests(unittest.TestCase):
    def test_full_model_parses(self):
        rendered = render_admin_script(load(SHIPPED))
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".rb", delete=False) as fh:
            fh.write(rendered)
            path = fh.name
        try:
            proc = subprocess.run(
                ["ruby", "-c", path],
                capture_output=True, text=True, timeout=120,
            )
        finally:
            os.unlink(path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Syntax OK", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
