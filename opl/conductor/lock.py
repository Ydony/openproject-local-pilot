"""Single-instance lock for opl-conductor (TH.9). Standard library only.

An OS file lock on `<state_dir>/conductor.lock`, held for the process's
lifetime. The OS drops it when the process dies, so a crash never leaves a
stale lock, and no PID probing is needed (os.kill(pid, 0) would terminate
the process on Windows).
"""

from __future__ import annotations

import os

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class AlreadyRunning(Exception):
    """Another conductor holds the lock."""


class InstanceLock:
    def __init__(self, state_dir):
        self.path = os.path.join(state_dir, "conductor.lock")
        self._fh = None

    def acquire(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            fh.seek(0)
            if os.name == "nt":
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise AlreadyRunning(
                "another opl-conductor is running (lock %s)" % self.path)
        fh.truncate(0)
        fh.write("%d\n" % os.getpid())
        fh.flush()
        self._fh = fh
        return self

    def release(self):
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fh.seek(0)
            if os.name == "nt":
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False
