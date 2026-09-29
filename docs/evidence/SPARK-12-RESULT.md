# LIVE-PILOT-RESULT (T5.1 live-pilot blocker, issue #12)

Scope: unblock the real conductor -> LLM -> GitHub flow for the Spark
route only. Synthetic fixtures/fakes only; no Docker, network,
OpenProject, GitHub, tokens, pushes, PRs, or merges. Offline tests do
NOT prove the live pilot.

## 1. Executable-path trace (as found)

Conductor live tick (`opl/conductor/__main__.py:_cycle` + `_make_runner`):

1. `collect_openproject` + `collect_github` -> `World`.
2. `run_once` (enforce/stages/screens/merge/costs) -> watch/apply.
3. `SparkRunner.tick(world)` (only in live mode: config `live=true` AND
   `--live`; otherwise watch-only):
   - `_candidates` / `_review_candidates` / `_test_candidates` /
     `_fix_candidates`: Public only, Task Ready+assignee `spark`
     (build/fix), Task In review+reviewer `spark` (review), Feature In
     test (test). Violation items (`enforce.violations`) skipped.
   - `_dispatch_problem` per item: settings project + `local_repo`,
     origin is `github.com/<repo>`, repo actually public, `pr_base` plain
     branch, `base_ref` resolves locally, parent Feature Approved/Building,
     predecessors Merged.
   - `Slots.acquire` (max 2) -> thread `_run_bound` -> `_fresh_problem`
     (live re-read) -> `_attempt`: `create_worktree`, move In progress,
     `_guard_spawn` (auth unchanged), `build_packet` (scrubbed last),
     `run_worker(cmd, worktree, limit, stall, log, env=_worker_env)`.
   - Outcomes: `parse_final_line` (needs `OPL-RESULT: DONE`),
     `_delivered` (clean + ahead of base), push + PR + move to In review
     (or WIP + Blocked on timeout/stall/fail; one retry then Blocked).
   - Review/test/fix variants use `OPL-REVIEW: PASS|CHANGES` /
     `OPL-TEST: PASS|FAIL` with the same guards.

`[runner] command` was the placeholder
`["example-worker", "--prompt-file", "{packet}", "--cwd", "{workdir}"]`.

## 2. Missing links closed by this run (Spark route only)

- Generic OpenCode adapter: `opl/conductor/spark/opencode.py`
  (`build_command`, `read_packet`, `resolve_bin`, `validate_model`,
  `validate_variant`, `discover_shell`) + `opl/conductor/spark/opencode_cli.py`
  (`--packet/--workdir/--model/--variant/--opencode-bin` ->
  `opencode run --model M [--variant V] --dir <workdir> <prompt>`)
  launched via bash `bin/opl-opencode-run`
  (`python -m opl.conductor.spark.opencode_cli`, same 3.11+ selection
  as the other launchers).
  CLI shape verified against https://opencode.ai/docs/cli/ (`run`
  section) and https://opencode.ai/docs/windows-wsl; operator must
  re-check `opencode run --help` on the pilot host (third-party flags).
