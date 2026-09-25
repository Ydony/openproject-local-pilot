#!/usr/bin/env python3
"""opl.hal against OpenProject v3 HAL shapes (as documented; TH.7).

Shapes mirror the documented API: schema field definitions sit at the
schema ROOT (no `properties` wrapper); list/user custom values live under
`_links` with href + title; scalar values sit on the resource root; text
values are formattables {format, raw, html}. TH.7a later swaps in sanitised
live samples.
"""

import unittest

from opl import hal

SCHEMA = {
    "_type": "Schema",
    "_dependencies": [],
    "id": {"type": "Integer", "name": "ID", "writable": False},
    "subject": {"type": "String", "name": "Subject", "writable": True},
    "customField11": {
        "type": "CustomOption", "name": "Risk", "location": "_links",
        "writable": True,
        "_links": {"allowedValues": [
            {"href": "/api/v3/custom_options/1", "title": "Low"},
            {"href": "/api/v3/custom_options/2", "title": "Medium"},
            {"href": "/api/v3/custom_options/3", "title": "High"}]},
    },
    "customField12": {
        "type": "[]CustomOption", "name": "Models", "location": "_links",
        "_embedded": {"allowedValues": [
            {"_type": "CustomOption", "id": 7, "value": "Claude",
             "_links": {"self": {"href": "/api/v3/custom_options/7", "title": "Claude"}}},
            {"_type": "CustomOption", "id": 8, "value": "Spark",
             "_links": {"self": {"href": "/api/v3/custom_options/8", "title": "Spark"}}}]},
    },
    "customField13": {"type": "User", "name": "Reviewer", "location": "_links",
                      "_links": {"allowedValues": {"href": "/api/v3/projects/1/available_assignees"}}},
    "customField14": {"type": "Boolean", "name": "Needs you"},
    "customField15": {"type": "Float", "name": "Est. cost"},
    "customField16": {"type": "Integer", "name": "Actual tokens"},
    "customField17": {"type": "Formattable", "name": "Notes"},
    "_links": {"self": {"href": "/api/v3/work_packages/schemas/1-3"}},
}

WORK_PACKAGE = {
    "_type": "WorkPackage", "id": 42, "lockVersion": 5, "subject": "Build form",
    "customField14": True,
    "customField15": 1.25,
    "customField16": 1200,
    "customField17": {"format": "markdown", "raw": "hello", "html": "<p>hello</p>"},
    "_links": {
        "self": {"href": "/api/v3/work_packages/42", "title": "Build form"},
        "status": {"href": "/api/v3/statuses/7", "title": "In review"},
        "type": {"href": "/api/v3/types/3", "title": "Task"},
        "parent": {"href": "/api/v3/work_packages/40", "title": "Registration"},
        "assignee": {"href": "/api/v3/users/9", "title": "Spark"},
        "customField11": {"href": "/api/v3/custom_options/3", "title": "High"},
        "customField12": [{"href": "/api/v3/custom_options/7", "title": "Claude"},
                          {"href": "/api/v3/custom_options/8", "title": "Spark"}],
        "customField13": {"href": "/api/v3/users/5", "title": "Claude"},
    },
}


class SchemaTests(unittest.TestCase):
    def test_fields_are_read_from_the_schema_root(self):
        names = hal.schema_fields(SCHEMA)
        self.assertEqual(names["Risk"], "customField11")
        self.assertEqual(names["Models"], "customField12")
        self.assertEqual(names["Reviewer"], "customField13")
        self.assertEqual(names["Needs you"], "customField14")
        self.assertNotIn("Subject", names)  # built-ins are not custom fields

    def test_a_properties_wrapper_is_not_expected(self):
        self.assertEqual(hal.schema_fields({"properties": {"customField1": {"name": "X"}}}), {})

    def test_allowed_options_from_links_and_embedded(self):
        self.assertEqual(hal.allowed_options(SCHEMA, "customField11"),
                         {"Low": "/api/v3/custom_options/1",
                          "Medium": "/api/v3/custom_options/2",
                          "High": "/api/v3/custom_options/3"})
        self.assertEqual(hal.allowed_options(SCHEMA, "customField12"),
                         {"Claude": "/api/v3/custom_options/7",
                          "Spark": "/api/v3/custom_options/8"})

    def test_link_valued_fields(self):
        self.assertTrue(hal.is_link_field(SCHEMA, "customField11"))
        self.assertTrue(hal.is_link_field(SCHEMA, "customField13"))
        self.assertFalse(hal.is_link_field(SCHEMA, "customField14"))


