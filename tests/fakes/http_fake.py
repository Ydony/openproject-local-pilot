"""Fake HTTP server for OpenProject and GitHub API tests.

Standard library only. Tests register canned replies per (method, path) and
inspect `server.requests` afterwards. No Docker, network, or real API needed.

Usage:
    with FakeServer() as server:
        server.add("GET", "/api/v3/projects", body={...})
        client = Client(server.base_url, "fake-token")
        ...
        assert server.write_count() == 0
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeServer/1"

    def _serve(self):
        fake = self.server.fake
        split = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            body = None
        fake.requests.append(
            {
                "method": self.command,
                "path": split.path,
                "query": split.query,
                "body": body,
                "raw": raw.decode("utf-8", "replace"),
                "headers": dict(self.headers),
            }
        )
        entry = fake.handlers.get((self.command, split.path))
        if entry is None:
            status, obj = 404, {"_type": "Error", "message": "no handler"}
        elif callable(entry):
            status, obj = entry(
                self.command, split.path, split.query, body, dict(self.headers)
            )
        else:
            status, obj = entry
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/hal+json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._serve()

    def do_POST(self):
        self._serve()

    def do_PATCH(self):
        self._serve()

    def do_PUT(self):
        self._serve()

    def do_DELETE(self):
        self._serve()

    def log_message(self, *args):
        pass


class FakeServer:
    """Context-managed fake API server on 127.0.0.1 with an ephemeral port."""

    def __init__(self):
        self.handlers = {}
        self.requests = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.fake = self
        self._server.daemon_threads = True
        self.base_url = "http://127.0.0.1:%d" % self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever)
        self._thread.daemon = True

    def add(self, method, path, status=200, body=None, handler=None):
        """Register a reply: static (status, body) or a callable handler."""
        self.handlers[(method.upper(), path)] = (
            handler if handler is not None else (status, body if body is not None else {})
        )
        return self

    def writes(self):
        """Recorded POST/PATCH/PUT/DELETE requests (the mutating calls)."""
        return [r for r in self.requests if r["method"] in ("POST", "PATCH", "PUT", "DELETE")]

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._thread.join(timeout=10)
        self._server.server_close()
        return False
