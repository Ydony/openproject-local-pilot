#!/usr/bin/env python3
"""Tests for opl.openproject: auth, errors, paging. FakeServer only."""

import base64
import unittest

from opl.openproject import ApiError, Client
from tests.fakes.http_fake import FakeServer


class AuthTests(unittest.TestCase):
    def test_basic_auth_header(self):
        with FakeServer() as server:
            server.add("GET", "/api/v3/projects", body={"_embedded": {"elements": []}})
            client = Client(server.base_url, "fake-token-123")
            client.get("/api/v3/projects")
            self.assertEqual(len(server.requests), 1)
            auth = server.requests[0]["headers"].get("Authorization")
            self.assertEqual(
                auth,
                "Basic " + base64.b64encode(b"apikey:fake-token-123").decode("ascii"),
            )

    def test_server_error_hides_token(self):
        with FakeServer() as server:
            server.add("GET", "/api/v3/projects", status=500,
                       body={"message": "boom"})
            client = Client(server.base_url, "super-secret-token")
            with self.assertRaises(ApiError) as ctx:
                client.get("/api/v3/projects")
            self.assertEqual(ctx.exception.status, 500)
            self.assertNotIn("super-secret-token", str(ctx.exception))

    def test_reflected_auth_in_error_body_is_scrubbed(self):
        # TH.2: a server that echoes the request's Authorization header (and
        # the raw token) into its error body must not leak either form.
        token = "super-secret-token"
        encoded = base64.b64encode(("apikey:" + token).encode()).decode()

        def reflect(method, path, query, body, headers):
            return 500, {"echo": "Authorization: %s" % headers.get("Authorization"),
                         "raw": token, "b64": encoded}

        with FakeServer() as server:
            server.add("GET", "/api/v3/projects", handler=reflect)
            client = Client(server.base_url, token)
            with self.assertRaises(ApiError) as ctx:
                client.get("/api/v3/projects")
        message = str(ctx.exception)
        self.assertNotIn(token, message)
        self.assertNotIn(encoded, message)


    def test_secret_straddling_the_cut_leaves_no_prefix(self):
        # TH.20 (Codex E3): the error text is cut to 500 characters. A
        # token that starts just before the cut must not survive as a
        # prefix: scrub the whole body first, then cut.
        token = "tok-SECRET-VALUE-123"

        def reflect(method, path, query, body, headers):
            return 500, {"m": "x" * 488 + token + "y" * 100}

        with FakeServer() as server:
            server.add("GET", "/api/v3/projects", handler=reflect)
            client = Client(server.base_url, token)
            with self.assertRaises(ApiError) as ctx:
                client.get("/api/v3/projects")
        self.assertNotIn("tok-S", ctx.exception.message)
        self.assertLessEqual(len(ctx.exception.message), 500)


class PagingTests(unittest.TestCase):
    def test_get_all_walks_three_pages(self):
        with FakeServer() as server:
            calls = []

            def pages(method, path, query, body, headers):
                calls.append(query)
                if query == "":
                    elements = [{"id": 1}]
                    nxt = {"href": "/api/v3/users?offset=2"}  # relative
                elif query == "offset=2":
                    elements = [{"id": 2}]
                    nxt = {"href": server.base_url + "/api/v3/users?offset=3"}  # absolute
                else:
                    elements = [{"id": 3}]
                    nxt = None
                links = {"nextByOffset": nxt} if nxt else {}
                return 200, {"_embedded": {"elements": elements},
                             "_links": links}

            server.add("GET", "/api/v3/users", handler=pages)
            client = Client(server.base_url, "t")
            elements = client.get_all("/api/v3/users")
            self.assertEqual([e["id"] for e in elements], [1, 2, 3])
            self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