class ValueTests(unittest.TestCase):
    def test_list_value_is_the_link_title(self):
        self.assertEqual(hal.custom_value(WORK_PACKAGE, "customField11"), "High")

    def test_multi_select_is_a_list_of_titles(self):
        self.assertEqual(hal.custom_value(WORK_PACKAGE, "customField12"), ["Claude", "Spark"])

    def test_user_value_gives_title_and_href(self):
        self.assertEqual(hal.custom_value(WORK_PACKAGE, "customField13"), "Claude")
        self.assertEqual(hal.link_href(WORK_PACKAGE, "customField13"), "/api/v3/users/5")

    def test_scalars_and_formattables(self):
        self.assertIs(hal.custom_value(WORK_PACKAGE, "customField14"), True)
        self.assertEqual(hal.custom_value(WORK_PACKAGE, "customField15"), 1.25)
        self.assertEqual(hal.custom_value(WORK_PACKAGE, "customField16"), 1200)
        self.assertEqual(hal.custom_value(WORK_PACKAGE, "customField17"), "hello")

    def test_missing_or_null_link(self):
        wp = {"_links": {"customField11": {"href": None}}}
        self.assertIsNone(hal.custom_value(wp, "customField11"))
        self.assertIsNone(hal.custom_value(wp, "customField99"))

    def test_link_id_and_null_parent(self):
        self.assertEqual(hal.link_id(WORK_PACKAGE, "parent"), 40)
        self.assertEqual(hal.link_id(WORK_PACKAGE, "status"), 7)
        self.assertIsNone(hal.link_id({"_links": {"parent": {"href": None}}}, "parent"))
        self.assertIsNone(hal.link_id({"_links": {}}, "parent"))
        self.assertEqual(hal.link_title(WORK_PACKAGE, "status"), "In review")


class JournalTests(unittest.TestCase):
    ACTIVITY = {"_embedded": {"elements": [
        {"createdAt": "2026-09-20T10:00:00Z",
         "details": [{"format": "custom", "raw": "Subject changed from a to b"}],
         "_links": {"user": {"href": "/api/v3/users/9", "title": "Spark"}}},
        {"createdAt": "2026-09-21T10:00:00Z",
         "details": [{"format": "custom", "raw": "Status changed from Ready to In progress"}],
         "_links": {"user": {"href": "/api/v3/users/9", "title": "Spark"}}},
        {"createdAt": "2026-09-22T10:00:00Z",
         "details": [{"format": "custom", "raw": "Merge OK set to Yes"}],
         "_links": {"user": {"href": "/api/v3/users/1", "title": "Owner Name"}}},
    ]}}

    def test_latest_change_of_a_field(self):
        entry = hal.latest_change(self.ACTIVITY, "Status")
        self.assertEqual(entry["createdAt"], "2026-09-21T10:00:00Z")
        self.assertIsNone(hal.latest_change(self.ACTIVITY, "Review result"))

    def test_change_author(self):
        entry = hal.latest_change(self.ACTIVITY, "Merge OK")
        self.assertEqual(hal.link_href(entry, "user"), "/api/v3/users/1")

    def test_new_value(self):
        self.assertEqual(hal.new_value(hal.latest_change(self.ACTIVITY, "Status"),
                                       "Status"), "In progress")
        self.assertEqual(hal.new_value(hal.latest_change(self.ACTIVITY, "Merge OK"),
                                       "Merge OK"), "Yes")
        deleted = {"details": [{"raw": "Merge OK deleted (Yes)"}]}
        self.assertIsNone(hal.new_value(deleted, "Merge OK"))

    def test_field_names_do_not_prefix_match(self):
        entry = {"createdAt": "2026-09-23T10:00:00Z",
                 "details": [{"raw": "Status note set to x"}]}
        self.assertIsNone(hal.latest_change([entry], "Status"))

    def test_accepts_a_plain_list_of_elements(self):
        elements = self.ACTIVITY["_embedded"]["elements"]
        self.assertEqual(hal.latest_change(elements, "Status")["createdAt"],
                         "2026-09-21T10:00:00Z")


if __name__ == "__main__":
    unittest.main(verbosity=2)
