"""Conductor loop heartbeat (issue #60). Standard library only.

The process staying alive says nothing about the loop: after a host sleep
the conductor keeps holding its instance lock while no cycle runs, and
every health check still reports healthy. So the loop itself writes a
timestamp file on every cycle (<state_dir>/conductor.heartbeat), and
`opl-conductor-start status` compares it with now instead of trusting the
lock alone. A separate watchdog command restarts the loop when the
heartbeat goes stale beyond a cycle or two.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from opl.conductor.lock import InstanceLock

HEARTBEAT_NAME = "conductor.heartbeat"

# A loop that stopped writing counts as stale beyond this many intervals
# ("a cycle or two" in the issue, with slack for one slow cycle).
STALE_AFTER_INTERVALS = 3

logger = logging.getLogger("opl.conductor")


def heartbeat_path(state_dir):
    """Path of the loop heartbeat file inside the state dir."""
    return os.path.join(state_dir, HEARTBEAT_NAME)


def write_heartbeat(state_dir, now=None):
    """Stamp the heartbeat file with now (UTC ISO-8601); never raises.

    The loop must not die because the heartbeat failed, so an OSError is
    logged and swallowed. Returns the stamp written (or that would have
    been written).
    """
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    try:
        os.makedirs(state_dir, exist_ok=True)
        tmp = os.path.join(state_dir, HEARTBEAT_NAME + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(stamp + "\n")
        os.replace(tmp, heartbeat_path(state_dir))
    except OSError as exc:
        logger.warning("heartbeat write failed: %s", exc)
    return stamp


def _parse(text):
    try:
        stamp = datetime.fromisoformat(text.strip())
    except (ValueError, AttributeError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def read_heartbeat(state_dir):
    """Aware datetime of the last finished loop cycle, or None.

    Falls back to the file mtime when the content is unreadable, so a
    half-written or foreign file still yields an age instead of silence.
    """
    try:
        with open(heartbeat_path(state_dir), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    parsed = _parse(text)
    if parsed is not None:
        return parsed
    try:
        return datetime.fromtimestamp(
            os.path.getmtime(heartbeat_path(state_dir)), timezone.utc)
    except OSError:
        return None


def heartbeat_age_seconds(state_dir, now=None):
    """Seconds since the last finished cycle, or None with no heartbeat.

    A backward clock jump clamps to 0 (fresh), never a huge age.
    """
    last = read_heartbeat(state_dir)
    if last is None:
        return None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max((now - last).total_seconds(), 0.0)


def stale_after_seconds(interval_seconds, factor=STALE_AFTER_INTERVALS):
    """How old a heartbeat may be before the loop counts as stalled."""
    return interval_seconds * factor


def is_stale(state_dir, interval_seconds, now=None):
    """True when the heartbeat is older than the stall threshold.

    None when there is no heartbeat yet (a fresh start that has not
    finished its first cycle): unknown, not stale.
    """
    age = heartbeat_age_seconds(state_dir, now)
    if age is None:
        return None
    return age > stale_after_seconds(interval_seconds)


def _ago(age):
    if age < 90:
        return "%ds" % int(age)
    minutes = int(age // 60)
    if minutes < 90:
        return "%dm" % minutes
    return "%dh%dm" % (minutes // 60, minutes % 60)


def heartbeat_line(state_dir, interval_seconds, now=None):
    """One human line for status: when the loop last ran, flagged if stale."""
    age = heartbeat_age_seconds(state_dir, now)
    if age is None:
        return ("loop heartbeat: none yet "
                "(no finished cycle in this state dir)")
    line = "loop last ran %s ago" % _ago(age)
    if age > stale_after_seconds(interval_seconds):
        line += " (STALE: no cycle for over %d intervals)" % STALE_AFTER_INTERVALS
    return line


def needs_restart(state_dir, interval_seconds, now=None):
    """Watchdog verdict for a conductor that holds its lock.

    Returns (restart, reason): True when the heartbeat is stale, or when
    no cycle ever finished within the stall threshold of the lock (a
    start whose first cycle never completes is wedged too). A missing
    heartbeat with a younger lock is a fresh start: not stale.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    stale = is_stale(state_dir, interval_seconds, now)
    line = heartbeat_line(state_dir, interval_seconds, now)
    if stale is True:
        return True, line
    if stale is False:
        return False, line
    try:
        lock_age = max((now - datetime.fromtimestamp(
            os.path.getmtime(InstanceLock(state_dir).path), timezone.utc)
        ).total_seconds(), 0.0)
    except OSError:
        return False, line
    if lock_age > stale_after_seconds(interval_seconds):
        return True, "%s (and the lock is %s old)" % (line, _ago(lock_age))
    return False, "%s (starting up: lock %s old)" % (line, _ago(lock_age))
