#!/usr/bin/env python3
"""scrub(): known secrets in every encoding, and auth headers, never leak."""

import base64
import unittest

from opl.redaction import scrub

SECRET = "s3cret-token-value"


def b64(text):
    return base64.b64encode(text.encode("ascii")).decode("ascii")


class ScrubTests(unittest.TestCase):
    def test_plain_value(self):
        out = scrub("token is %s here" % SECRET, [SECRET])
        self.assertNotIn(SECRET, out)
        self.assertIn("***", out)
        self.assertIn("here", out)

    def test_base64_forms(self):
        for form in (b64(SECRET), b64("x-access-token:" + SECRET),
                     b64("apikey:" + SECRET)):
            with self.subTest(form=form):
                out = scrub("header value %s end" % form, [SECRET])
                self.assertNotIn(form, out)
                self.assertIn("end", out)

    def test_authorization_headers(self):
        for line in ("Authorization: Basic YWJjOmRlZg==",
                     "authorization: bearer abc.def.ghi",
                     "AUTHORIZATION: basic Zm9vOmJhcg==",
                     '{"Authorization": "token ghp_example"}'):
            with self.subTest(line=line):
                out = scrub(line, [])
                self.assertNotRegex(out, r"YWJj|abc\.def|Zm9v|ghp_example")
                self.assertIn("***", out)

    def test_short_or_missing_secrets_are_ignored(self):
        self.assertEqual(scrub("keep x and ab", ["x", "ab", "", None]),
                         "keep x and ab")

    def test_none_text(self):
        self.assertEqual(scrub(None, [SECRET]), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
