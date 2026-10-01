"""Readiness gate (issue #44): no work in a project that is not ready.

Wraps `opl.projectcheck.check_project`. The result per project is cached in
`<state_dir>/readiness.json`; a project is re-checked once per conductor
start and then at most once a day. The check can take minutes (the smoke run
installs and tests the project in a fresh clone), so it runs on a background
thread: a cycle never waits for it and only reads the last known answer.

`issues(keys)` returns {project key: (level, text)} for World.not_ready:

- "fail": a check failed; text is `Project not ready: <item> - <fix>`.
- "warn": only the token expires soon; text says so. Never stops work.
- "pending": no answer yet (first ever check running); stops work, no action.

A project absent from the result is ready.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

from opl import projectcheck

logger = logging.getLogger("opl.conductor.readiness")

CACHE_NAME = "readiness.json"
RECHECK_SECONDS = 24 * 3600
_EXPIRY_ITEM = "github: token expiry"


def summarize(results):
    """The cacheable essence of a check: the FAILs and the expiry WARN."""
    return {
        "fail": [[r.item, r.fix or r.detail] for r in results
                 if r.status == projectcheck.FAIL],
        "warn": [[r.item, r.fix or r.detail] for r in results
                 if r.status == projectcheck.WARN and r.item == _EXPIRY_ITEM],
    }


def describe(key, summary):
    """(level, text) for a cached summary, or None when the project is ready."""
    if summary.get("fail"):
        item, fix = summary["fail"][0]
        more = len(summary["fail"]) - 1
        text = "Project not ready: %s - %s" % (item, fix)
        if more:
            text += " (+%d more; run bin/opl-project-check %s)" % (more, key)
        return "fail", text
    if summary.get("warn"):
        item, fix = summary["warn"][0]
        return "warn", "Project warning: %s - %s" % (item, fix)
    return None


class ReadinessGate:
    def __init__(self, settings, check, background=True, clock=time.time):
        """`check(project)` returns a list of projectcheck.Result."""
        self.settings = settings
        self.check = check
        self.background = background
        self.clock = clock
        self.path = os.path.join(settings.conductor.state_dir, CACHE_NAME)
        self._lock = threading.Lock()
        self._running = set()
        self._started = set()       # projects re-checked since this start
        self._cache = self._load()
        self._reported = {}

    # -- cache --------------------------------------------------------------
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(self._cache, fh, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning("could not save %s: %s", self.path, exc)

    # -- checking -----------------------------------------------------------
    def _due(self, key):
        entry = self._cache.get(key)
        if key not in self._started or entry is None:
            return True
        return self.clock() - entry.get("at", 0) >= RECHECK_SECONDS

    def _run(self, project):
        try:
            summary = summarize(self.check(project))
        except Exception as exc:  # a broken check must not stop the conductor
            logger.warning("readiness check for %s crashed: %s",
                           project.key, exc)
            summary = {"fail": [["readiness check",
                                 "the check itself failed (%s); run "
                                 "bin/opl-project-check %s" % (exc, project.key)]],
                       "warn": []}
        with self._lock:
            self._cache[project.key] = dict(summary, at=self.clock())
            self._running.discard(project.key)
            self._save()

    def _start(self, project):
        with self._lock:
            if project.key in self._running:
                return
            self._running.add(project.key)
            self._started.add(project.key)
        if self.background:
            threading.Thread(target=self._run, args=(project,), daemon=True,
                             name="readiness-" + project.key).start()
        else:
            self._run(project)

    def issues(self, keys):
        """{key: (level, text)} for the projects that are not ready."""
        out = {}
        for project in self.settings.projects:
            if project.key not in keys:
                continue
            if self._due(project.key):
                self._start(project)
            with self._lock:
                entry = self._cache.get(project.key)
            if entry is None:
                out[project.key] = ("pending", "")
                continue
            found = describe(project.key, entry)
            if found:
                out[project.key] = found
            if found != self._reported.get(project.key):
                self._reported[project.key] = found
                if found:
                    logger.warning("project %s: %s", project.key, found[1])
                else:
                    logger.info("project %s is ready", project.key)
        return out
