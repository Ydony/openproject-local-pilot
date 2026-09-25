#!/usr/bin/env python3
"""Tests: template sections span multiple lines up to the next heading."""

import unittest

from opl.conductor.spark.runner import SparkRunner

TEMPLATE = """Why: Users cannot sign up
without an admin.
What you'll see: A form on /register
Done when:
- Valid input creates an account
- A duplicate email shows an error
"""


class SplitSectionsTests(unittest.TestCase):
    def test_multiline_sections(self):
        sections = SparkRunner._split_sections(TEMPLATE)
        self.assertEqual(sections["why"],
                         "Users cannot sign up\nwithout an admin.")
        self.assertEqual(sections["what"], "A form on /register")
        self.assertIn("- Valid input creates an account",
                      sections["done_when"])
        self.assertIn("- A duplicate email shows an error",
                      sections["done_when"])
        self.assertNotIn("Why:", sections["done_when"])

    def test_plain_text_falls_back_whole(self):
        sections = SparkRunner._split_sections("just some text")
        self.assertEqual(sections, {"why": "just some text",
                                    "what": "just some text",
                                    "done_when": "just some text"})

    def test_empty_falls_back_empty(self):
        sections = SparkRunner._split_sections("")
        self.assertEqual(sections, {"why": "", "what": "",
                                    "done_when": ""})


if __name__ == "__main__":
    unittest.main(verbosity=2)
