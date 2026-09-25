"""Spark build runs: start ready tasks, run workers, handle outcomes.

One tick() starts a bounded set of runs (slots cap concurrency; each run
goes in its own thread and the tick joins them). Outcomes move the task and
comment through the API with Spark's own token. Standard library only.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timezone

from opl import hal
from opl.conductor.engine import _LiveLookups
from opl.conductor.rules.enforce import violations
from opl.conductor.spark.outcomes import keep_partial_work, parse_final_line
from opl.conductor.spark.packet import build_packet
from opl.conductor.spark.records import record_run
from opl.conductor.spark.supervisor import (
    RunResult,
    bind_stop,
    run_worker,
    terminate_all,
    worker_environment,
)
from opl.conductor.spark.worktree import (
    checkout_pr_head,
    checkout_worktree,
    create_worktree,
    delete_ref,
    dispose_worktree,
    remove_worktree,
)
from opl.github import pr_source_problem
from opl.openproject import ApiError
from opl.settings import redact as redact_text
from opl.settings import valid_pr_base

logger = logging.getLogger("opl.conductor.spark")

_REVIEW_RE = re.compile(r"^OPL-REVIEW:\s*(PASS|CHANGES)\b(.*)$",
                        re.MULTILINE | re.IGNORECASE)
_TEST_RE = re.compile(r"^OPL-TEST:\s*(PASS|FAIL)\b(.*)$",
                      re.MULTILINE | re.IGNORECASE)


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _push_env(remote_url, token_or_none):
    """git environment for pushing. The token travels only in an in-process
    GIT_CONFIG_* entry for github.com remotes (never in argv, logs, or the
    remote URL); local remotes push with no token at all. Requires git 2.31+.
    """
    if token_or_none and remote_url.startswith("https://github.com/"):
        raw = base64.b64encode(
            ("x-access-token:" + token_or_none).encode("ascii")).decode("ascii")
        return {"GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + raw}
    return {}


def _sh(args, cwd, timeout=120, env=None, redact=None):
    from opl.conductor.spark.worktree import git_env

    merged = git_env(env)
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, env=merged)
    if proc.returncode != 0:
        detail = proc.stderr.strip()[:300]
        if redact is not None:
            detail = redact(detail)
        raise RuntimeError("%s failed: %s" % (" ".join(args[:3]), detail))
    return proc.stdout.strip()



def _origin_host_slug(url):
    """(host, owner/name) of a git remote URL, or ("", "") when unknown.

    Accepts https, ssh:// and scp-like (`git@host:path`) GitHub forms.
    Local paths have no host and never qualify.
    """
    text = (url or "").strip()
    if not text:
        return "", ""
    if "://" in text:
        _scheme, _, rest = text.partition("://")
        if "/" not in rest:
            return "", ""
        host, _, path = rest.partition("/")
        host = host.split("@")[-1]
    elif "@" in text.split("/")[0] and ":" in text:
        head, _, path = text.partition(":")
        host = head.split("@")[-1]
    else:
        return "", ""
    slug = path.lower().rstrip("/")
    if slug.endswith(".git"):
        slug = slug[:-4]
    return host.lower(), slug.strip("/")

# The PR identity check shared with the merge rule and the collector.
_review_source_problem = pr_source_problem


class _StaleRun(Exception):
    """A run's authorization changed before its worker could start."""


def _public(auth):
    """The comparable part of an authorization snapshot."""
    return {k: v for k, v in auth.items() if not k.startswith("_")}


# A refused run's scratch tree was never touched by a worker: dispose of
# it like a clean success instead of keeping it as "evidence".
_NEVER_RAN = RunResult(outcome="success", duration_s=0.0,
                       last_lines=("not started",))


_ATTEMPTS_FILE = "attempts.json"

# Review and test jobs get one retry: the initial run plus one more.
# The count persists in <state_dir>/attempts.json so the ceiling holds
# across ticks and restarts (TH.10, R13).
_MAX_RUN_ATTEMPTS = 2


def read_attempts(state_dir):
    """{(kind, id): failed-run count}; corrupt or missing reads as {}."""
    counts = {}
    for key, value in _read_attempts_raw(state_dir).items():
        try:
            kind, raw = str(key).split(":", 1)
            if kind == "ceiling":
                continue
            counts[(kind, int(raw))] = int(value)
        except (ValueError, TypeError):
            continue
    return counts


def _read_attempts_raw(state_dir):
    """Raw attempts.json dict (counts plus ceiling flags)."""
    try:
        with open(os.path.join(state_dir, _ATTEMPTS_FILE),
                  encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_attempts(state_dir, counts):
    _write_attempts_raw(state_dir, counts)


def _write_attempts_raw(state_dir, raw):
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, _ATTEMPTS_FILE), "w",
                  encoding="utf-8", newline="\n") as fh:
            json.dump({str(key): value
                       for key, value in sorted(raw.items(),
                                               key=lambda kv: str(kv[0]))},
                      fh)
    except OSError as exc:
        logger.warning("attempts record failed: %s", exc)

