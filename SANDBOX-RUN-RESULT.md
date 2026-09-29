# Sandbox adapter result (issue #24, T5.1 S5)

Owner-side adapter so the conductor runs Spark as the isolated worker:
`bin/opl-sandbox-run` + `opl/conductor/spark/sandbox_run.py`, driving the
fixed `opl-spark-launch` clone/run/export modes. The runner
(`opl/conductor/spark/runner.py`) is unchanged.

## What was built

- `opl/conductor/spark/sandbox_run.py` (stdlib only):
  - Workdir inspection: repo slug from the `origin` URL (github.com
    https/ssh/scp forms only, else fail), base = full `HEAD` SHA, branch
    = current branch (detached HEAD refused).
  - Run ID = sanitized workdir basename + UTC stamp + 4 random hex,
    within the launcher's `^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$`.
  - `clone` with no env preserved; `run` with
    `--preserve-env=OPL_WORKER_API_KEY` (fail closed when the variable is
    missing; the value never appears in argv, logs, or errors; a packet
    containing the value is refused).
  - Model/variant validation and packet limits reused from
    `opl/conductor/spark/opencode.py` (#12), not duplicated.
  - JSON-lines → text on stdout, flushed line by line: assistant text
    verbatim (the worker's `OPL-RESULT:` line survives intact), tool
    calls as `[tool] <title>`, unknown events silent, non-JSON lines
    passed through. One trailing `usage: input=N output=N cost=F` line
    (or `usage unknown`), so `OPL-RESULT` stays in the runner's
    last-20-lines window.
  - Heartbeat: a daemon thread polls the newest mtime under
    `<root>/<ID>/repo` (excluding `.git`) every 5 s and touches
    `{workdir}/.opl-sandbox-heartbeat` only when it advanced past the
    post-clone baseline — an idle worker still stalls. The name is added
    once to the worktree's `info/exclude`; the file is removed at the end.
  - Export: bundle streams into a temp file under the owner's private
    runner state (`$OPL_SANDBOX_STATE_DIR`, default
    `~/.local/state/opl/sandbox-bundles`, 0700; never shared /tmp).
    Exit 3 imports nothing; exit 0 runs `git -C {workdir} fetch --no-tags
    <bundle> refs/heads/<branch>` then `merge --ff-only FETCH_HEAD`.
    No git command ever runs inside the worker's repo from the owner
    side. The bundle is always deleted.
  - Exit code = the run's exit code. SIGTERM/SIGINT is forwarded to the
    sudo child (SIGINT as SIGINT, else terminate), the import is skipped,
    cleanup runs, exit 143.
- `bin/opl-sandbox-run`: bash wrapper (same shape as `opl-opencode-run`),
  100755 in the git index.
- `config/opl.pilot.example.toml`: `[runner] command` now uses
  `opl-sandbox-run --packet {packet} --workdir {workdir} --model
  example/spark-1 --variant xhigh`; `worker_env` stays
  `["OPL_WORKER_API_KEY"]`.
- Tests (fakes only, stdlib only): `tests/fakes/sandbox_launcher_fake.py`
  (clone/run/export over a local bare repo; modes default, no-usage,
  progress, commit, idle, hang; `FAKE_LAUNCHER_EXIT` override),
  sandbox pass-through branch in `tests/fakebin/sudo` (drops `-n -u`
  worker / `--preserve-env=*` / `--`, runs the command; the canary
  `-n -H -u` shape untouched), 26 tests in `tests/test_sandbox_run.py`
  covering JSON→text with the final line intact, usage/unknown lines,
  heartbeat-only-on-progress (+ `.git` exclusion), bundle import
  fast-forward, exit-3 no-op, missing-key and packet-key fail-closed
  without logging the value, SIGTERM/SIGINT forwarding with import
  skipped (unit-level), detached HEAD and non-GitHub origin refused,
  and the fake-sudo pass-through itself.

## Verification

- `py -3.13 -m unittest tests.test_sandbox_run`: 26/26 OK.
- Full suite `py -3.13 -m unittest discover -s tests`: 616 tests —
  green except one pre-existing, unrelated failure:
  `test_publication.test_no_tracked_file_has_a_machine_path` trips on
  Windows drive-path literals in the committed (untouched) `tests/test_opencode_adapter.py`
  on this Windows checkout. The two `FakeOpenCodeFlowTests` failures from
  dispatch are gone (lead's commit cacc2e4); that area was not touched.

## Open questions / blockers

- Live verification is the lead's: real `sudo -n -u opl-worker`, the real
  launcher paths (`/usr/local/libexec/opl-spark-launch`,
  `/srv/opl-worktrees`), and the exact OpenCode JSON-lines event shapes
  (the adapter passes unknown shapes silently and raw lines verbatim, so
  extra event types degrade to ignored rather than lost output).
- The SIGTERM/SIGINT path is covered by unit tests (forward + skip-import
  + exit 143) but not by a live-signal integration test — Windows cannot
  deliver SIGTERM to a handler; needs a POSIX run.
- Timing/usage: full suite ~7 min on this box; usage metering
  unavailable in this environment (unknown).