- Preserved: `supervisor.run_worker` owns timeout/stall/kill
  (`_LIVE` + stop gates), `_worker_env`/`worker_environment` allowlist
  (worker's own key only + fresh HOME/XDG/TMP), packet scrubbed last,
  logs chmod 600, no secret values in argv/packet (fail-closed
  `forbidden` check; wrapper reads no values, prints names only).
- Shell discovery distinguishes Git Bash from the WSL launcher:
  `OPENCODE_GIT_BASH_PATH` (bash.exe only) wins; PATH scan reports
  `bash.exe` as `git_bash` and `wsl.exe` as `wsl`, never cross-labelled;
  bogus override pointing at `wsl.exe` is ignored (tests prove).
- Synthetic pilot template: `config/opl.pilot.example.toml`
  (adapter command, explicit `example/spark-1` + `xhigh`, synthetic
  only) + operator checklist `docs/LIVE-PILOT-CHECKLIST.md` (setup,
  two tiny tasks, evidence, stop points, security-review list).

Two tiny tasks (synthetic proof with fake OpenCode):

- `YES in yes.txt`: fake writes `YES` when the prompt contains
  `WRITE_YES` (`test_tiny_task_writes_yes_txt` green).
- `observed task status in task-status.txt`: status flow is the existing
  runner outcome path (DONE -> PR -> In review; missing/FAILED ->
  failed; timeout/stall -> Blocked + WIP). Synthetic status observation
  is covered by `test_spark_runner`/`test_spark_reviews` (66 green);
  no live status was observed here (no live services).

## 3. Second-model route: verified absent (blocker, not implemented)

- `runner.py:_candidates` requires `assignee == "spark"`;
  `_review_candidates` requires `reviewer == "spark"`;
  `_fix_candidates` requires `assignee == "spark"`. No Claude/Codex
  automatic dispatch exists; DESIGN §5 says Claude/Codex are manual
  (owner opens a session, reads its queue). Only one `[runner] command`
  exists. There is nothing to "enable" without a new authorization
  architecture, which is explicitly out of scope: NOT implemented, NOT
  relabelled (Spark is never presented as another model; manual
  handoffs are not automatic-dispatch proof).
- Requires lead security review (not done here): any Claude/Codex auto
  routing, token scoping across roles, approval/privacy/reviewer/sandbox
  changes, launch helper + narrow sudoers, cross-UID shutdown, container
  boundary. See `docs/SANDBOX.md`, `bin/opl-sandbox-check`.

## 4. Defects fixed vs diagnosed (no broad refactoring)

- Fixed (reproducible, route-blocking): missing launchable OpenCode
  command (above); shell conflation risk (new `discover_shell` +
  5 tests); adapter secret discipline (fail-closed `forbidden` +
  wrapper tests).
- Diagnosed, not changed: "earlier focused suite review-outcome
  failures" do not reproduce here — `test_spark_reviews` +
  `test_spark_runner` + `test_spark_fix` + `test_outcomes` (66 tests)
  are green on this checkout (py -3.13, `OPL_TIME_SCALE=0.05`).
  Per instructions tests were not weakened.
- Untouched: full-suite backup failures below (unrelated to this route;
  no broad fix attempted).

## 5. Tests

- New: `tests/test_opencode_adapter.py` (21 tests) +
  `tests/fakes/opencode_fake.py`. Covers packet->prompt, workdir,
  explicit model/variant, shell Git-Bash-vs-WSL, allowlist (worker's own
  key only), secret-values-never-in-argv, YES probe, missing-line
  observability, wrapper argv/exit/redaction.
- Focused (existing + new, green before any commit):
  `test_opencode_adapter` (21) + `test_spark_reviews` +
  `test_spark_runner` + `test_spark_fix` + `test_outcomes` (66) +
  `test_spark_packet` + `test_supervisor` — all OK via
  `py -3.13 -m unittest`.
- Full suite baseline (pre-change, this checkout): 
  `py -3.13 -m unittest discover -s tests` -> 565 tests,
  556 pass, 9 fail, 2 skip. All 9 failures are pre-existing
  `tests/test_static.py::FakeDockerTests` backup/restore cases on
  Windows (`opl-backup` line 178 `assets.tgz: No such file or directory`;
  `tar: Cannot connect to C: resolve failed`) — unrelated to the Spark
  route, present before this run's changes, not fixed here per scope.
  A post-change full re-run (now 586 tests with the 21 new ones) was not
  completed inside the 30-minute timebox; `SyntaxTests` (bash -n +
  index modes incl. the new launcher) and the focused suites above are
  green post-change. Per assignment the work is therefore staged but
  uncommitted and reported, not claimed green.

## 6. Remaining lead steps + blockers (exact)

1. Lead: `opencode run --help` flag check; set `OPENCODE_GIT_BASH_PATH`;
   copy `config/opl.pilot.example.toml` -> `$OPL_CONFIG_DIR/opl.toml`
   with real repo/`local_repo`/tokens (owner creates tokens).
2. Lead: `bin/opl-conductor --once` (watch) clean, then
   `bin/opl-conductor --once --live` for Task A (YES) and Task B
   (In review + PR link). Collect `packets/`, `logs/` (600),
   `runs.jsonl`, worktree branch, `watch.log`.
3. Lead: real GitHub push/PR/merge, OpenProject status/result/link,
   usage (`bin/opl-conductor runs`, else explicit `unknown`).
4. Blockers: no second-model auto route (needs security review);
   full suite red on Windows backup cases (unrelated but blocks the
   commit gate); genuine API/model/GitHub flow entirely unproven
   offline — no claim is made here.

## 7. Evidence in this checkout (staged, uncommitted: full suite red)

- `opl/conductor/spark/opencode.py`,
  `opl/conductor/spark/opencode_cli.py`, `bin/opl-opencode-run` (bash
  launcher, `100755` in the index),
  `tests/fakes/opencode_fake.py`, `tests/test_opencode_adapter.py`,
  `config/opl.pilot.example.toml`, `docs/LIVE-PILOT-CHECKLIST.md`,
  this file. Staged with `git add --chmod=+x` (bin launcher executable);
  NOT committed because the full suite is red on unrelated Windows
  backup cases (commit gate requires green). `ASSIGNMENT.md` /
  `WORKER-RULES.md` untouched, unstaged, and never to be committed.
- No credentials, real APIs, Docker, pushes, or merges were used.
- Usage (this worker run): unknown (no model-usage telemetry available
  to the worker; no paid calls made from inside the task).
