"""Keep tests that run the whole conductor away from the real ccusage.

`opl-conductor` prices sessions with `npx ccusage` over the owner's real
Claude/Codex/OpenCode logs. Any test that calls the conductor's main()
must wrap it in no_real_ccusage(): ccusage is replaced by an empty
report, and the calls are recorded so the wiring can still be checked.
"""

import contextlib
from unittest import mock


@contextlib.contextmanager
def no_real_ccusage():
    calls = []

    def fake(command, tool, timeout=None, env=None):
        calls.append({"command": list(command), "tool": tool, "env": env})
        return {"sessions": []}

    with mock.patch("opl.usage.run_ccusage", fake):
        yield calls
