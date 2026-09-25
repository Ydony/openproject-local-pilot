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
                        r"Workflow\.where\(role_id: .*?, type_id: .*?\)\.delete_all",
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
