"""GitHub REST client (pull requests, checks, merges, deploys).

Standard library only. Auth is a Bearer token; like the OpenProject client,
errors carry only status, path and the server message - never the token.
Endpoint shapes follow the public GitHub REST API; FakeServer stands in for
it in tests, api.github.com in production.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from opl.conductor.state import Deploy, PullRequest
from opl.openproject import ApiError
from opl.redaction import scrub


def _parse_time(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


# Legacy status pages read at most (x100 statuses) before giving up.
_STATUS_PAGES = 10

# Enough of an error body to scrub whole before cutting it to 500.
_ERROR_READ = 65536

# Check-run conclusions that count as passing.
_PASSING = ("success", "skipped", "neutral")


class GitHub:
    """Small GitHub client for exactly what the conductor needs."""

    def __init__(self, token, base_url="https://api.github.com", timeout=30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._token = token
        self._auth = "Bearer " + token

    def pr_head_ref(self, url):
        """Head branch name of a pull request (for checking it out)."""
        owner, repo, number = self._split_pr(url)
        data = self._request(
            "GET", "/repos/%s/%s/pulls/%d" % (owner, repo, number)) or {}
        return ((data.get("head") or {}).get("ref") or "")

    def raw_token(self):
        """The token, for subprocess use only (git auth header).

        Never log or print it; callers pass it to git's in-process config,
        never to a URL or a log line.
        """
        return self._token

    def create_pull(self, owner, repo, title, head, base, body=""):
        """Open a pull request; return the created resource."""
        return self._request(
            "POST", "/repos/%s/%s/pulls" % (owner, repo),
            {"title": title, "head": head, "base": base, "body": body})

    def find_open_pr(self, owner, repo, head, base=None):
        """html_url of an open PR from `head` (into `base`), or "".

        TH.11, idempotent PRs: a re-run after a partial success reuses the
        PR instead of opening a duplicate. With `base`, only a PR that
        targets that branch counts. A failed lookup raises ApiError.
        """
        params = {"head": "%s:%s" % (owner, head), "state": "open"}
        if base:
            params["base"] = base
        # A failed lookup raises (TH.20, Codex E7): "unknown" must never
        # look like "none", or a retry could open a duplicate.
        pulls = self._request(
            "GET", "/repos/%s/%s/pulls" % (owner, repo), params=params)
        for pull in pulls or []:
            if not isinstance(pull, dict) or not pull.get("html_url"):
                continue
            if base and ((pull.get("base") or {}).get("ref") != base):
                continue
            return pull["html_url"]
        return ""

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
            "Accept": "application/vnd.github+json",
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
                           scrub(detail or str(exc.reason),
                                 (self._token,))[:500])
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

    def _split_pr(self, url):
        parts = urllib.parse.urlsplit(url).path.strip("/").split("/")
        if len(parts) != 4 or parts[2] != "pull":
            raise ApiError(0, url, "not a pull request URL")
        try:
            number = int(parts[3])
        except ValueError:
            raise ApiError(0, url, "not a pull request URL")
        return parts[0], parts[1], number

    def pull_request(self, url):
        """Fetch merged state and check greenness for a PR URL."""
        owner, repo, number = self._split_pr(url)
        data = self._request("GET", "/repos/%s/%s/pulls/%d" % (owner, repo, number)) or {}
        merged = bool(data.get("merged", False))
        merged_at = _parse_time(data.get("merged_at"))
        head = data.get("head") or {}
        sha = head.get("sha") or ""
        # Repo identities as GitHub reports them; missing means unknown
        # (""), never assumed to be the URL's repo (TH.23, Codex F1).
        head_repo = (head.get("repo") or {}).get("full_name") or ""
        base_data = data.get("base") or {}
        base_repo = (base_data.get("repo") or {}).get("full_name") or ""
        base = base_data.get("ref", "main")
        checks_green = self._checks_green(owner, repo, base, sha) if sha else False
        return PullRequest(url=url, merged=merged, merged_at=merged_at,
                           checks_green=checks_green, head_sha=sha,
                           head_repo=head_repo, base_repo=base_repo)

    def _required_checks(self, owner, repo, branch):
        """Required checks as [(context, app_id or None)], None if unreadable.

        Uses `required_status_checks.checks` (context plus issuing app) when
        present, else the older `contexts` list. An app_id of -1 or null
        means any app may satisfy the check. Protection without required
        checks gives [], so the caller falls back to "every signal".
        """
        try:
            protection = self._request(
                "GET", "/repos/%s/%s/branches/%s/protection" % (owner, repo, branch))
        except ApiError:
            return None
        checks = (protection or {}).get("required_status_checks") or {}
        detailed = checks.get("checks")
        if isinstance(detailed, list):
            found = []
            for entry in detailed:
                if isinstance(entry, dict) and entry.get("context"):
                    app = entry.get("app_id")
                    found.append((entry["context"],
                                  None if app in (None, -1) else app))
            return found
        contexts = checks.get("contexts")
        if isinstance(contexts, list):
            return [(c, None) for c in contexts if c]
        return []

    def _check_runs(self, owner, repo, sha):
        """All check runs for a commit, across pages.

        A 404 means the repo has no check runs endpoint data: absence of
        signal, which never counts as green (fail closed downstream).
        """
        runs = []
        page = 1
        while True:
            try:
                data = self._request(
                    "GET", "/repos/%s/%s/commits/%s/check-runs" % (owner, repo, sha),
                    params={"per_page": "100", "page": str(page)}) or {}
            except ApiError as exc:
                if exc.status == 404 and page == 1:
                    return []
                raise
            batch = data.get("check_runs", [])
            runs.extend(batch)
            total = data.get("total_count", len(runs))
            if len(batch) < 100 or len(runs) >= total:
                break
            page += 1
        return runs

    def _commit_status(self, owner, repo, sha):
        """[(context, state)] of every legacy commit status, [] when none,
        None when the list could not be read completely.

        Every page is read (TH.23, Codex F6): a failing context on a later
        page must count. Zero statuses is not pending. A status without
        its own state counts as pending (fail closed).
        """
        found = []
        for page in range(1, _STATUS_PAGES + 1):
            try:
                combined = self._request(
                    "GET", "/repos/%s/%s/commits/%s/status" % (owner, repo, sha),
                    params={"per_page": "100", "page": str(page)}) or {}
            except ApiError as exc:
                if exc.status == 404 and page == 1:
                    return []
                raise
            batch = [st for st in combined.get("statuses", [])
                     if isinstance(st, dict)]
            found.extend((st.get("context", ""), st.get("state") or "pending")
                         for st in batch)
            total = combined.get("total_count")
            if isinstance(total, int) and len(found) >= total:
                return found
            if len(batch) < 100:
                # Short page: done, unless GitHub claimed more than it gave.
                return found if not isinstance(total, int) else None
        return None

    def _checks_green(self, owner, repo, base, sha):
        """Green only with evidence (TH.8, TH.20).

        With required checks from branch protection, each must be present
        and every matching signal must pass (a check run from the named
        app, or a legacy status of that context); other checks don't
        count, as on GitHub. Without readable or non-empty requirements,
        every present signal must pass and at least one must exist. A
        failing legacy status is never outvoted by passing check runs.
        """
        required = self._required_checks(owner, repo, base)
        runs = [r for r in self._check_runs(owner, repo, sha)
                if isinstance(r, dict)]
        statuses = self._commit_status(owner, repo, sha)
        if statuses is None:
            return False    # incomplete CI observation is never green

        def run_ok(run):
            return (run.get("status", "completed") == "completed"
                    and run.get("conclusion") in _PASSING)

        if required:
            for context, app_id in required:
                matches = [run_ok(r) for r in runs
                           if r.get("name") == context
                           and (app_id is None
                                or (r.get("app") or {}).get("id") == app_id)]
                if app_id is None:
                    matches += [state == "success"
                                for name, state in statuses if name == context]
                if not matches or not all(matches):
                    return False
            return True
        if not runs and not statuses:
            return False
        return (all(run_ok(r) for r in runs)
                and all(state == "success" for _, state in statuses))

    def merge(self, url, method="squash", sha=None):
        """Merge the pull request; return True when merged.

        `sha` is the expected head SHA the caller verified (TH.5 binds it
        to the reviewed SHA); GitHub refuses when the head has moved.
        """
        owner, repo, number = self._split_pr(url)
        body = {"merge_method": method}
        if sha:
            body["sha"] = sha
        data = self._request(
            "PUT", "/repos/%s/%s/pulls/%d/merge" % (owner, repo, number),
            body) or {}
        return bool(data.get("merged", False))

    def test_deploys(self, project, signal=None):
        """Successful runs of a workflow signal on the default branch."""
        owner, repo = project.repo.split("/", 1)
        signal = signal or project.test_signal
        workflows = self._request("GET", "/repos/%s/%s/actions/workflows" % (owner, repo)) or {}
        match = None
        for workflow in workflows.get("workflows", []):
            name = workflow.get("name", "")
            path = workflow.get("path", "")
            if name == signal.name or path == signal.name or path.endswith("/" + signal.name):
                match = workflow
                break
        if match is None:
            raise ApiError(0, "/repos/%s/%s/actions/workflows" % (owner, repo),
                           "workflow %r not found" % signal.name)
        repo_data = self.get("/repos/%s/%s" % (owner, repo)) or {}
        branch = repo_data.get("default_branch", "main")
        runs = self._request(
            "GET", "/repos/%s/%s/actions/workflows/%s/runs" % (owner, repo, match["id"]),
            {"branch": branch, "status": "success", "per_page": "100"}) or {}
        out = []
        for run in runs.get("workflow_runs", []):
            if run.get("conclusion") == "success":
                out.append(Deploy(project=project.key, target="test",
                                  at=_parse_time(run.get("updated_at"))))
        return out

    def prod_deploys(self, project):
        """Successful deployments to the environment signal."""
        owner, repo = project.repo.split("/", 1)
        signal = project.prod_signal
        deployments = self._request(
            "GET", "/repos/%s/%s/deployments" % (owner, repo),
            {"environment": signal.name, "per_page": "100"}) or {}
        items = deployments if isinstance(deployments, list) else deployments.get("deployments", [])
        out = []
        for deployment in items:
            statuses = self._request("GET", "%s/statuses" % deployment["url"],
                                     {"per_page": "100"}) or []
            if isinstance(statuses, dict):
                statuses = statuses.get("statuses", [])
            for status in statuses:
                if status.get("state") == "success":
                    out.append(Deploy(project=project.key, target="production",
                                      at=_parse_time(status.get("created_at"))))
                    break
        return out

    def repo_private(self, owner, repo):
        """True when the GitHub repo is private (TH.18, dispatch boundary).

        Any lookup failure raises, so the caller refuses fail-closed.
        """
        data = self._request("GET", "/repos/%s/%s" % (owner, repo)) or {}
        # Only an explicit "private": false counts as public; a missing or
        # odd field is treated as private (fail closed).
        return data.get("private") is not False
