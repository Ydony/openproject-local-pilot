"""OpenProject API v3 client. Standard library only.

Auth is HTTP Basic for user `apikey` with the token as password. The token
never appears in exceptions, logs, or error text: messages carry only the
HTTP status, the path, and the server's message body.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

from opl.redaction import scrub


class ApiError(Exception):
    """A failed API call. Never contains the token."""

    def __init__(self, status, path, message):
        super().__init__("API %s %s: %s" % (status, path, message))
        self.status = status
        self.path = path
        self.message = message


# Enough of an error body to scrub whole before cutting it to 500.
_ERROR_READ = 65536

# Guard against runaway paging; a real instance never needs this many.
MAX_PAGES = 100


class Client:
    """Minimal HAL+JSON client for the endpoints opl-conductor needs."""

    def __init__(self, base_url, token, timeout=30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        credentials = base64.b64encode(("apikey:" + token).encode("ascii"))
        self._auth = "Basic " + credentials.decode("ascii")
        self._secrets = (token,)

    def _url(self, path, params=None):
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return url

    def _request(self, method, path, body=None, params=None):
        data = None
        headers = {
            "Authorization": self._auth,
            "Accept": "application/hal+json",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self._url(path, params), data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(_ERROR_READ).decode("utf-8", "replace")
            except Exception:
                detail = ""
            # Scrub, then cut: a secret straddling the cut would otherwise
            # leave its unmatched prefix behind (TH.20, Codex E3).
            raise ApiError(exc.code, path,
                           scrub(detail or str(exc.reason), self._secrets)[:500])
        except urllib.error.URLError as exc:
            raise ApiError(0, path, "unreachable: %s" % exc.reason)
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ApiError(0, path, "invalid JSON response: %s" % exc)

    def get(self, path, params=None):
        return self._request("GET", path, None, params)

    def post(self, path, body):
        return self._request("POST", path, body)

    def patch(self, path, body):
        return self._request("PATCH", path, body)

    def delete(self, path):
        return self._request("DELETE", path)

    def get_all(self, path, params=None):
        """Collect every element of a paged collection.

        Follows the `nextByOffset` link until it disappears. (Extra method
        beyond get/post/patch: needed to list users, types, statuses and
        memberships without missing rows past page one.)
        """
        elements = []
        seen = 0
        next_path = path
        next_params = params
        while next_path is not None:
            if seen >= MAX_PAGES:
                raise ApiError(0, path, "paging exceeded %d pages" % MAX_PAGES)
            seen += 1
            page = self._request("GET", next_path, None, next_params) or {}
            next_params = None
            embedded = page.get("_embedded", {})
            elements.extend(embedded.get("elements", []))
            links = page.get("_links", {})
            nxt = links.get("nextByOffset", {})
            next_path = nxt.get("href") if isinstance(nxt, dict) else None
        return elements
