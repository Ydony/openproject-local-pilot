#!/usr/bin/env python3
"""A timeout or dropped connection is an ApiError, never a crash (issue #80)."""

import http.client
import unittest
from unittest import mock

from opl.github import GitHub
from opl.openproject import ApiError, Client

FAILURES = (
    TimeoutError("timed out"),
    ConnectionResetError("reset by peer"),
    http.client.RemoteDisconnected("closed without response"),
    http.client.IncompleteRead(b"x"),
)


class SocketErrorTests(unittest.TestCase):
    def check(self, call):
        for failure in FAILURES:
            with self.subTest(failure=type(failure).__name__):
                with mock.patch("urllib.request.urlopen", side_effect=failure):
                    with self.assertRaises(ApiError) as ctx:
                        call()
                self.assertEqual(ctx.exception.status, 0)
                self.assertIn("unreachable", str(ctx.exception))

    def test_openproject_client(self):
        client = Client("http://127.0.0.1:1", "tok")
        self.check(lambda: client.get("/api/v3/projects"))

    def test_github_client(self):
        gh = GitHub("tok", base_url="http://127.0.0.1:1")
        self.check(lambda: gh.get("/repos/o/r"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