class SparkRunner:
    """Run Spark build tasks. `model` is reserved for future policy checks."""

    def __init__(self, settings, model, op_spark_client, gh, slots):
        self.settings = settings
        self.model = model
        self.op = op_spark_client
        self.gh = gh
        self.slots = slots
        self._active = {}
        self._lock = threading.Lock()
        # This runner's own stop gate (TH.23), bound to its run threads.
        self._stop = threading.Event()

    # -- candidate selection ------------------------------------------------
    def _candidates(self, world):
        bad = violations(world)
        found = []
        for item in sorted(world.items.values(), key=lambda i: i.id):
            project = world.projects.get(item.project)
            if project is None or project.visibility != "Public":
                continue
            if item.type != "Task" or item.status != "Ready":
                continue
            if item.assignee != "spark" or item.id in bad:
                continue
            with self._lock:
                if ("build", item.id) in self._active:
                    continue
            found.append(item)
        return found

    def _review_candidates(self, world):
        bad = violations(world)
        found = []
        for item in sorted(world.items.values(), key=lambda i: i.id):
            project = world.projects.get(item.project)
            if project is None or project.visibility != "Public":
                continue
            if item.type != "Task" or item.status != "In review":
                continue
            if item.reviewer != "spark" or item.review_result is not None:
                continue
            if not item.pr_url or item.id in bad:
                continue
            with self._lock:
                if ("review", item.id) in self._active:
                    continue
            found.append(item)
        return found

    def _test_candidates(self, world):
        bad = violations(world)
        found = []
        for item in sorted(world.items.values(), key=lambda i: i.id):
            project = world.projects.get(item.project)
            if project is None or project.visibility != "Public":
                continue
            if item.type != "Feature" or item.status != "In test":
                continue
            if item.test_result is not None or item.id in bad:
                continue
            with self._lock:
                if ("test", item.id) in self._active:
                    continue
            found.append(item)
        return found

    def _fix_candidates(self, world):
        """Tasks sent back by review: In progress + spark + Changes requested."""
        bad = violations(world)
        found = []
        for item in sorted(world.items.values(), key=lambda i: i.id):
            project = world.projects.get(item.project)
            if project is None or project.visibility != "Public":
                continue
            if item.type != "Task" or item.status != "In progress":
                continue
            if item.assignee != "spark" or item.id in bad:
                continue
            if item.review_result != "Changes requested":
                continue
            if not item.pr_url:
                continue
            with self._lock:
                if ("fix", item.id) in self._active:
                    continue
            found.append(item)
        return found

    def _attempt_count(self, kind, iid):
        """Failed runs so far for (kind, id), persisted across restarts."""
        return read_attempts(self.settings.conductor.state_dir).get(
            (kind, iid), 0)

    def _bump_attempts(self, kind, iid):
        """Persist one more failed run; never breaks the loop."""
        raw = _read_attempts_raw(self.settings.conductor.state_dir)
        key = "%s:%d" % (kind, iid)
        try:
            raw[key] = int(raw.get(key, 0)) + 1
        except (ValueError, TypeError):
            raw[key] = 1
        _write_attempts_raw(self.settings.conductor.state_dir, raw)
        return raw[key]

    def _reset_attempts(self, kind, iid):
        """A concluded result clears the count: only result-less runs count."""
        raw = _read_attempts_raw(self.settings.conductor.state_dir)
        key = "%s:%d" % (kind, iid)
        if key in raw:
            del raw[key]
            _write_attempts_raw(self.settings.conductor.state_dir, raw)

    def _ceiling_entry(self, kind, iid):
        """The ceiling record {"at": iso, "notified": bool} or {}."""
        raw = _read_attempts_raw(self.settings.conductor.state_dir)
        entry = raw.get("ceiling:%s:%d" % (kind, iid))
        if isinstance(entry, dict):
            return entry
        if entry is True:
            # Pre-TH.21 flag: notified, but the hit time is unknown, so it
            # never re-arms on its own (fail closed).
            return {"at": None, "notified": True}
        return {}

    def _store_ceiling(self, kind, iid, notified):
        raw = _read_attempts_raw(self.settings.conductor.state_dir)
        entry = raw.get("ceiling:%s:%d" % (kind, iid))
        if not isinstance(entry, dict):
            entry = {}
        entry["notified"] = bool(notified)
        if not entry.get("at"):
            entry["at"] = _utcnow()
        raw["ceiling:%s:%d" % (kind, iid)] = entry
        _write_attempts_raw(self.settings.conductor.state_dir, raw)

    @staticmethod
    def _epoch(value):
        """Epoch seconds for a datetime or ISO string, else None."""
        try:
            moment = value
            if isinstance(value, str):
                moment = datetime.fromisoformat(
                    value.replace("Z", "+00:00"))
            stamp = moment.timestamp()
        except (ValueError, TypeError, AttributeError, OverflowError):
            return None
        return stamp

    def _rearm_if_moved(self, kind, item):
        """Clear a ceiling hit when someone moved the item since.

        A ceiling whose hit time predates the item's status_since means a
        lead unblocked it by hand: the count and the notification go away
        and the item gets a fresh pair of attempts. Anything unreadable
        stays blocked (fail closed).
        """
        entry = self._ceiling_entry(kind, item.id)
        if not entry.get("at"):
            return False
        moved = self._epoch(item.status_since)
        try:
            hit = self._epoch(entry["at"])
        except (ValueError, TypeError, AttributeError):
            hit = None
        if moved is None or hit is None or moved <= hit:
            return False
        raw = _read_attempts_raw(self.settings.conductor.state_dir)
        raw.pop("%s:%d" % (kind, item.id), None)
        raw.pop("ceiling:%s:%d" % (kind, item.id), None)
        _write_attempts_raw(self.settings.conductor.state_dir, raw)
        logger.info("%s %d moved since the ceiling hit: fresh attempts",
                    kind, item.id)
        return True

    def _ceiling_block(self, world, kind, item, statuses):
        """Out of retries: Blocked with "needs lead" (tasks), or a single
        "needs lead" comment where no Blocked status exists (features —
        the notification persists, later ticks stay quiet)."""
        if item.type != "Task":
            if self._ceiling_entry(kind, item.id).get("notified"):
                return []
            # Record the hit first (unsent), mark it sent only once the
            # comment is posted: a failed post is retried next tick instead
            # of being remembered as delivered (TH.22, Codex F7).
            self._store_ceiling(kind, item.id, False)
            text = ("%s run failed %d times with no result: needs lead"
                    % (kind, _MAX_RUN_ATTEMPTS))
            try:
                self._comment(item, text)
            except ApiError as exc:
                logger.warning("%s %d at ceiling: notice not posted, retrying "
                               "next tick: %s", kind, item.id, exc)
                return ["%s %d at ceiling: notice pending" % (kind, item.id)]
            self._store_ceiling(kind, item.id, True)
            logger.warning("%s %d at ceiling: needs lead", kind, item.id)
            return ["%s %d at ceiling: needs lead" % (kind, item.id)]
        self._store_ceiling(kind, item.id, False)
        text = ("%s run failed %d times with no result: needs lead"
                % (kind, _MAX_RUN_ATTEMPTS))
        self._move(item, statuses["Blocked"])
        self._comment(item, text)
        return ["task %d %s: %s" % (item.id, kind, "needs lead")]

    def _changes_rounds(self, item):
        """Prior review rounds that returned CHANGES for this task."""
        from opl.conductor.spark.records import read_runs

        try:
            rows = read_runs(self.settings.conductor.state_dir)
        except OSError:
            return 0
        return sum(1 for row in rows
                   if row.get("task") == item.id
                   and row.get("kind") == "review"
                   and row.get("outcome") == "review-changes")

    def _reviewer_notes(self, item):
        """Latest task comment not written by spark, oldest fallback.

        Best-guess journal shape (matches collect._status_since): entries
        under `_embedded.elements` with `comment.raw` and
        `_links.author` carrying a title or href. Entries whose author
        mentions spark are skipped first; when every comment is spark's
        (the usual review-notes case) the latest one is used.
        """
        try:
            journal = self.op.get(
                "/api/v3/work_packages/%d/activities" % item.id) or {}
        except ApiError:
            return ""
        elements = (journal.get("_embedded", {}).get("elements", []) or [])
        fallback = ""
        for entry in elements:
            comment = entry.get("comment") or {}
            raw = (comment.get("raw") or "").strip() if isinstance(comment, dict) \
                else str(comment).strip()
            if not raw:
                continue
            fallback = raw
            author = ((entry.get("_links", {}) or {}).get("author", {}) or {})
            who = " ".join(str(author.get(key, ""))
                           for key in ("title", "href", "name")).lower()
            if "spark" not in who:
                return raw
        return fallback

    def _settings_project(self, key):
        for project in self.settings.projects:
            if project.key == key:
                return project
        return None

    def _origin_problem(self, sproject, remote_cache):
        """The checkout's origin must be this repo on github.com (TH.18).

        Reads the raw config (never the insteadOf expansion, which tests
        use to stay offline). Foreign hosts and local paths fail, even
        when the slug matches.
        """
        if sproject.key not in remote_cache:
            try:
                url = self._sh(["git", "-C", sproject.local_repo, "config",
                                "--get", "remote.origin.url"],
                               sproject.local_repo)
            except RuntimeError:
                url = ""
            remote_cache[sproject.key] = url
        url = remote_cache[sproject.key]
        host, slug = _origin_host_slug(url)
        if host != "github.com" or slug != sproject.repo.strip("/").lower():
            return ("the local checkout's origin is not %s on github.com "
                    "(got %r)" % (sproject.repo, url))
        return None

    def _privacy_problem(self, sproject, priv_cache):
        """The GitHub repo must actually be public (TH.18).

        Cached per tick. A failed lookup refuses fail-closed.
        """
        try:
            owner, repo = sproject.repo.split("/", 1)
        except ValueError:
            return "configured repo %r is not owner/name" % sproject.repo
        key = (owner, repo)
        if key not in priv_cache:
            try:
                priv_cache[key] = self.gh.repo_private(owner, repo)
            except ApiError:
                return "could not verify the repo is public"
        if priv_cache[key]:
            return "the GitHub repo %s is not public" % sproject.repo
        return None

    def _dispatch_problem(self, world, kind, item, remote_cache,
                          priv_cache):
        """Why a run must not start now, or None (TH.6).

        Re-checked immediately before every build, fix, review and test run:
        status Ready (or In review / In test) alone proves nothing, because
        a model can set it by hand.
        """
        project = world.projects.get(item.project)
        if project is None or project.visibility != "Public":
            return "project is not Public"
        sproject = self._settings_project(item.project)
        if sproject is None or not sproject.local_repo:
            return "no local repo configured for the project"
        origin = self._origin_problem(sproject, remote_cache)
        if origin:
            return origin
        privacy = self._privacy_problem(sproject, priv_cache)
        if privacy:
            return privacy
        if not valid_pr_base(sproject.pr_base):
            return "pr base %r is not a plain branch name" % sproject.pr_base
        if "/" in sproject.pr_base:
            try:
                remotes = self._sh(["git", "-C", sproject.local_repo,
                                    "remote"], sproject.local_repo)
            except RuntimeError:
                remotes = ""
            if sproject.pr_base.split("/", 1)[0] in remotes.split():
                return ("pr base %r starts with a local remote name"
                        % sproject.pr_base)
        bad_ref = self._start_ref_ok(sproject)
        if bad_ref:
            return bad_ref
        parent = world.items.get(item.parent_id) if item.parent_id else None
        if kind == "test":
            if (parent is None or parent.type != "Epic"
                    or parent.project != item.project):
                return "feature is not under an Epic in the same project"
            return None
        if (parent is None or parent.type != "Feature"
                or parent.project != item.project):
            return "task is not under a Feature in the same project"
        if parent.status not in ("Approved", "Building"):
            return "feature %d is %s, not Approved/Building" % (
                parent.id, parent.status)
        if kind == "build":
            for pid in item.predecessors:
                pred = world.items.get(pid)
                if pred is None or pred.status != "Merged":
                    return "predecessor %s is not Merged" % pid
        return None

    # -- main entry ----------------------------------------------------------
    def tick(self, world):
        """Conclude finished runs, start new ones, return actions.

        Never blocks on workers: build, review, test and fix runs execute
        in background threads and their outcomes are concluded on later
        ticks. Records stay in `_active` (and slots held) until concluded.
        """
        actions = []
        statuses = self._status_ids()
        actions.extend(self._reap(world, statuses))
        jobs = []
        for item in self._candidates(world):
            jobs.append((("build", item.id), item))
        for item in self._review_candidates(world):
            jobs.append((("review", item.id), item))
        for item in self._test_candidates(world):
            jobs.append((("test", item.id), item))
        for item in self._fix_candidates(world):
            if self._changes_rounds(item) >= 2:
                self._move(item, statuses["Blocked"])
                self._comment(item, "review loop: 2 rounds of changes "
                                    "requested; needs a human decision")
                actions.append("task %d review loop: 2 rounds, moved to Blocked"
                               % item.id)
                continue
            jobs.append((("fix", item.id), item))
        remote_cache = {}
        priv_cache = {}
        for key, item in jobs:
            if key[0] in ("review", "test") and self._attempt_count(
                    key[0], item.id) >= _MAX_RUN_ATTEMPTS:
                if not self._rearm_if_moved(key[0], item):
                    actions.extend(self._ceiling_block(world, key[0], item,
                                                       statuses))
                    continue
            problem = self._dispatch_problem(world, key[0], item,
                                             remote_cache, priv_cache)
            if problem:
                actions.extend(self._not_started(key[0], item, statuses,
                                                 problem))
                continue
            if not self.slots.acquire(key):
                continue
            record = {"key": key, "task_id": item.id, "attempts": 0,
                      "done": threading.Event()}
            with self._lock:
                self._active[key] = record
            thread = threading.Thread(
                target=self._run_bound, args=(record, item, world, statuses),
                daemon=True)
            thread.start()
        return actions

    def _not_started(self, kind, item, statuses, problem):
        """A dispatch failure means no run: Blocked for tasks (with the
        reason in a comment), a warning action for anything else."""
        if item.type == "Task":
            self._move(item, statuses["Blocked"])
            self._comment(item, "Not started (%s run): %s" % (kind, problem))
            return ["task %d not started: %s" % (item.id, problem)]
        logger.warning("%s run for %d skipped: %s", kind, item.id, problem)
        return ["%s %d not started: %s" % (item.type.lower(), item.id,
                                           problem)]

    def _reap(self, world, statuses):
        """Conclude runs finished since the last tick, in task order."""
        with self._lock:
            ready = [(tid, r) for tid, r in self._active.items()
                     if r["done"].is_set()]
        actions = []
        errors = []
        for task_id, record in sorted(ready):
            if "error" in record:
                errors.append(record["error"])
            else:
                actions.extend(
                    self._conclude(record, record["result"], world, statuses))
            with self._lock:
                self._active.pop(task_id, None)
            self.slots.release(task_id)
        if errors:
            raise errors[0]
        return actions

    # -- lifecycle for the conductor (TH.9) ----------------------------------
    def pending(self):
        """How many runs are started but not yet concluded."""
        with self._lock:
            return len(self._active)

    def drain(self, world, timeout=None):
        """Wait for every started run to finish, then conclude them.

        For single-cycle (`--once`) use: nothing is left running or
        unconcluded when the conductor exits. Returns the actions.
        """
        with self._lock:
            records = list(self._active.values())
        for record in records:
            record["done"].wait(timeout)
        return self._reap(world, self._status_ids())

    def shutdown(self, timeout=60):
        """Stop every running worker's process tree (clean exit).

        Keeps calling terminate_all() until every active record is done
        or one bounded deadline passes, so workers that spawn late (still
        preparing during an earlier call) are caught too. Killed runs
        report "failed" to their threads; they are not concluded here, so
        their worktrees stay as evidence. Returns how many worker
        processes were stopped in total.
        """
        # This runner's threads never spawn again, even if a later runner
        # in the same process reopens the process-wide gate (TH.23).
        self._stop.set()
        deadline = time.monotonic() + max(timeout, 0)
        stopped = 0
        while True:
            stopped += terminate_all()
            with self._lock:
                pending = [r for r in self._active.values()
                           if not r["done"].is_set()]
            if not pending:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        return stopped

    def _run_bound(self, record, item, world, statuses):
        """Run-thread entry: bind this runner's stop gate, then run."""
        bind_stop(self._stop)
        self._run_guarded(record, item, world, statuses)

    def _run_guarded(self, record, item, world, statuses):
        try:
            kind = record["key"][0]
            stale = self._fresh_problem(record, item, world)
            if stale is not None:
                record["result"] = {"kind": "stale", "run_kind": kind,
                                    "item": item, "error": stale}
            elif kind == "build":
                record["result"] = self._run_one(record, world, statuses)
            elif kind == "review":
                record["result"] = self._run_review(
                    record, world, item, statuses)
            elif kind == "fix":
                record["result"] = self._run_fix(record, world, item, statuses)
            else:
                record["result"] = self._run_test(record, world, item, statuses)
        except _StaleRun as exc:
            record["result"] = {"kind": "stale", "run_kind": record["key"][0],
                                "item": item, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - re-raised at reap
            record["error"] = exc
        finally:
            record["done"].set()

    # Statuses that qualify a run at dispatch time, per kind (TH.18): the
    # run thread re-reads them live and refuses a stale world.
    _FRESH_STATUS = {"build": "Ready", "review": "In review",
                     "test": "In test", "fix": "In progress"}

    @staticmethod
    def _tail_id(href):
        try:
            return str(href).rstrip("/").rsplit("/", 1)[-1].split("?")[0]
        except (AttributeError, TypeError, ValueError):
            return ""

    # Task statuses that may stand at spawn time: the qualifying one, or
    # "In progress" that Spark itself set for a build (TH.22).
    _SPAWN_STATUS = {"build": ("Ready", "In progress"),
                     "review": ("In review",), "test": ("In test",),
                     "fix": ("In progress",)}

    def _fresh_problem(self, record, item, world):
        """Re-read what authorizes this run before preparing it (TH.18).

        Stores the live authorization in the record, so every spawn can
        check nothing changed while the worktree and fetch were prepared
        (TH.22, Codex F4). Returns the reason not to run, or None.
        """
        kind = record["key"][0]
        problem, auth = self._authorization(kind, item, world,
                                            (self._FRESH_STATUS.get(kind),))
        if problem is None:
            record["auth"] = auth
        return problem

    def _guard_spawn(self, record, item, world):
        """Right before a worker starts (and before each retry): the live
        authorization must equal what qualified the run. Raises _StaleRun
        with the reason otherwise; no worker starts (TH.22, Codex F4).
        Returns the (task, parent) elements it read, for the packet."""
        kind = record["key"][0]
        problem, auth = self._authorization(kind, item, world,
                                            self._SPAWN_STATUS.get(kind, ()))
        before = _public(record.get("auth") or {})
        if problem is None and _public(auth) != before:
            changed = sorted(k for k in set(_public(auth)) | set(before)
                             if _public(auth).get(k) != before.get(k))
            problem = "changed while preparing: %s" % ", ".join(changed)
        if problem is not None:
            raise _StaleRun(problem)
        # The prompt is built from exactly these reads (Codex final2 #1).
        return auth["_elements"]

    def _authorization(self, kind, item, world, statuses_ok):
        """(problem, live authorization) for a run of `kind` on `item`.

        The task (or feature, for test runs) must stand in one of
        `statuses_ok`, in the same project, under the same parent the run
        was planned with; the parent must still be Approved/Building (an
        Epic for test runs). Builds and fixes also need every predecessor
        Merged, read live so a predecessor added meanwhile counts. The
        returned snapshot (parent, assignee, reviewer, predecessors) is
        what later spawns compare against. Anything unreadable refuses.
        """
        project = world.projects.get(item.project)
        if project is None:
            return "project is gone", {}
        try:
            task_el = self.op.get("/api/v3/work_packages/%d" % item.id) or {}
            id_to_status = {
                str(e.get("id")): e.get("name")
                for e in self.op.get_all("/api/v3/statuses")
                if isinstance(e, dict)}
            id_to_type = {
                str(e.get("id")): e.get("name")
                for e in self.op.get_all("/api/v3/types")
                if isinstance(e, dict)}
        except ApiError as exc:
            return "could not re-read the task: %s" % exc, {}
        links = task_el.get("_links", {}) or {}
        status = id_to_status.get(self._tail_id(
            (links.get("status") or {}).get("href", "")))
        if status not in statuses_ok:
            return ("task is %s, not %s" % (status or "unknown",
                                            "/".join(s for s in statuses_ok if s)),
                    {})
        if self._tail_id((links.get("project") or {}).get("href", "")) != str(
                project.op_id):
            return "task moved to another project", {}
        parent_href = (links.get("parent") or {}).get("href")
        if not parent_href:
            return "task has no parent", {}
        parent_id = self._tail_id(parent_href)
        if item.parent_id is not None and parent_id != str(item.parent_id):
            return "task was moved to another parent", {}
        try:
            parent_el = self.op.get("/api/v3/work_packages/%s" % parent_id) or {}
        except ApiError as exc:
            return "could not re-read the parent: %s" % exc, {}
        plinks = parent_el.get("_links", {}) or {}
        if self._tail_id((plinks.get("project") or {}).get("href", "")) != str(
                project.op_id):
            return "parent is in another project", {}
        parent_type = id_to_type.get(self._tail_id(
            (plinks.get("type") or {}).get("href", "")))
        parent_status = id_to_status.get(self._tail_id(
            (plinks.get("status") or {}).get("href", "")))
        auth = {"parent": parent_id,
                "assignee": (links.get("assignee") or {}).get("href"),
                "_elements": (task_el, parent_el)}
        if kind == "test":
            if parent_type != "Epic":
                return "feature is not under an Epic", {}
            return None, auth
        if parent_type != "Feature":
            return "task is not under a Feature", {}
        if parent_status not in ("Approved", "Building"):
            return ("feature is %s, not Approved/Building"
                    % (parent_status or "unknown"), {})
        if item.assignee == "spark" and kind in ("build", "fix"):
            title = str((links.get("assignee") or {}).get("title") or "").lower()
            if "spark" not in title.replace(",", " ").split():
                return "task is no longer assigned to Spark", {}
        try:
            reviewer, pr_link, has_pr_field = self._task_fields(
                task_el, project, links)
        except ApiError as exc:
            return "could not read the task's schema: %s" % exc, {}
        auth["reviewer"] = reviewer
        if kind in ("review", "fix"):
            # Which PR is reviewed or fixed is part of the authorization:
            # the field must exist and hold exactly the planned link, else
            # the run refuses (Codex final2 #2, final3 D1: never fail open).
            if not has_pr_field:
                return "the task's type has no PR link field", {}
            if not pr_link:
                return "the task has no PR link", {}
            if pr_link != (item.pr_url or ""):
                return "PR link changed to %r" % (pr_link,), {}
            auth["pr"] = pr_link
        if kind in ("build", "fix"):
            try:
                preds = self._live_predecessors(item.id)
                open_preds = []
                for pid in preds:
                    el = self.op.get("/api/v3/work_packages/%s" % pid) or {}
                    pstatus = id_to_status.get(self._tail_id(
                        ((el.get("_links") or {}).get("status") or {}).get("href", "")))
                    if pstatus != "Merged":
                        open_preds.append(pid)
            except ApiError as exc:
                return "could not re-read predecessors: %s" % exc, {}
            if open_preds:
                return ("predecessor %s is not Merged"
                        % ", ".join("#%s" % p for p in open_preds), {})
            auth["predecessors"] = tuple(preds)
        return None, auth

    def _task_fields(self, task_el, project, links):
        """(Reviewer href, PR link, whether the type has a PR link field),
        read through the task's schema."""
        tid = self._tail_id((links.get("type") or {}).get("href", ""))
        schema = self.op.get("/api/v3/work_packages/schemas/%s-%s"
                             % (project.op_id, tid)) or {}
        fields = hal.schema_fields(schema)
        reviewer = (links.get(fields["Reviewer"]) or {}).get("href") \
            if "Reviewer" in fields else None
        pr_link = hal.custom_value(task_el, fields["PR link"]) \
            if "PR link" in fields else None
        return reviewer, pr_link, "PR link" in fields

    def _live_predecessors(self, task_id):
        """Ids of the task's predecessors, read from its live relations."""
        found = set()
        for rel in self.op.get_all("/api/v3/relations", {"filters": json.dumps(
                [{"involved": {"operator": "=", "values": [str(task_id)]}}])}):
            frm = self._tail_id(((rel.get("_links") or {}).get("from") or {}).get("href", ""))
            to = self._tail_id(((rel.get("_links") or {}).get("to") or {}).get("href", ""))
            kind = str(rel.get("type", ""))
            if kind == "precedes" and to == str(task_id) and frm:
                found.add(frm)
            elif kind == "follows" and frm == str(task_id) and to:
                found.add(to)
        return sorted(found, key=lambda x: (len(x), x))

    # -- one run (possibly retried once) --------------------------------------
    def _redact(self, text):
        return redact_text(text, self.settings)

    def _sh(self, args, cwd, timeout=120, env=None):
        return _sh(args, cwd, timeout, env, self._redact)

    def _record(self, kind, item, size, started, result, outcome,
                worktree_path="", commit=""):
        """Append one runs.jsonl line; recording never breaks a run."""
        try:
            record_run(self.settings.conductor.state_dir,
                       task=item.id, kind=kind, size=size or "-",
                       started=started, ended=_utcnow(),
                       duration_s=result.duration_s, outcome=outcome,
                       cost_usd=result.cost_usd,
                       worktree=worktree_path,
                       commit=commit or self._head_commit(worktree_path))
        except OSError as exc:
            logger.warning("run record failed: %s", exc)

    def _head_commit(self, worktree_path):
        """HEAD sha of a worktree, or "" when it is gone."""
        if not worktree_path:
            return ""
        try:
            return self._sh(["git", "-C", worktree_path, "rev-parse", "HEAD"],
                            worktree_path)
        except RuntimeError:
            return ""

    def _patch_item(self, item, body):
        """PATCH a work package with a freshly fetched lockVersion."""
        current = self.op.get("/api/v3/work_packages/%d" % item.id) or {}
        payload = dict(body)
        payload["lockVersion"] = current.get("lockVersion", item.lock_version)
        return self.op.patch("/api/v3/work_packages/%d" % item.id, payload)

    @staticmethod
    def _raw_desc(value):
        if isinstance(value, dict):
            return value.get("raw", "") or ""
        return value or ""

    @staticmethod
    def _split_sections(text):
        """Split a feature description on the DESIGN §1 template headings.

        Each section runs from its heading line up to the next template
        heading or the end of the text, so multi-line bodies (checklists,
        paragraphs) survive. Each missing section falls back to the whole
        description text.
        """
        full = (text or "").strip()
        heads = (("why", "why"), ("what", "what you'll see"),
                 ("done_when", "done when"))
        starts = {}
        for key, head in heads:
            match = re.search(r"^%s:\s*(.*)$" % re.escape(head), full,
                              re.MULTILINE | re.IGNORECASE)
            if match:
                starts[key] = match
        sections = {}
        for key, _head in heads:
            if key not in starts:
                sections[key] = full
                continue
            match = starts[key]
            begin = match.start(1)
            following = [m.start() for k, m in starts.items()
                         if m.start() > match.start()]
            end = min(following) if following else len(full)
            sections[key] = full[begin:end].strip()
        return sections

    # -- one run (possibly retried once) --------------------------------------
    def _run_one(self, record, world, statuses):
        item = world.items[record["task_id"]]
        sproject = self._settings_project(item.project)
        if sproject is None or not sproject.local_repo:
            raise ValueError("no local_repo configured for project %r"
                             % (item.project,))
        minutes = self.settings.runner.limits_minutes.get(item.size or "M",
                                                           self.settings.runner.limits_minutes.get("M", 45))
        stall_min = self.settings.runner.limits_minutes.get("stall", 10)
        last_error = ""
        while record["attempts"] < 2:
            record["attempts"] += 1
            outcome = self._attempt(record, world, item, sproject, statuses,
                                    minutes, stall_min, last_error)
            if outcome["done"]:
                outcome["item"] = item
                outcome["minutes"] = minutes
                return outcome
            last_error = outcome["error"]
        return {"kind": "failed", "error": last_error,
                "minutes": minutes, "item": item}

    def _attempt(self, record, world, item, sproject, statuses, minutes,
                 stall_min, last_error):
        task_id = item.id
        logger.info("task %d: attempt %d", task_id, record["attempts"])
        # Prepare first, then claim the task: if the worktree can't be made
        # (disk full, broken repo), the task is Blocked with the reason
        # instead of being left In progress, where no build picks it up
        # again (PR #6 review).
        try:
            worktree = create_worktree(sproject.local_repo, sproject.base_ref,
                                       "T%d" % task_id,
                                       self.settings.conductor.state_dir)
        except RuntimeError as exc:
            raise _StaleRun("could not prepare the worktree: %s" % exc)
        self._move(item, statuses["In progress"])
        try:
            base = self._sh(["git", "-C", worktree.path, "rev-parse", "HEAD"],
                            worktree.path)
        except RuntimeError:
            base = None
        try:
            # Immediately before this attempt's worker; the packet below is
            # built from exactly these reads (TH.22, Codex F4).
            elements = self._guard_spawn(record, item, world)
        except _StaleRun:
            # Nothing ran in this fresh tree; drop it and report why.
            try:
                remove_worktree(worktree)
            except RuntimeError:
                pass
            raise
        packet_dir = os.path.join(self.settings.conductor.state_dir, "packets")
        os.makedirs(packet_dir, exist_ok=True)
        packet_path = os.path.join(
            packet_dir, "task-%d-%d.md" % (task_id, record["attempts"]))
        task_view, feature_view, project_view = self._views(world, item,
                                                            elements)
        packet = build_packet("build", task_view, feature_view,
                              project_view, self.settings)
        if last_error:
            packet += ("\n## Previous attempt failed\n\n"
                       "The last run failed with:\n\n```\n" + last_error +
                       "\n```\nFix the cause and finish the task.\n")
        # The packet is the worker's whole prompt: scrub it last, after the
        # retry error is appended (TH.2).
        packet = self._redact(packet)
        with open(packet_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(packet)
        log_path = os.path.join(
            self.settings.conductor.state_dir, "logs",
            "run-%d-%d.log" % (task_id, record["attempts"]))
        cmd = [part.replace("{packet}", packet_path).replace("{workdir}", worktree.path)
               for part in self.settings.runner.command]
        started = _utcnow()
        result = run_worker(cmd, worktree.path, minutes * 60, stall_min * 60,
                            log_path, env=self._worker_env(log_path))
        logger.info("task %d: worker %s", task_id, result.outcome)
        branch, path = worktree.branch, worktree.path
        if result.outcome in ("timeout", "stalled"):
            keep_partial_work(path, task_id, record["attempts"])
            self._record("build", item, item.size, started, result,
                         result.outcome, path)
            return {"done": True, "kind": result.outcome,
                    "minutes": minutes, "item": item,
                    "branch": branch, "path": path,
                    "lines": list(result.last_lines)}
        if result.outcome == "success":
            verdict, message = parse_final_line(result.last_lines)
            if verdict == "done" and self._delivered(path, base):
                self._record("build", item, item.size, started, result,
                             "success", path)
                return {"done": True, "kind": "success",
                        "branch": branch, "path": path,
                        "minutes": minutes, "item": item,
                        "cost": result.cost_usd}
            if verdict == "done":
                message = "uncommitted work"
            keep_partial_work(path, task_id, record["attempts"])
            lines = list(result.last_lines) or ["worker produced no output"]
            self._record("build", item, item.size, started, result, "failed",
                         path)
            return {"done": False, "error": message or "\n".join(lines[-10:]),
                    "branch": branch, "path": path}
        lines = list(result.last_lines) or ["worker produced no output"]
        keep_partial_work(path, task_id, record["attempts"])
        self._record("build", item, item.size, started, result, "failed",
                     path)
        return {"done": False, "error": "\n".join(lines[-10:]),
                "branch": branch, "path": path}

    def _delivered(self, worktree_path, base):
        """Clean tree plus commits beyond the recorded base, or False."""
        try:
            if self._sh(["git", "-C", worktree_path, "status", "--porcelain"],
                        worktree_path):
                return False
            if base is None:
                return False
            ahead = self._sh(["git", "-C", worktree_path, "rev-list", "--count",
                              "%s..HEAD" % base], worktree_path)
        except RuntimeError:
            return False
        return ahead.strip() not in ("", "0")

    def _views(self, world, item, elements=None):
        """Task/feature/project views for packets.

        With `elements` (the spawn guard's (task, parent) reads) the packet
        describes exactly the state that was authorized; otherwise both are
        read live.
        """
        task_el = (elements[0] if elements else
                   self.op.get("/api/v3/work_packages/%d" % item.id) or {})
        task_view = {"title": task_el.get("subject") or "Task %d" % item.id,
                     "description": self._raw_desc(task_el.get("description")),
                     "size": item.size}
        feature_view = {}
        parent = world.items.get(item.parent_id) if item.parent_id else None
        if parent is not None:
            if elements:
                element = elements[1] or {}
            else:
                try:
                    element = self.op.get(
                        "/api/v3/work_packages/%d" % parent.id) or {}
                except ApiError:
                    element = {}
            feature_view = self._split_sections(
                self._raw_desc(element.get("description")))
            feature_view["title"] = element.get("subject") or "Feature %d" % parent.id
        project = world.projects.get(item.project)
        project_view = {"key": getattr(project, "key", "?"),
                        "visibility": getattr(project, "visibility", "")}
        return task_view, feature_view, project_view

    def _run_packet(self, packet, worktree_path, minutes, log_path,
                      label, task_id, guard=None):
        """Write the packet, run the worker once, return the RunResult.

        `guard` runs immediately before the worker starts and raises
        _StaleRun when the run is no longer authorized (TH.22); it returns
        the reads it checked. `packet` may be a function of those reads, so
        the prompt describes exactly the authorized state.
        """
        elements = guard() if guard is not None else None
        if callable(packet):
            packet = packet(elements)
        packet_dir = os.path.join(self.settings.conductor.state_dir, "packets")
        os.makedirs(packet_dir, exist_ok=True)
        packet_path = os.path.join(packet_dir, "%s-%d.md" % (label, task_id))
        # Scrubbed last: reviewer notes and tracker text are already in (TH.2).
        packet = self._redact(packet)
        with open(packet_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(packet)
        cmd = [part.replace("{packet}", packet_path).replace("{workdir}", worktree_path)
               for part in self.settings.runner.command]
        stall_min = self.settings.runner.limits_minutes.get("stall", 10)
        return run_worker(cmd, worktree_path, minutes * 60, stall_min * 60,
                          log_path, env=self._worker_env(log_path))

    def _drop_ref(self, sproject, worktree, kept):
        """Delete an isolated fetch ref once its worktree is gone.

        Trees kept as evidence (TH.4) keep their ref. Only `refs/opl/`
        refs are ever deleted; a failed deletion warns, since the run
        already concluded.
        """
        if kept is not None:
            return
        if not worktree.branch.startswith("refs/opl/"):
            return
        try:
            delete_ref(sproject.local_repo, worktree.branch)
        except RuntimeError as exc:
            logger.warning("review ref kept: %s", exc)

    def _dispose(self, worktree, result, kind):
        """Remove a review/test scratch tree only after a clean success.

        Returns the kept path (or None when removed). A failed, stalled or
        crashed run, or a tree with changes, stays for the lead to inspect.
        """
        succeeded = result is not None and result.outcome == "success"
        try:
            return dispose_worktree(worktree, self.settings.conductor.state_dir,
                                    succeeded)
        except RuntimeError as exc:
            logger.warning("%s worktree kept: %s", kind, exc)
            return worktree.path

    def _worker_env(self, log_path):
        """Isolated worker environment: only the configured worker_env names
        plus a fresh per-run home, so no conductor token (admin, GitHub,
        conductor, other models) and no owner tool config reaches a worker.
        Every run shares one OpenCode data dir (TH.18/TH.19) while HOME
        and the other dirs stay per run."""
        home = os.path.join(self.settings.conductor.state_dir, "homes",
                            os.path.splitext(os.path.basename(log_path))[0])
        env = worker_environment(self.settings.runner.worker_env, home)
        data = os.path.join(self.settings.conductor.state_dir, "spark-data")
        os.makedirs(data, exist_ok=True)
        env["XDG_DATA_HOME"] = data
        return env

    def _run_review(self, record, world, item, statuses):
        sproject = self._settings_project(item.project)
        if sproject is None or not sproject.local_repo:
            raise ValueError("no local_repo configured for project %r"
                             % (item.project,))
        minutes = self.settings.runner.limits_minutes.get("review", 15)
        # The link is checked before any API call with the GitHub token.
        source = _review_source_problem(sproject.repo, item.pr_url)
        if source:
            return {"kind": "stale", "run_kind": "review", "item": item,
                    "error": source}
        try:
            pr = self.gh.pull_request(item.pr_url)
        except ApiError as exc:
            return {"kind": "review-failed", "item": item,
                    "error": "could not read PR %s: %s" % (item.pr_url, exc)}
        if not pr.head_sha:
            return {"kind": "review-failed", "item": item,
                    "error": "PR %s has no head SHA" % item.pr_url}
        source = _review_source_problem(sproject.repo, item.pr_url, pr)
        if source:
            # Not this project's public code: no fetch with the GitHub
            # token and no worker; the task is blocked with the reason.
            return {"kind": "stale", "run_kind": "review", "item": item,
                    "error": source}
        # Exact head SHA from the PR's head repository into an isolated
        # ref (TH.8): fetch failure aborts, never a stale local branch.
        head_url = "https://github.com/%s.git" % pr.head_repo
        fetch_env = _push_env(
            head_url, self.gh.raw_token()
            if head_url.startswith("https://") else None)
        try:
            worktree = checkout_pr_head(
                sproject.local_repo, head_url, pr.head_sha,
                "review-%d" % item.id, self.settings.conductor.state_dir,
                fetch_env)
        except RuntimeError as exc:
            return {"kind": "review-failed", "item": item,
                    "error": str(exc), "head_sha": pr.head_sha,
                    "head_repo": pr.head_repo}
        result = None
        commit = ""
        try:
            log_path = os.path.join(
                self.settings.conductor.state_dir, "logs",
                "review-%d.log" % item.id)
            started = _utcnow()
            result = self._run_packet(
                lambda els: build_packet("review",
                                         *self._views(world, item, els),
                                         self.settings),
                worktree.path, minutes, log_path, "review", item.id,
                guard=lambda: self._guard_spawn(record, item, world))
            commit = self._head_commit(worktree.path)
        except _StaleRun:
            result = _NEVER_RAN
            raise
        finally:
            # Keep the tree as evidence unless the run succeeded cleanly
            # (TH.4); never force-remove. A tree no worker ever touched is
            # simply removed (Codex final2 #3).
            kept = self._dispose(worktree, result, "review")
            self._drop_ref(sproject, worktree, kept)
        if result.outcome != "success":
            outcome = {"kind": "review-failed", "item": item,
                       "error": "worker %s%s" % (
                           result.outcome,
                           "; kept worktree at %s" % kept if kept else "")}
        else:
            match = _REVIEW_RE.search("\n".join(result.last_lines))
            if not match:
                outcome = {"kind": "review-failed", "item": item,
                           "error": "no OPL-REVIEW line in worker output"}
            elif match.group(1).upper() == "PASS":
                outcome = {"kind": "review-pass", "item": item,
                           "head_sha": pr.head_sha, "head_repo": pr.head_repo}
            else:
                outcome = {"kind": "review-changes", "item": item,
                           "notes": match.group(2).strip() or "changes requested",
                           "to_status": statuses["In progress"],
                           "head_sha": pr.head_sha, "head_repo": pr.head_repo}
        self._record("review", item, getattr(item, "size", None),
                     started, result, outcome["kind"], worktree.path, commit)
        return outcome

    def _run_test(self, record, world, item, statuses):
        sproject = self._settings_project(item.project)
        if sproject is None or not sproject.local_repo:
            raise ValueError("no local_repo configured for project %r"
                             % (item.project,))
        minutes = self.settings.runner.limits_minutes.get("test", 20)
        if sproject.test_url:
            target = "Test target: %s" % sproject.test_url
        else:
            target = "No test environment: build main locally and walk through the feature."
        try:
            worktree = checkout_worktree(sproject.local_repo, sproject.base_ref,
                                         "test-%d" % item.id,
                                         self.settings.conductor.state_dir)
        except RuntimeError as exc:
            # A failed run like any other: counted toward the ceiling.
            return {"kind": "test-failed", "item": item,
                    "error": "could not prepare the worktree: %s" % exc}
        result = None
        commit = ""
        try:
            log_path = os.path.join(
                self.settings.conductor.state_dir, "logs",
                "test-%d.log" % item.id)
            started = _utcnow()
            result = self._run_packet(
                lambda els: build_packet("test", *self._views(world, item, els),
                                         self.settings)
                + "\n## Test target\n\n%s\n" % target,
                worktree.path, minutes, log_path, "test", item.id,
                guard=lambda: self._guard_spawn(record, item, world))
            commit = self._head_commit(worktree.path)
        except _StaleRun:
            result = _NEVER_RAN
            raise
        finally:
            # Keep the tree as evidence unless the run succeeded cleanly
            # (TH.4); never force-remove.
            kept = self._dispose(worktree, result, "test")
            self._drop_ref(sproject, worktree, kept)
        if result.outcome != "success":
            outcome = {"kind": "test-failed", "item": item,
                       "error": "worker %s%s" % (
                           result.outcome,
                           "; kept worktree at %s" % kept if kept else "")}
        else:
            match = _TEST_RE.search("\n".join(result.last_lines))
            if not match:
                outcome = {"kind": "test-failed", "item": item,
                           "error": "no OPL-TEST line in worker output"}
            else:
                passed = match.group(1).upper() == "PASS"
                outcome = {"kind": "test", "item": item, "passed": passed,
                           "summary": match.group(2).strip()
                           or ("Pass" if passed else "Fail")}
        self._record("test", item, getattr(item, "size", None),
                     started, result, outcome["kind"], worktree.path, commit)
        return outcome

    def _run_fix(self, record, world, item, statuses):
        """One fix attempt on the task's existing PR branch (no retry)."""
        sproject = self._settings_project(item.project)
        if sproject is None or not sproject.local_repo:
            raise ValueError("no local_repo configured for project %r"
                             % (item.project,))
        minutes = self.settings.runner.limits_minutes.get(item.size or "M",
                                                           self.settings.runner.limits_minutes.get("M", 45))
        source = _review_source_problem(sproject.repo, item.pr_url)
        if source:
            return {"kind": "stale", "run_kind": "fix", "item": item,
                    "error": source}
        head = self.gh.pr_head_ref(item.pr_url)
        if not head:
            return {"kind": "fix-failed", "item": item,
                    "error": "no head branch found for %s" % item.pr_url}
        branch = head.split("/", 1)[1] if head.startswith("origin/") else head
        try:
            worktree = checkout_worktree(sproject.local_repo, head,
                                         "fix-%d" % item.id,
                                         self.settings.conductor.state_dir)
        except RuntimeError as exc:
            return {"kind": "fix-failed", "item": item,
                    "error": "could not prepare the worktree: %s" % exc}
        try:
            base = self._sh(["git", "-C", worktree.path, "rev-parse", "HEAD"],
                            worktree.path)
        except RuntimeError:
            base = None
        notes = self._reviewer_notes(item)

        def fix_packet(elements):
            packet = build_packet("build", *self._views(world, item, elements),
                                  self.settings)
            if notes:
                packet += ("\n## Reviewer notes\n\nAddress this feedback:\n\n"
                           + notes + "\n")
            return packet

        log_path = os.path.join(
            self.settings.conductor.state_dir, "logs",
            "fix-%d.log" % item.id)
        started = _utcnow()
        try:
            result = self._run_packet(
                fix_packet, worktree.path, minutes, log_path, "fix", item.id,
                guard=lambda: self._guard_spawn(record, item, world))
        except _StaleRun:
            # No worker touched this tree: remove it (Codex final2 #3).
            self._dispose(worktree, _NEVER_RAN, "fix")
            raise
        if result.outcome in ("timeout", "stalled"):
            keep_partial_work(worktree.path, item.id, 1)
            self._record("fix", item, item.size, started, result,
                         result.outcome, worktree.path)
            return {"done": True, "kind": result.outcome,
                    "minutes": minutes, "item": item,
                    "branch": branch, "path": worktree.path,
                    "lines": list(result.last_lines)}
        if result.outcome == "success":
            verdict, message = parse_final_line(result.last_lines)
            if verdict == "done" and self._delivered(worktree.path, base):
                self._record("fix", item, item.size, started, result,
                             "fix-success", worktree.path)
                return {"done": True, "kind": "fix-success",
                        "branch": branch, "path": worktree.path,
                        "minutes": minutes, "item": item,
                        "cost": result.cost_usd}
            if verdict == "done":
                message = "uncommitted work"
            keep_partial_work(worktree.path, item.id, 1)
            lines = list(result.last_lines) or ["worker produced no output"]
            error = message or "\n".join(lines[-10:])
        else:
            lines = list(result.last_lines) or ["worker produced no output"]
            keep_partial_work(worktree.path, item.id, 1)
            error = "worker %s: %s" % (result.outcome, "\n".join(lines[-10:]))
        self._record("fix", item, item.size, started, result, "fix-failed",
                     worktree.path)
        return {"done": True, "kind": "fix-failed", "item": item,
                "minutes": minutes, "branch": branch, "path": worktree.path,
                "error": error}

    # -- outcome handling (main thread, deterministic order) -------------------
    def _conclude(self, record, outcome, world, statuses):
        item = outcome["item"]
        kind = outcome["kind"]
        if kind == "success":
            return self._conclude_success(record, outcome, world, statuses)
        if kind == "fix-success":
            return self._conclude_fix_success(record, outcome, world, statuses)
        branch = outcome.get("branch", "?")
        if kind in ("timeout", "stalled"):
            text = ("%s after %s min on branch %s; options: more time, split, reassign"
                    % (kind, outcome["minutes"], branch))
            self._move(item, statuses["Blocked"])
            self._comment(item, text)
            return ["task %d %s: kept branch %s, moved to Blocked" % (item.id, kind, branch)]
        if kind in ("review-pass", "review-changes", "test"):
            return self._conclude_check(outcome, world)
        if kind in ("review-failed", "test-failed"):
            self._bump_attempts(record["key"][0], item.id)
            self._comment(item, "%s run failed: %s"
                          % (record["key"][0], outcome.get("error", "unknown error")))
            return ["%s %d failed: left %s" % (
                record["key"][0], item.id,
                "review_result unset" if kind == "review-failed" else "test_result unset")]
        if kind == "fix-failed":
            self._move(item, statuses["Blocked"])
            self._comment(item, "fix run failed on branch %s: %s"
                          % (branch, outcome.get("error", "unknown error").splitlines()[-1][:500]))
            return ["fix task %d failed: moved to Blocked" % item.id]
        if kind == "stale":
            text = ("Not started (%s run): %s"
                    % (outcome.get("run_kind", "?"),
                       outcome.get("error", "unknown error")))
            if item.type == "Task":
                self._move(item, statuses["Blocked"])
                self._comment(item, text)
                return ["task %d not started: %s"
                        % (item.id, outcome.get("error", "unknown error"))]
            self._comment(item, text)
            return ["%s %d not started: %s"
                    % (item.type.lower(), item.id,
                       outcome.get("error", "unknown error"))]
        error = outcome.get("error", "unknown error")
        self._move(item, statuses["Blocked"])
        self._comment(item, "failed twice on branch %s: %s"
                      % (branch, error.splitlines()[-1][:500]))
        return ["task %d failed twice: moved to Blocked" % item.id]

    def _conclude_success(self, record, outcome, world, statuses):
        item = outcome["item"]
        actions = []
        remote = self._sh(["git", "-C", outcome["path"], "config", "--get",
                           "remote.origin.url"], outcome["path"])
        push_env = _push_env(
            remote, self.gh.raw_token() if remote.startswith("https://") else None)
        self._sh(["git", "push", "origin", outcome["branch"]],
                 outcome["path"], env=push_env)
        actions.append("task %d: pushed %s" % (item.id, outcome["branch"]))
        owner, repo = self._settings_project(item.project).repo.split("/", 1)
        title = "Task %d" % item.id
        feature_name = ""
        try:
            subject = self.op.get("/api/v3/work_packages/%d" % item.id).get("subject")
            if subject:
                title += ": %s" % subject
            parent = world.items.get(item.parent_id)
            if parent is not None:
                feature_name = (self.op.get(
                    "/api/v3/work_packages/%d" % parent.id) or {}).get("subject", "")
        except ApiError:
            pass
        base_url = self.settings.openproject.url.rstrip("/")
        body = ("Implements %s/work_packages/%d\n\nFeature: %s"
                % (base_url, item.id, feature_name or "(unknown)"))
        pr_url = self.gh.find_open_pr(owner, repo, outcome["branch"],
                                      self._pr_base(item))
        if pr_url:
            verb = "reusing open PR"
        else:
            try:
                created = self.gh.create_pull(owner, repo, title,
                                              outcome["branch"],
                                              self._pr_base(item), body)
            except ApiError as exc:
                if exc.status != 422:
                    raise
                pr_url = self.gh.find_open_pr(owner, repo, outcome["branch"],
                                              self._pr_base(item))
                if not pr_url:
                    raise
                verb = "reusing open PR"
            else:
                pr_url = created.get("html_url", "")
                verb = "opened PR"
        lookups = _LiveLookups(self.op)
        tid = lookups.types.get(item.type)
        prop = lookups.custom_prop("PR link",
                                   world.projects[item.project].op_id, tid)
        self._patch_item(item, {
            "_links": {
                "status": {"href": "/api/v3/statuses/%s" % statuses["In review"]}},
            prop: pr_url})
        actions.append("task %d: %s %s, moved to In review"
                       % (item.id, verb, pr_url))
        return actions

    def _conclude_fix_success(self, record, outcome, world, statuses):
        """Push the same PR branch, clear Review result, back to In review."""
        item = outcome["item"]
        actions = []
        remote = self._sh(["git", "-C", outcome["path"], "config", "--get",
                           "remote.origin.url"], outcome["path"])
        push_env = _push_env(
            remote, self.gh.raw_token() if remote.startswith("https://") else None)
        # Detached worktree: push HEAD onto the existing PR branch.
        self._sh(["git", "push", "origin", "HEAD:" + outcome["branch"]],
                 outcome["path"], env=push_env)
        actions.append("fix task %d: pushed %s" % (item.id, outcome["branch"]))
        lookups = _LiveLookups(self.op)
        tid = lookups.types.get(item.type)
        prop, link = lookups.option_href(
            "review_result", None, world.projects[item.project].op_id, tid,
            item.id)
        self._patch_item(item, {
            "_links": {
                "status": {"href": "/api/v3/statuses/%s" % statuses["In review"]},
                prop: link}})
        actions.append("fix task %d: cleared review result, moved to In review"
                       % item.id)
        return actions

    def _conclude_check(self, outcome, world):
        item = outcome["item"]
        lookups = _LiveLookups(self.op)
        tid = lookups.types.get(item.type)
        pid = world.projects[item.project].op_id
        kind = outcome["kind"]
        if kind == "review-pass":
            # Bind the Pass to the exact commit reviewed (TH.5): the line
            # goes in first, so the conductor never sees an unbound Pass.
            sha = outcome.get("head_sha") or ""
            if not sha:
                self._comment(item, "review passed but the PR head SHA is "
                                    "unknown; Review result left unset")
                return ["review task %d: pass without head SHA, left unset"
                        % item.id]
            self._comment(item, "Spark review passed.\n\nreviewed: %s" % sha)
            prop, link = lookups.option_href("review_result", "Pass", pid, tid,
                                             item.id)
            self._patch_item(item, {"_links": {prop: link}})
            self._reset_attempts("review", item.id)
            return ["review task %d: Pass at %s" % (item.id, sha[:12])]
        if kind == "review-changes":
            prop, link = lookups.option_href("review_result", "Changes requested",
                                             pid, tid, item.id)
            self._patch_item(item, {
                "_links": {
                    "status": {"href": "/api/v3/statuses/%s" % outcome["to_status"]},
                    prop: link}})
            self._comment(item, outcome["notes"])
            self._reset_attempts("review", item.id)
            return ["review task %d: changes requested" % item.id]
        prop, link = lookups.option_href(
            "test_result", "Pass" if outcome["passed"] else "Fail", pid, tid,
            item.id)
        self._patch_item(item, {"_links": {prop: link}})
        self._comment(item, outcome["summary"])
        self._reset_attempts("test", item.id)
        return ["test feature %d: %s" % (item.id, "Pass" if outcome["passed"] else "Fail")]

    def _pr_base(self, item):
        sproject = self._settings_project(item.project)
        return sproject.pr_base if sproject else "main"

    def _start_ref_ok(self, sproject):
        """The local start ref resolves in the checkout, or why not."""
        ref = sproject.base_ref or "main"
        if ref.startswith("-") or "\x00" in ref or "\n" in ref:
            return "start ref %r is not a plain ref" % ref
        try:
            self._sh(["git", "-C", sproject.local_repo, "rev-parse",
                      "--verify", "--quiet", ref + "^{commit}"],
                     sproject.local_repo)
        except RuntimeError:
            return "start ref %r does not resolve in the local checkout" % ref
        return None

    # -- small API helpers (spark token client) ---------------------------------
    def _sh(self, args, cwd, timeout=120, env=None):
        return _sh(args, cwd, timeout, env, self._redact)

    def _move(self, item, status_id):
        self._patch_item(item, {
            "_links": {"status": {"href": "/api/v3/statuses/%s" % status_id}}})

    def _comment(self, item, text):
        # Every tracker comment passes the scrubber: comments often carry
        # worker output, which may echo secrets (TH.2).
        self.op.post("/api/v3/work_packages/%d/activities" % item.id,
                     {"comment": {"raw": self._redact(text)}})

    def _status_ids(self):
        ids = {}
        for element in self.op.get_all("/api/v3/statuses"):
            if element.get("name") is not None:
                ids[element["name"]] = element.get("id")
        for name in ("In progress", "In review", "Blocked"):
            if name not in ids:
                raise ApiError(0, "/api/v3/statuses",
                               "status %r not found" % (name,))
        return ids
