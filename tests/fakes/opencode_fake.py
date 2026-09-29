"""Fake OpenCode for synthetic tests: records its invocation, never calls a model.

Usage (tests only):
    FAKE_OPENCODE_LOG=<jsonl path> python tests/fakes/opencode_fake.py ...

Behaviour:
- appends one JSON line {"argv": [...], "cwd": ..., "env_names": [...]}
  to FAKE_OPENCODE_LOG (env *names* only for the allowlist check; the
  test asserts secret *values* are absent from argv/prompt, so values
  are never recorded).
- honours FAKE_OPENCODE_EXIT (default 0) and FAKE_OPENCODE_MODE:
    default      -> prints OPL-RESULT: DONE fake-ok
    missing-line -> prints chatter with no final line
    fail-line    -> prints OPL-RESULT: FAILED fake-boom
- when the prompt (last argv element) contains "WRITE_YES", writes
  "YES\\n" to <cwd>/yes.txt (the T5.1 tiny-task probe).
"""

import json
import os
import sys


def main():
    log = os.environ.get("FAKE_OPENCODE_LOG", "")
    argv = sys.argv[1:]
    cwd = os.getcwd()
    # Names only: values never leave the process in test evidence.
    names = sorted(n for n in os.environ if n.startswith("OPL_") or n.startswith("FAKE_"))
    if log:
        try:
            with open(log, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps({"argv": argv, "cwd": cwd, "env_names": names}) + "\n")
        except OSError:
            pass
    prompt = argv[-1] if argv else ""
    if "WRITE_YES" in prompt:
        try:
            with open(os.path.join(cwd, "yes.txt"), "w", encoding="utf-8", newline="\n") as fh:
                fh.write("YES\n")
        except OSError:
            pass
    mode = os.environ.get("FAKE_OPENCODE_MODE", "")
    if mode == "missing-line":
        print("fake opencode chatter, no final line")
    elif mode == "fail-line":
        print("OPL-RESULT: FAILED fake-boom")
    else:
        print("fake opencode did the task")
        print("OPL-RESULT: DONE fake-ok")
    return int(os.environ.get("FAKE_OPENCODE_EXIT", "0") or "0")


if __name__ == "__main__":
    raise SystemExit(main())
