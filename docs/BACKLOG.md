# Build backlog: OpenProject multi-model delivery system

> **For agentic workers:** this file is both the implementation plan and the
> live task list. Work it using the protocol below. Each task's state lives on
> its own `Status:` line. Read [DESIGN.md](DESIGN.md) before your first task.
>
> **OPEN REVIEW FIXES: do these BEFORE any Todo task (Protocol step 1).**
> **Phase 8 (pre-merge hardening), split by owner:** **Spark:** TH.11, TH.13, TH.8, TH.12, TH.10, in that order, in THIS worktree. **Claude:** TH.7, TH.5, TH.9 on a separate branch (`ai/hardening-claude`), merged in at the end. **Codex:** TH.RE (early review), TH.15, TH.14 preparation on `ai/hardening-codex` (branched from Claude's), merged in at the end. **Overnight round (all branches start at 785b1bd):** Spark TH.16 → TH.17 → TH.18; Codex TH.14 prep → TH.19 → reviews of new commits; Claude TH.20, then merges, reviews and the full suite. Spark: don't change `opl/conductor/collect.py`, `opl/conductor/rules/merge.py`, `opl/conductor/rules/enforce.py`, `opl/conductor/engine.py`, `opl/conductor/__main__.py`, `opl/configure/*.py` or `opl/openproject.py` (Claude's area) unless your task text says so. Nothing is pushed or run live until TH.R passes.
> Each one's `Review:` line says exactly what to change. Earlier runs skipped them; don't.
>
> **Spark: finishing one task is never the end of your run.** After every
> commit, come back here, pick the next eligible Spark task, and start it
> straight away. Don't summarise, don't ask, don't wait. Your run ends only
> when Protocol step 10 says so.

**Goal:** a local OpenProject where the owner sees every project's features
from Proposed to Done, while Claude, Codex and Spark plan, build, review and
test under rules that are enforced.

**Architecture:**
- **Phase 0:** a Bash operator toolkit runs OpenProject 17 on loopback.
- **Phase 1:** a Python `opl-configure` command applies the tracker model
  (stages, types, workflows, roles, fields, users, views) from committed TOML.
- **Phases 3–4:** a Python `opl-conductor` runs a once-a-minute check-and-fix
  loop over OpenProject and GitHub. It moves stages, maintains the screens,
  merges when allowed, and runs Spark.

**Tech stack:**
- Bash 5 for the toolkit.
- Python 3.11+ standard library only: `tomllib`, `urllib`, `http.server`,
  `unittest`, `subprocess`, `dataclasses`.
- OpenProject 17: API v3 and Rails runner.
- GitHub REST API.

**Spec:** [DESIGN.md](DESIGN.md) for the whole system; [SPEC.md](../SPEC.md)
for the toolkit.

## Global constraints

Every task implicitly includes these.

- The repository is **public and synthetic-only**. No real tokens, personal
  domains, private project or repo names, or machine-specific paths in
  committed files.
- **Secrets:** code reads them only from environment variables whose *names*
  come from the external config. Code never writes, logs or prints a secret
  value.
- **Real configuration** (project list, repos, deploy signals, token variable
  names) lives **outside the repo** in `$OPL_CONFIG_DIR/opl.toml` (default
  `~/.config/opl/opl.toml`). The repo ships only `config/opl.example.toml`,
  with fake values.
- **Python:** 3.11 or newer, standard library only.
- **Test command:** `python -m unittest discover -s tests -v`, run from the
  repo root.
- **No test may need** Docker, network access, OpenProject or GitHub. Use the
  fakes in `tests/fakes/`.
- **Never start, stop or remove** Docker containers, volumes or images on the
  development machine. The owner may be running a real OpenProject there.
- **Networking:** loopback (`127.0.0.1`) only. Every wait has a time limit.
- **Names:** stage names, field names, and their values must be exactly as in
  DESIGN.md §1–§2. The tracker model file (T1.2) is the single code copy of
  those names.
- **Git:** never push, open PRs, or merge. The owner and reviewers do that.

## Protocol

### Status values and owners

- **Status:** `Todo`, `In progress`, `Awaiting review`, `Changes requested`,
  `Done`, `Blocked`
- **Owner:** `Spark`, `Claude/Codex`, `Owner`

### For Spark

0. **Reconcile once at the start.** For each git commit whose subject ends in
   `(item N)`, task **T0.N** was done by an earlier run. If its Status is still
   `Todo`, set it to `Awaiting review` (Low risk: `Done`) and put the commit
   hash in `Result:`.
1. **Pick the next task.**
   - First choice: the first Spark task with `Changes requested`.
   - Otherwise: the first Spark task in file order with `Status: Todo` whose
     every `Depends` task is `Done` or `Awaiting review`.
2. **Start it.** Set `Status: In progress`.
3. **Build it test-first.** Write the failing test, see it fail, implement, see
   it pass. Before finishing, run the whole suite; it must pass.
4. **Finish it.**
   - Risk Low: set `Done`.
   - Risk Medium or High: set `Awaiting review`.
   - Fill `Result:` with commit hash(es), the test result, and anything you
     were unsure about or changed from the task text.
5. **Commit** code, tests and this file together:
   `git commit -m "<ID>: <what and why>"`. Then go straight back to step 1.
   A finished task is not a stopping point.
6. **Timebox** by size: S = 20 min, M = 45 min, L = 90 min. When the time is
   up:
   - commit the partial work;
   - set `Blocked` with `Result: timebox — <what is left>`;
   - go to the next task.
7. **When you can't proceed** (you need the owner, credentials, live Docker,
   or a decision DESIGN.md doesn't answer): set `Blocked` with the reason and
   go to the next task.
8. **Stay in your lane.** Never change tasks owned by someone else, except
   where a task explicitly says so. Never mark your own Medium or High task
   `Done`.
9. **Log each task.** Append one line to the **Run log** per finished,
   blocked or timeboxed task.
10. **Stop only when no Spark task is eligible.** Before stopping, re-read
    this file once more and confirm that no Spark task is `Todo` or
    `Changes requested` with its dependencies met. Then append the line:
    `Stopped: no eligible Spark tasks. Waiting on: <task IDs>`.
    Blocked, timeboxed or finished tasks never end the run on their own.

If a task's text is wrong when checked against the code, upstream, or
DESIGN.md, do the right thing and record why in `Result:`.

### For reviewers (Claude/Codex)

1. Take tasks with status `Awaiting review`.
2. Set `Done`, or `Changes requested` with notes in `Review:`.
3. Review the High-risk tasks first.

## Run log

Format: `YYYY-MM-DD HH:MM · <ID> · <status> · <one line>`

- 2026-09-24 · T0.1–T0.14 · done by an earlier Spark run (commits ending "(item N)", plus follow-up 6021df0) · reconcile per protocol step 0
- 2026-09-24 · step 0 · reconciled: T0.1–T0.5, T0.7, T0.12 Awaiting review; T0.6, T0.8–T0.11, T0.13, T0.14 Done; hashes in Result
- 2026-09-24 · T0.15 · Awaiting review · 37/37 green; CRLF fixtures + mingw curl shadowing fixed test-side
- 2026-09-24 · T1.1 · Done · mapping doc created, 2 downstream edits, suite green
- 2026-09-24 · T1.2 · Done · model/settings loaders + TOML + discovery, 60/60 green
- 2026-09-24 · T1.3 · Awaiting review · admin-ruby renderer + reviewed golden, 68/68 green
- 2026-09-24 · T1.4 · Awaiting review · API client + apply_api + stateful fake, 75/75 green
- 2026-09-24 · T1.5 · Done · saved views + My-page pin + shared lookups, 80/80 green
- 2026-09-24 · T1.6 · Awaiting review · configure CLI + docs, 83/83 green
- 2026-09-24 · T2.3 · Awaiting review · permcheck command, 89/89 green
- 2026-09-24 · T3.1 · Awaiting review · conductor state model, 95/95 green
- 2026-09-24 · T3.2 · Awaiting review · collect OpenProject into World, 97/97 green
- 2026-09-24 · T3.3 · Awaiting review · GitHub client + collect, 104/104 green
- 2026-09-24 · T3.4 · Awaiting review · enforce rule, 112/112 green
- 2026-09-24 · T3.5 · Awaiting review · stages rule, 125/125 green
- 2026-09-24 · T3.6 · Awaiting review · screens rule, 137/137 green
- 2026-09-24 · T3.7 · Awaiting review · merge rule, 144/144 green
- 2026-09-24 · T3.8 · Awaiting review · engine + loop + watch, 155/155 green
- 2026-09-24 · T3.9 · Awaiting review · end-to-end scenarios, 158/158 green
- 2026-09-24 · T0.1 review fixes · Awaiting review · per-entry ports + PORT env guard, suite green
- 2026-09-24 · T0.2 review fixes · Awaiting review · newline-before-append + umask 077, suite green
- 2026-09-24 · T0.4 review fixes · Awaiting review · disposable naming + label refusal, suite green
- 2026-09-24 · T0.12 review fix · Awaiting review · start call-order test, suite green
- 2026-09-24 · T0.14 review fix · Done · prerequisites line, one page kept
- 2026-09-24 · T1.3 review fixes · Awaiting review · registry perms, global default, strict versions, suite green
- 2026-09-24 · T3.4 review fix · Awaiting review · E4/E5 need a set reviewer, suite green
- 2026-09-24 · T3.5 review fixes · Awaiting review · S0 + strict merge times, suite green
- 2026-09-24 · T3.8 review fixes · Awaiting review · option links, grouped PATCH, comment gating, suite green
- 2026-09-24 · T0.16 · Awaiting review · local tuning override, suite green
- 2026-09-24 · T2.2 · Done · MCP client snippets, suite green
- 2026-09-24 · T4.1 · Awaiting review · packet + worktree, suite green
- 2026-09-24 · T4.2 · Awaiting review · supervisor + slots, suite green
- 2026-09-24 · T4.3 · Awaiting review · build-run outcomes, suite green
- 2026-09-24 · T0.R (Claude) · first review pass · approved T0.3 T0.5 T0.7 T0.15; changes requested T0.1 T0.2 T0.4 T0.12 T0.14 T1.3 (see each Review:)
- 2026-09-24 · T4.3 review fixes · Awaiting review · 7 review items fixed, suite 222 green
- 2026-09-24 · T4.4 · Awaiting review · review + test runs, suite 226 green
- 2026-09-24 · T4.5 · Done · run records + tuning report, suite 236 green
- 2026-09-24 · T4.6 · Done · conductor docs + runbook link, suite 236 green
- 2026-09-24 · T4.7 · Awaiting review · fix-after-review loop, suite 241 green
- 2026-09-24 · T4.4 review fixes · Awaiting review · 5 review items fixed, suite 252 green
- 2026-09-25 · TC.1 · Done · usage-mapping spike, suite 257 green
- 2026-09-25 · TC.2 · Done · prices, estimates, cost fields, suite 269 green
- 2026-09-25 · TC.3 · Awaiting review · conductor costs job, suite 296 green
- 2026-09-25 · TC.4 · Done · estimate calibration report, suite 306 green
- 2026-09-25 · TC.2 review fix · Done · price by model, suite 317 green
- 2026-09-25 · TC.3 review fixes · Awaiting review · folder match, model pricing, feature cost, suite 317 green
- Stopped: no eligible Spark tasks. Waiting on: TC.R (Claude/Codex), T5.1/T4.O/Owner live proofs, T6.x

- 2026-09-25 · Claude · all Spark tasks reviewed and approved; full suite 317 OK; remaining work is Codex (TM.1, T1.P, T6.0) and the owner's live steps

- 2026-09-25 · Codex · TM.1 (blockers → Phase 8), T1.P, T6.0 done; private TC.1 live check → TH.12
- 2026-09-25 · TH.11 · Awaiting review · start ref vs PR base + idempotent PRs, suite 357 green
- 2026-09-25 · TH.13 · Awaiting review · publication hygiene, suite 361 green
- 2026-09-25 · TH.8 · Awaiting review · checks, merge SHA, exact review checkout, suite 376 green
- 2026-09-25 · TH.12 · Awaiting review · real log shapes for cost tracking, suite 386 green
- 2026-09-25 · TH.10 · Awaiting review · attempt ceilings + real progress, suite 390 green
- 2026-09-25 · TH.16 · Awaiting review · attempts reset, refs, backup lock, suite 467 green
- 2026-09-25 · TH.17 · Awaiting review · shutdown stop gate, suite 472 green
- 2026-09-25 · TH.18 · Awaiting review · dispatch boundary, suite 480 green
- 2026-09-25 · TH.21 · Awaiting review · ceiling re-arm on human move, suite 501 green

## File map

| Path | Responsibility | Task |
|---|---|---|
| `bin/opl-setup`, `opl-start`, `opl-stop`, `opl-status`, `opl-logs`, `opl-update`, `opl-backup`, `opl-restore-test` | Operator toolkit | T0.x |
| `lib/common.sh` | Shared Bash helpers | T0.x |
| `compose/docker-compose.override.yml` | Loopback-only port template | T0.1 |
| `.opl-runtime/docker-compose.local.yml` (optional, ignored) | Owner laptop tuning (lead-managed) | T0.16 |
| `tests/fakes/docker`, `tests/fakes/curl` | Fake commands for toolkit tests | T0.12 |
| `bin/opl-compose` | Exposes the pinned `compose` wrapper to other programs | T1.6 |
| `config/pm-model.toml` | Tracker model: statuses, types, workflows, roles, fields, users, versions, views | T1.2 |
| `config/opl.example.toml` | Example external config (fake values) | T1.2 |
| `opl/model.py` | Load and validate `pm-model.toml` | T1.2 |
| `opl/settings.py` | Load the external config; look up secrets by env var name; redact | T1.2 |
| `opl/openproject.py` | OpenProject API v3 client | T1.4 |
| `opl/configure/admin_ruby.py` | Render the Rails-runner admin script | T1.3 |
| `opl/configure/apply_api.py` | Projects, users, memberships, versions via API | T1.4 |
| `opl/configure/views.py` | Saved views via API | T1.5 |
| `opl/configure/__main__.py`, `bin/opl-configure` | Configure command | T1.6 |
| `opl/permcheck.py`, `bin/opl-permcheck` | Permission check per model user | T2.3 |
| `opl/github.py` | GitHub REST client | T3.3 |
| `opl/conductor/state.py` | World, Project, Item, Change, and friends | T3.1 |
| `opl/conductor/collect.py` | Build a World from OpenProject and GitHub | T3.2, T3.3 |
| `opl/conductor/rules/enforce.py` | Enforce rule | T3.4 |
| `opl/conductor/rules/stages.py` | Stages rule | T3.5 |
| `opl/conductor/rules/screens.py` | Screens rule | T3.6 |
| `opl/conductor/rules/merge.py` | Merge rule | T3.7 |
| `opl/conductor/engine.py` | run_once, conflicts, ownership guard, apply or log | T3.8 |
| `opl/conductor/__main__.py`, `bin/opl-conductor` | CLI and loop | T3.8 |
| `opl/conductor/spark/*.py` | Spark runner | T4.x |
| `tests/fakes/http_fake.py` | Fake HTTP server for OpenProject and GitHub | T1.4 |
| `docs/SETUP-MAPPING.md` | Which mechanism applies each setup item | T1.1 |
| `docs/MCP.md` | MCP server review and client setup | T2.1, T2.2 |

---

## Phase 0: Hosting toolkit

**Spec:** SPEC.md; DESIGN.md §6 step 0. Items 1–14 come from the fix prompt
that an earlier Spark run started. T0.N corresponds to item N.

### T0.1 · Setup must not abort on the override comment
- **Owner:** Spark · **Size:** S · **Risk:** Medium · **Depends:** —
- **Status:** Done
- **Result:** f3b129d (item 1). Review fixes (this commit): per-entry `127.0.0.1:` prefix check (bare ports and wildcards both refused, fail-closed on zero entries; the wildcard-specific branch was subsumed so no `0.0.0.0` literal lives in code) + `assert_loopback_env` (PORT must be `127.0.0.1:<port>`) wired into setup (after env) and start (before any docker call). Tests: bare-alongside-loopback fails, bad PORT refuses with zero docker calls. Suite green.
- **Review:** (Claude, 2026-09-24, re-review) Approved. Every port entry must start with `127.0.0.1:` (fail-closed on no entries), and `assert_loopback_env` runs in setup and in start before any docker call.

**Problem:** the template comment contains `0.0.0.0`, and setup/start grep
the whole rendered file, so setup always aborted.

**Done when:** only non-comment port lines are checked; a line with no host
IP (e.g. `8080:80`) counts as public; the synthetic setup test passes.

### T0.2 · Consistent runtime .env
- **Owner:** Spark · **Size:** M · **Risk:** High · **Depends:** T0.1
- **Status:** Done
- **Result:** 4faa78d (item 2) + follow-up 6021df0. Review fixes (this commit): `env_ensure` prepends a newline when the file lacks a trailing one (tested with a newline-less fixture, incl. the no-guess DATABASE_URL rule); new `.env` created under `umask 077` in a subshell with `chmod 600` kept as backstop (proven by a POSIX-only test with chmod stubbed to no-op). Suite green.
- **Review:** (Claude, 2026-09-24, re-review) Approved. The file is created under `umask 077` (chmod kept as a backstop), and the newline is added before appending.

**Done when:**
- A new install writes `PORT`, `OPENPROJECT_HTTPS=false`, `OPENPROJECT_HSTS=false`,
  `OPENPROJECT_HOST__NAME`, `TAG`, `POSTGRES_VERSION=17`, `SECRET_KEY_BASE`,
  `POSTGRES_PASSWORD`, `DATABASE_URL` (same password),
  `COLLABORATIVE_SERVER_SECRET` and `COLLABORATIVE_SERVER_URL`.
- An existing .env gains only missing keys. Existing values never change.
  `POSTGRES_VERSION` is never added to an existing install.
- Generated values are never printed. Mode is 600. A second run leaves the
  file byte-identical.

### T0.3 · Status reports four states
- **Owner:** Spark · **Size:** M · **Risk:** Medium · **Depends:** —
- **Status:** Done
- **Result:** b9b8ac3 (item 3) + follow-up 6021df0 (label lookup, head/SIGPIPE guard; see T0.15).
- **Review:** (Claude, 2026-09-24) Approved. The state rules match the spec:
  nothing running → stopped; restarting/dead/OOM/failed seeder → unhealthy;
  a service down while others run → unhealthy; web health → ready.

**Done when:**
- The states are: `stopped` (nothing running), `starting`, `ready`, and
  `unhealthy` (crashed, restarting, OOM-killed, or web unhealthy while others
  run), plus `unknown` when Docker is unreachable.
- Containers are found by the `com.docker.compose.project` label.
- After `opl-stop`, status reports `stopped`.

### T0.4 · Restore test cannot touch live data
- **Owner:** Spark · **Size:** M · **Risk:** High · **Depends:** —
- **Status:** Done
- **Result:** 7881e63 (item 4). Review fixes (this commit): disposable names must match `^opl-restore-[a-z0-9][a-z0-9_-]*$` (checked before any docker call) + refusal when labelled containers already exist for the name. Tests: prefix refusal with zero docker calls, label refusal before any `up`, active-project guard reached via a prefixed active name. Suite green.
- **Review:** (Claude, 2026-09-24, re-review) Approved. The `opl-restore-` prefix is checked before any docker call, labelled-container refusal happens before `up`, and the guards run before the cleanup trap is armed.

**Done when:** the throwaway project uses only its own named volumes. The
restore test refuses to run when the throwaway project's rendered config
mounts a host path or a volume not prefixed with the throwaway name.
`PGDATA`/`OPDATA` from the runtime .env **and from the calling shell** cannot
leak into it.

### T0.5 · Backup failure cleanup and retention
- **Owner:** Spark · **Size:** S · **Risk:** Medium · **Depends:** —
- **Status:** Done
- **Result:** 4225d16 (item 5) + follow-up 6021df0 (trap exit-status passthrough).
- **Review:** (Claude, 2026-09-24) Approved. Only this run's folder is
  removed, retention counts manifests only, and the exit status passes
  through.

**Done when:** a failed run deletes only the folder it created; retention
counts and prunes only folders that contain a manifest.

### T0.6 · Empty assets are a valid restore proof
- **Owner:** Spark · **Size:** S · **Risk:** Low · **Depends:** —
- **Status:** Done
- **Result:** d17a11b (item 6). Low risk.
- **Review:** —

**Done when:** `assets=0` passes; only an archive that fails to extract fails.

### T0.7 · Update pulls images
- **Owner:** Spark · **Size:** S · **Risk:** Medium · **Depends:** —
- **Status:** Done
- **Result:** 0458823 (item 7).
- **Review:** (Claude, 2026-09-24) Approved. It pulls after the bundle
  update, skips cleanly without a daemon, and the comment is fixed.

**Done when:** `opl-update` runs `compose pull` after updating the bundle,
and the wrong comment claiming opl-start pulls images is fixed.

### T0.8 · Start stages never restart the seeder
- **Owner:** Spark · **Size:** S · **Risk:** Low · **Depends:** —
- **Status:** Done
- **Result:** 09bcdf3 (item 8). Low risk.
- **Review:** —

**Done when:** phases 2 and 3 use `up -d --no-deps`.

### T0.9 · Backup reads OPDATA from the runtime .env
- **Owner:** Spark · **Size:** S · **Risk:** Low · **Depends:** —
- **Status:** Done
- **Result:** 06e32e6 (item 9). Low risk.
- **Review:** —

**Done when:** backup reads `OPDATA` from the runtime .env, not the caller's
shell. An absolute or relative path is archived as a host path; anything else
is treated as the named volume `<project>_<value>`.

### T0.10 · Fix three wrong tests
- **Owner:** Spark · **Size:** S · **Risk:** Low · **Depends:** —
- **Status:** Done
- **Result:** a20974c (item 10). Low risk.
- **Review:** —

**Done when:**
- Service names in scripts are not flagged as vendored upstream content.
- The runbook word check is case-insensitive.
- The cleanup-scope check accepts the scoped `dcompose` line.

### T0.11 · Loopback test checks the effective result
- **Owner:** Spark · **Size:** S · **Risk:** Low · **Depends:** T0.1
- **Status:** Done
- **Result:** b620b78 (item 11). Low risk. Rendered-override + PORT checks in SyntheticSetupTests.
- **Review:** —

**Done when:** a synthetic setup run produces an override whose non-comment
port lines, **and** a .env whose `PORT`, all bind 127.0.0.1. The test fails if
either is public.

### T0.12 · Behaviour tests with fake docker and curl
- **Owner:** Spark · **Size:** L · **Risk:** Medium · **Depends:** T0.3, T0.4, T0.5
- **Status:** Done
- **Result:** 1990071 (item 12). Review fix (this commit): start-order test asserting the exact `up/exec` phase sequence with `--no-deps` throughout plus the `--no-background` omission (Bash fakes kept, no Python rework per review). Suite green.
- **Review:** (Claude, 2026-09-24, re-review) Approved. The start call-order test covers `--no-deps` and `--no-background`.

**Fake contract:**
- `tests/fakes/docker` and `tests/fakes/curl` are executable Python scripts.
  Tests put `tests/fakes` first on `PATH`.
- `FAKE_DOCKER_SCENARIO` points to a JSON file of scripted replies keyed by
  subcommand, e.g. `"info"`, `"ps"`, `"inspect"`, `"compose config"`,
  `"compose exec db pg_dump"`, `"volume ls"`, `"run"`.
- Every call is appended as a JSON line to `FAKE_DOCKER_LOG`, including argv
  and whether `PGDATA`/`OPDATA` were set in its environment.
- `FAKE_CURL_EXIT` sets curl's exit code.

**Done when:** these behaviours are covered:
- all four status states;
- backup failure cleanup and retention;
- restore refusal for the active project, a name without the
  `opl-restore-` prefix, and host-path mounts;
- restore cleanup touching only throwaway resources;
- `opl-start`'s exact call order, including `--no-deps`.

### T0.13 · Executable bit survives Windows
- **Owner:** Spark · **Size:** S · **Risk:** Low · **Depends:** —
- **Status:** Done
- **Result:** 807cda4 (item 13). Low risk.
- **Review:** —

**Done when:** `bin/*`, `tests/*.sh` and `tests/fakes/*` are staged with
`git add --chmod=+x`, and the executable test checks git's index mode
(`git ls-files -s` shows `100755`) instead of `os.access`.

### T0.14 · Runbook: first login, update, status
- **Owner:** Spark · **Size:** S · **Risk:** Low · **Depends:** T0.3, T0.7
- **Status:** Done
- **Result:** b325590 (item 14). Review fix (this commit): prerequisites line added near the top as specified; runbook at 56 lines, one-page test green.
- **Review:** (Claude, 2026-09-24) Clear and one page, but the
  prerequisites line is **not** in `docs/RUNBOOK.md` despite the Result
  note. Add near the top: "Needs (installed by the lead): Docker Engine
  inside WSL 2, git, curl, python3 (3.11+)". Keep the ≤ 130-line test green.

**Done when:**
- First login is covered: user `admin`, password `admin`, with a forced change
  at first sign-in.
- The update steps match T0.7, and the status meanings match T0.3.
- Prerequisites are listed: Docker Engine in WSL 2, git, curl, python3.
- It stays one page (≤ 130 lines) and plain language.

### T0.15 · Fix the three failing status tests
- **Owner:** Spark · **Size:** S · **Risk:** Medium · **Depends:** —
- **Status:** Done
- **Review:** (Claude, 2026-09-24) Approved. The root cause was test-side
  (CRLF fixtures, MSYS curl shadowing), and the scripts are unchanged. Good
  call to clear the suspect commit with evidence rather than "fixing" working
  code.
- **Result:** Done in commit (this commit): full suite 37/37 OK via `python tests/test_static.py` and `bash tests/run.sh` (exit 0). Root causes, both test-side: (1) fixture files written by Python in text mode got Windows CRLF, so container ids arrived as `db1\r` and matched no fixture — all test-written bash fixtures now use `newline="\n"`; (2) on Git-Bash dev machines the MSYS2 runtime prepends `/mingw64/bin` to every process PATH, so its real `curl` shadowed the fake (MSYSTEM does not disable this; a command hash does not survive `exec`) — `run_tool` now pins both fakes via exported shell functions, which child bash processes inherit. The task text's suspect (6021df0's head/SIGPIPE guards) is innocent: the same 3 tests failed identically before that commit, and traces show the guards behave. No toolkit script logic changed except none — scripts were correct. Small extra: `tests/run.sh` falls back to `python` when `python3` is absent (Windows Git-Bash). Timebox S overrun (deep platform debugging); unsure about one thing: why the old CRLF backfill fixture still passed — left as-is since LF fixtures now pass deterministically.

**Problem:** `python tests/test_static.py` fails 3 of 37 tests. All three are
in `FakeDockerTests`:

| Test | Expected | Actual |
|---|---|---|
| `test_status_stopped_after_compose_stop` | exit 2 | exit 1 |
| `test_status_restarting_is_unhealthy` | exit 3 | exit 1 |
| `test_status_starting_ready_unhealthy` | `STATUS=starting` in stdout | empty stdout |

They most likely broke in follow-up commit 6021df0, which changed container
lookup in `lib/common.sh` (the head/SIGPIPE guard).

**Steps:**
- [ ] Run the three tests and read `bin/opl-status`'s stderr in each case.
- [ ] Find the root cause (`git diff b325590 6021df0 -- lib/common.sh bin/opl-status`).
- [ ] Fix the script, not the tests, unless a test is wrong. If a test is
  wrong, say why in `Result:`.
- [ ] Run the full suite; everything must pass. Commit.

**Done when:** the whole suite passes.

### T0.16 · Optional owner tuning override
- **Owner:** Spark · **Size:** S · **Risk:** Medium · **Depends:** T0.1
- **Status:** Done
- **Review:** (Claude, 2026-09-24) Approved (d018b5b). `compose()` adds `$OPL_RUNTIME_DIR/docker-compose.local.yml` only when it exists; `assert_tuning_only` refuses `ports:`/`network_mode:` in setup, start and restore-test (the throwaway db doesn't load the file, which is fine because tuning targets web/worker). The owner's existing tuning file (environment-only) is compatible. 51/51 toolkit tests pass.
- **Result:** This commit: `compose()` appends the local file when present; `assert_tuning_only` refuses publish/network lines, wired into setup/start/dcompose; runbook boundary line; file-map row. Tests: per-entry file counts via direct compose call, helper pass/fail incl. comment-only, start/restore refusal before any `up`, setup acceptance. Suite 190/190 green.

**Problem:** the owner's existing instance runs with laptop memory tuning
(fewer web workers and job threads, set as service `environment:` entries
in a separate compose file). The toolkit only knows its loopback override,
so adopting that instance (T0.L) would silently drop the tuning.

**Behaviour:**
- If `$OPL_RUNTIME_DIR/docker-compose.local.yml` exists, `compose()` adds it
  as a third `-f` file, after the loopback override. When it doesn't exist,
  nothing changes.
- The file may only tune; it must not publish anything. Refuse to run
  (setup, start, and the restore test's `dcompose`) if it contains `ports:`
  or `network_mode:` on a non-comment line.
- Document it in the runbook's "Who does what": the lead edits it; the
  owner never needs to.

**Tests:**
- Without the file, every compose call gets 2 files; with it, 3.
- A local file containing `ports:` makes start refuse before any `up`.

### T0.R · Review phase 0
- **Owner:** Claude/Codex · **Size:** M · **Risk:** High · **Depends:** T0.1–T0.16
- **Status:** Done
- **Result:** (Claude, 2026-09-24) First pass done against HEAD 56de409.
  All 68 committed tests pass.
  - **Approved:** T0.3, T0.5, T0.7, T0.15. Spot-checked the Low-risk Done
    tasks T0.6, T0.8, T0.9, T0.13 (index modes are 100755); they are fine.
  - **Changes requested:** T0.1 (per-entry port check plus an env `PORT`
    guard), T0.2 (newline before append; umask 077), T0.4 (`opl-restore-`
    prefix plus a label refusal), T0.12 (start call-order test; Bash fakes
    accepted), T0.14 (missing prerequisites line).
  - I re-review once those land, then this task is Done.
- **Review:** (Claude, 2026-09-24) Phase 0 fully reviewed: T0.1–T0.16 are all approved (see each Review). What remains is live proof in T0.L on the owner's machine.

**Scope:** review every phase 0 commit. Items 2, 3 and 4 get the most
attention.

### T0.L · Live check on the owner's machine
- **Owner:** Owner · **Size:** M · **Risk:** High · **Depends:** T0.R, T0.16
- **Status:** Todo
- **Result:** —
- **Review:** —

**Scope:** with Claude assisting, run setup, start, status, stop, backup and
restore-test on a spare port and project name, away from any running instance.

**Also check:** that OpenProject accepts `127.0.0.1:<port>` as its host name.

**Then make it one place** (added 2026-09-24). The owner already has an
OpenProject instance running with real data, started by older private
scripts outside this repo. After the spare-port check passes:
1. **Adopt, don't duplicate.** Point this toolkit at the existing instance:
   the same compose project name, the same named volumes, the same secrets.
   Set `OPL_PROJECT` and `OPL_RUNTIME_DIR` to match. Don't create a second
   instance.
2. **Move secrets out of every repo.** The runtime `.env` and the upstream
   clone go to a fixed folder outside all repositories (for example
   `~/.local/share/opl/runtime` inside WSL). The toolkit reads it through
   `OPL_RUNTIME_DIR`, recorded in the owner's shell profile.
3. **One set of controls.** From then on, start, stop, status, backup and
   update only through `bin/opl-*`. The older private scripts are archived
   with the old PM system (T6.4). Take a backup with `bin/opl-backup` and
   prove it with `bin/opl-restore-test` before archiving anything.

---

## Phase 1: Setup as code

**Spec:** DESIGN.md §1, §2, §4; §6 step 1.

### T1.1 · Spike: which mechanism applies each setup item
- **Owner:** Spark · **Size:** M · **Risk:** Low · **Depends:** —
- **Status:** Done
- **Result:** `docs/SETUP-MAPPING.md` created (this commit): all 22 items have a mechanism; evidence is v17.8.0 source files + docs URLs in the table. Corrections to the assumption: (1) no passwordless `active` users — bot users need generated passwords, tokens stay manual (T1.O); (2) no native admin-only on work-package fields (`admin_only` is project-fields-only) → Merge OK enforced conductor-side; applied as "Updated by T1.1" edits to T1.3/T1.4. Live re-checks for T1.L collected in the doc. Suite 37/37 OK (docs-only change). Timebox M overrun (source+docs research across ~12 fetches); remaining unknowns are marked in-table with reasons.

**Why:** OpenProject API v3 creates users, projects, memberships, versions and
saved queries. Admin configuration (statuses, types, workflows, roles, custom
fields, the progress mode) is not in API v3. The working assumption is a Ruby
script run with `bundle exec rails runner` inside the `web` container. Confirm
or correct that assumption **before** T1.3–T1.6 build on it.

**Source:** read OpenProject's own source and docs:
https://github.com/opf/openproject (use the tag matching the 17.x release in
the upstream compose `TAG`) and https://www.openproject.org/docs/.

**Output:** create `docs/SETUP-MAPPING.md` with one row per item:

| Item | Mechanism (API endpoint / Rails model + attributes / manual) | Idempotency key | Evidence (URL to source file or doc) | Community Edition? |

**Items to cover:**
- statuses, with `is_closed` and a default % done
- the status-based progress mode setting, and whether a parent's roll-up
  includes its own value
- work package types Epic, Feature and Task: do they exist by default?
- how default status per type works
- per-type, per-role workflows
- roles: project roles vs global roles in 17.x, and exact permission
  identifiers for view, add, edit, notes, subtasks, relations, versions and
  members
- work package custom fields: list, multi-list, user, bool, link; whether an
  admin-only flag exists; activation per type and project
- project custom fields (Repo, Visibility)
- users (create without password), and how a bot user gets an API token
- memberships
- versions
- saved queries: global and per project, filters on custom fields, group by,
  columns, timeline, starred/public
- adding a query to "My page"
- project status (On track / At risk) via API
- whether Epic → Feature → Task nesting can be enforced natively (expected:
  no; the conductor enforces it)

**Allowed edit to others' tasks:** if the findings contradict T1.2–T1.6,
update those tasks' text, mark each edit `Updated by T1.1`, then continue.

**Done when:** every item has a mechanism and evidence, or is marked
`unknown — needs live check` with the reason.

### T1.2 · Tracker model file, external config, and loaders
- **Owner:** Spark · **Size:** M · **Risk:** Low · **Depends:** T1.1
- **Status:** Done
- **Result:** This commit: `config/pm-model.toml` + `config/opl.example.toml` copied verbatim; `opl/model.py` (`load`, `transitions`, `ModelError`) and `opl/settings.py` (`load`, `load_file`, `token`, `redact`, `MissingSecret`) stdlib-only; `tests/test_model.py` (counts, DESIGN §1–§2 names, transitions, 9 validation rules) and `tests/test_settings.py` (example, tokens, redact, 4 validation rules); `tests/run.sh` + CI run `unittest discover`; `__pycache__/` ignored. Suite 60/60 OK via discovery (py 3.13) and `bash tests/run.sh`. Judgement calls (documented in code): transitions `"all"` = full cartesian incl. self-pairs; view fields = custom + project + 10 builtins; `versions` after `[[user]]` parses inside the last user table per TOML rules, so the loader accepts it from there; runner requires the 6 named limits + prod_signal + all 4 model tokens; redact skips values < 4 chars; unknown settings sections tolerated. Needs Python 3.11+ (tomllib); box `python` is 3.10, so verification used 3.13 and run.sh now selects the first ≥3.11 interpreter. Timebox M overrun.

**Files:**
- Create: `config/pm-model.toml`, `config/opl.example.toml`, `opl/__init__.py`,
  `opl/model.py`, `opl/settings.py`, `tests/test_model.py`,
  `tests/test_settings.py`
- Modify: `tests/run.sh` (run discovery),
  `.github/workflows/checks.yml` (run discovery),
  `.gitignore` (add `__pycache__/`)

**Produces:**
- `opl.model.load(path) -> Model` (frozen dataclasses: `statuses`, `types`,
  `roles`, `workflows`, `fields`, `project_fields`, `users`, `versions`,
  `views`)
- `Model.transitions(role, type) -> set[tuple[str, str]]` (returns all pairs
  for `"all"`)
- `opl.settings.load(config_dir=None) -> Settings`
  (reads `$OPL_CONFIG_DIR/opl.toml`, default `~/.config/opl/opl.toml`)
- `Settings.token(who) -> str`, where `who` is one of `"admin"`, `"claude"`,
  `"codex"`, `"spark"`, `"conductor"`, `"github"`. It reads the env var named
  in config and raises `MissingSecret(var_name)` without ever including the
  value.
- `opl.settings.redact(text, settings) -> str`: replaces every known secret
  value with `***`

**`config/pm-model.toml`:** copy exactly, then adjust only if T1.1 says so.

```toml
# Tracker model applied by bin/opl-configure. Generic: no real names or secrets.
# Names must match docs/DESIGN.md. Change DESIGN.md first, then this file.

[progress]
mode = "status"

[[status]]
name = "Open"
closed = false
done_ratio = 0
[[status]]
name = "Closed"
closed = true
done_ratio = 100
[[status]]
name = "Proposed"
closed = false
done_ratio = 0
[[status]]
name = "Approved"
closed = false
done_ratio = 0
[[status]]
name = "Building"
closed = false
done_ratio = 0
[[status]]
name = "In test"
closed = false
done_ratio = 0
[[status]]
name = "In production"
closed = false
done_ratio = 0
[[status]]
name = "Done"
closed = true
done_ratio = 100
[[status]]
name = "Parked"
closed = true
done_ratio = 0
[[status]]
name = "Rejected"
closed = true
done_ratio = 0
[[status]]
name = "Draft"
closed = false
done_ratio = 0
[[status]]
name = "Ready"
closed = false
done_ratio = 0
[[status]]
name = "In progress"
closed = false
done_ratio = 30
[[status]]
name = "Blocked"
closed = false
done_ratio = 30
[[status]]
name = "In review"
closed = false
done_ratio = 70
[[status]]
name = "Merged"
closed = true
done_ratio = 100

[[type]]
name = "Epic"
statuses = ["Open", "Closed"]
default_status = "Open"
[[type]]
name = "Feature"
statuses = ["Proposed", "Approved", "Building", "In test", "In production", "Done", "Parked", "Rejected"]
default_status = "Proposed"
[[type]]
name = "Task"
statuses = ["Draft", "Ready", "In progress", "In review", "Merged", "Blocked"]
default_status = "Draft"

[[role]]
name = "Owner"
permissions = "all"
[[role]]
name = "Model"
permissions = ["view_project", "view_work_packages", "add_work_packages", "edit_work_packages", "add_work_package_notes", "manage_subtasks", "manage_work_package_relations", "assign_versions", "view_members"]
[[role]]
name = "Conductor"
permissions = ["view_project", "view_work_packages", "edit_work_packages", "add_work_package_notes", "view_members"]

[[workflow]]
role = "Owner"
type = "*"
transitions = "all"
[[workflow]]
role = "Model"
type = "Epic"
transitions = []
[[workflow]]
role = "Model"
type = "Feature"
transitions = []
[[workflow]]
role = "Model"
type = "Task"
transitions = [
  ["Ready", "In progress"],
  ["In progress", "In review"],
  ["In review", "In progress"],
  ["Draft", "Blocked"], ["Ready", "Blocked"], ["In progress", "Blocked"], ["In review", "Blocked"],
  ["Blocked", "Ready"], ["Blocked", "In progress"],
]
[[workflow]]
role = "Conductor"
type = "Feature"
transitions = [["Approved", "Building"], ["Building", "In test"], ["In test", "In production"]]
[[workflow]]
role = "Conductor"
type = "Task"
transitions = [
  ["Draft", "Ready"], ["In review", "Merged"],
  ["Draft", "Blocked"], ["Ready", "Blocked"], ["In progress", "Blocked"], ["In review", "Blocked"],
]

[[field]]
name = "Risk"
on = ["Feature", "Task"]
format = "list"
values = ["Low", "Medium", "High"]
[[field]]
name = "Spec link"
on = ["Feature"]
format = "link"
[[field]]
name = "Models"
on = ["Feature"]
format = "list"
multi = true
values = ["Claude", "Codex", "Spark"]
[[field]]
name = "Test result"
on = ["Feature"]
format = "list"
values = ["Pass", "Fail"]
[[field]]
name = "Reviewer"
on = ["Task"]
format = "user"
[[field]]
name = "Size"
on = ["Task"]
format = "list"
values = ["S", "M", "L"]
[[field]]
name = "PR link"
on = ["Task"]
format = "link"
[[field]]
name = "Review result"
on = ["Task"]
format = "list"
values = ["Pass", "Changes requested"]
[[field]]
name = "Merge OK"
on = ["Task"]
format = "bool"
admin_only = true
[[field]]
name = "Needs you"
on = ["Epic", "Feature", "Task"]
format = "bool"
[[field]]
name = "Action"
on = ["Epic", "Feature", "Task"]
format = "list"
values = ["Approve", "OK merge", "Decide deploy", "Unblock", "Reassign", "Confirm done"]

[[project_field]]
name = "Repo"
format = "link"
[[project_field]]
name = "Visibility"
format = "list"
values = ["Public", "Private"]

# The owner is the existing admin account and gets the Owner role everywhere.
[[user]]
login = "claude"
name = "Claude"
role = "Model"
projects = "all"
[[user]]
login = "codex"
name = "Codex"
role = "Model"
projects = "all"
[[user]]
login = "spark"
name = "Spark"
role = "Model"
projects = "public"
[[user]]
login = "conductor"
name = "Conductor"
role = "Conductor"
projects = "all"

versions = ["Now", "Next", "Later"]

[[view]]
name = "Needs me"
scope = "global"
filters = [{ field = "Needs you", op = "=", values = ["true"] }]
sort = [["priority", "desc"]]
columns = ["id", "project", "type", "subject", "status", "Action", "priority", "assignee"]
starred = true
my_page = true
[[view]]
name = "Feature pipeline"
scope = "each-project"
filters = [{ field = "type", op = "=", values = ["Feature"] }, { field = "status", op = "open" }]
group_by = "status"
columns = ["id", "parent", "subject", "priority", "Risk", "progress", "Models", "version"]
[[view]]
name = "Feature pipeline (all projects)"
scope = "global"
filters = [{ field = "type", op = "=", values = ["Feature"] }, { field = "status", op = "open" }]
group_by = "status"
columns = ["id", "project", "parent", "subject", "priority", "Risk", "progress", "Models"]
[[view]]
name = "Roadmap"
scope = "each-project"
filters = [{ field = "type", op = "=", values = ["Feature"] }, { field = "status", op = "open" }]
group_by = "version"
columns = ["id", "subject", "status", "progress"]
timeline = true
[[view]]
name = "All projects"
scope = "global"
filters = [{ field = "type", op = "=", values = ["Feature"] }, { field = "status", op = "open" }]
group_by = "project"
columns = ["id", "subject", "status", "progress", "priority"]
```

**`config/opl.example.toml`:** copy exactly.

```toml
# Example only. Copy to $OPL_CONFIG_DIR/opl.toml (default ~/.config/opl/opl.toml)
# and replace the fake values. Token entries are environment variable NAMES.

[openproject]
url = "http://127.0.0.1:8080"
admin_token_env = "OPL_TOKEN_ADMIN"
owner_login = "admin"

[tokens]
claude = "OPL_TOKEN_CLAUDE"
codex = "OPL_TOKEN_CODEX"
spark = "OPL_TOKEN_SPARK"
conductor = "OPL_TOKEN_CONDUCTOR"

[github]
token_env = "OPL_GITHUB_TOKEN"

[users]
email_domain = "example.invalid"

[conductor]
live = false
interval_seconds = 60
state_dir = "~/.local/state/opl"

[runner]
# EXAMPLE ONLY. The owner sets the real worker command. {packet} is replaced
# with the task packet path, {workdir} with the isolated worktree.
command = ["example-worker", "--prompt-file", "{packet}", "--cwd", "{workdir}"]
max_parallel = 2

[runner.limits_minutes]
S = 20
M = 45
L = 90
review = 15
test = 20
stall = 10

[[project]]
key = "demo-public"
name = "Demo public project"
repo = "example-owner/demo-public"
visibility = "Public"
has_test_env = true
test_url = "http://127.0.0.1:3001"
test_signal = { kind = "workflow", name = "deploy-test" }
prod_signal = { kind = "workflow", name = "deploy-prod" }

[[project]]
key = "demo-private"
name = "Demo private project"
repo = "example-owner/demo-private"
visibility = "Private"
has_test_env = false
prod_signal = { kind = "environment", name = "production" }
```

**Validation rules.** `load()` raises `ModelError` / `SettingsError`
naming the problem when:
- a type lists an unknown status;
- a transition uses a status not in that type's list;
- a field `on` names an unknown type;
- a user's role is unknown;
- `projects` is not `all` or `public`;
- a view uses an unknown field;
- a project's visibility is not Public or Private;
- `has_test_env = true` but `test_signal` is missing;
- a signal kind is not `workflow` or `environment`;
- a runner limit is missing.

**Steps:**
- [ ] Write `tests/test_model.py`. Loading the shipped model succeeds and the
  counts match: 16 statuses, 3 types, 3 roles, 11 fields, 2 project fields,
  4 users, 3 versions, 5 views. Also write one failing-validation test per
  model rule above, using small TOML strings.
- [ ] Write `tests/test_settings.py`:
  - loading the example succeeds;
  - `token()` returns an env value and raises `MissingSecret` when it is
    missing, with the value absent from the message;
  - `redact()` masks values;
  - one failing-validation test per settings rule.
- [ ] Run the tests and see them fail. Implement. Run the tests and see them
  pass.
- [ ] Update `tests/run.sh` and CI to run `python -m unittest discover -s tests -v`.
  Add `__pycache__/` to `.gitignore`.
- [ ] Run the full suite and commit.

**Done when:** the suite passes and the model's names equal DESIGN.md §1–§2.

### T1.3 · Render the admin configuration script
- **Owner:** Spark · **Size:** L · **Risk:** Medium · **Depends:** T1.2
- **Status:** Done
- **Review:** (Claude, 2026-09-24, re-review) Approved (cd268ee). The Owner's "all" resolves from the registry and fails if it's empty; explicit names are checked with `AccessControl.permission`; `is_default` comes from `global_default`; the Draft→Proposed/Open rows are merged per role; `versions` and `global_default` sit at the top level, and the loader workaround is gone.
- **Result:** Review fixes (this commit): Owner permissions resolve non-global registry names at run time (verified `permissions`/`permission(name)`/`global?` in access_control.rb + permission.rb @ v17.8.0); unknown explicit names raise before assigning (golden-checked); `global_default = "Draft"` top-level + Draft→Proposed/Open escape rows for Owner/Model/Conductor (second rows merge with `"all"` in `transitions()`); loader allows the global default as from-status anywhere, requires it to name a status, and rejects `versions` inside any table; `versions`+`global_default` moved above `[progress]` in the shipped file (TOML scoping); is_default pinned from the model; progress-mode setting confirmed by review. Tests: merge union, global-default validation, nested-versions rejection, Owner registry/guard golden lines, Draft-escape counts (65/5). Suite green. Timebox L overrun (incl. a TOML-scoping trap: keys after `[progress]` nest inside it).

**Files:** `opl/configure/__init__.py`, `opl/configure/admin_ruby.py`,
`tests/test_admin_ruby.py`, `tests/golden/admin_small.rb`

**Produces:** `render_admin_script(model: Model) -> str` returns an
idempotent Ruby script for `rails runner`. It uses the Rails models and
attributes documented in `docs/SETUP-MAPPING.md`.

**Script behaviour, in this order:**
1. Statuses: `find_or_initialize_by(name:)`, then set closed and % done, then save.
2. Types: `find_or_initialize_by(name:)`, then save.
3. Roles: set the permissions exactly.
4. Workflows: for each (role, type), delete that pair's workflow rows only,
   then insert the listed transitions. `"all"` means every from/to pair of
   the type's statuses.
5. Work package custom fields: set format, values, multi and admin-only, and
   activate them for the listed types. (Updated by T1.1: no native admin-only
   exists on work-package fields — `admin_only` lives on project fields only —
   so ship `Merge OK` as a plain bool field and keep it owner-only by
   conductor enforcement instead.)
6. Project custom fields.
7. The progress-mode setting.

**The script never deletes** anything except the workflow rows for the roles
it manages. It prints one line per created or changed record; it prints no
line for unchanged ones.

**Steps:**
- [ ] Write a golden-file test: rendering a small model (2 statuses, 1 type,
  1 role, 1 transition, 1 field) equals `tests/golden/admin_small.rb`.
- [ ] Write an escaping test: a status named `It's "odd"` renders as a valid
  Ruby string literal.
- [ ] Write a "never deletes" test: the only `delete`/`destroy` calls are
  workflow deletes scoped by role_id and type_id.
- [ ] If `ruby` is on PATH, run `ruby -c` on the rendered full model;
  otherwise skip that test.
- [ ] Implement, run the full suite, commit.

**Done when:** the tests pass. The live run is part of T1.L.

### T1.4 · API client; projects, users, memberships, versions
- **Owner:** Spark · **Size:** M · **Risk:** Medium · **Depends:** T1.2
- **Status:** Done
- **Review:** (Claude, 2026-09-24) Approved. Bot passwords are random, discarded and never logged. Spark memberships in Private projects are removed. Dry run equals live, and it's idempotent.
- **Result:** This commit: `opl/openproject.py` (Basic-auth client, get/post/patch/delete + paged get_all, token-free ApiError), `opl/configure/apply_api.py`, `tests/fakes/http_fake.py` (FakeServer with per-route static/callable replies + request log), `tests/test_openproject.py`, `tests/test_apply_api.py` (stateful fake world: create-then-silent, spark-in-private removal, dry==live, Maintenance-once). Suite 75/75 OK (1 skip: ruby). Judgement calls: `delete()` added (needed for membership removal); bot users created active with a random discarded password, tokens stay manual; `get_all` follows `nextByOffset` (cap 100); Maintenance epic in Open + feature in Approved under it (epic has no Approved per model); dry-run plans the feature under the would-be epic; re-created users orphan old memberships (known limit). Assumed API shapes to prove live in T1.L: projects/schema custom-field ids, nested versions route, membership/project links, project-name filter. Timebox M overrun.

**Files:** `opl/openproject.py`, `opl/configure/apply_api.py`,
`tests/fakes/http_fake.py`, `tests/test_openproject.py`,
`tests/test_apply_api.py`

**Produces:**
- `Client(base_url, token)` with `get(path, params=None)`, `post(path, body)`,
  `patch(path, body)` and `get_all(path, params=None)`. `get_all` follows
  collection paging.
- Auth is the HTTP Basic header for user `apikey` with password = token.
  The token never appears in exceptions or logs.
- `ApiError(status, path, message)`.
- `apply_api(client, model, settings, dry_run) -> list[str]` returns
  human-readable actions ("create user spark", "add spark to demo-public as
  Model", ...).

**`FakeServer` in `tests/fakes/http_fake.py`:** a context manager running
`http.server` on `127.0.0.1:0`. Tests register handlers per (method, path).
It records requests (method, path, JSON body, headers) and serves canned JSON.

**Behaviour:**
- Create any missing project, then set its Repo and Visibility.
- Create any missing users with the email `<login>@<email_domain>`.
  (Updated by T1.1: `active` users require `password`/`auth_source`/`identity_url`
  or creation fails — generate an owner-held password per bot user.)
- Memberships:
  - Owner role everywhere for the owner;
  - users with `projects = "all"` get their role everywhere;
  - `spark` gets its role **only** where visibility is Public;
  - a Spark membership in a Private project is **removed** if found.
- Create versions Now, Next and Later per project.
- Create a **Maintenance** epic in each project with a **Maintenance** feature
  under it, in status Approved, so bugs and chores always have a parent
  (DESIGN.md §1).
- Idempotent: a second run against the same fake state performs no POST or
  PATCH.

**Steps:**
- [ ] Test: the auth header is Basic `apikey:<token>`, and on HTTP 500 the
  `ApiError` text has no token.
- [ ] Test: `get_all` walks 3 pages.
- [ ] Test: first `apply_api` creates the expected users, memberships and
  versions; the second call makes zero writes.
- [ ] Test: an existing Spark membership in a Private project is deleted.
- [ ] Test: the Maintenance epic and feature are created once per project,
  and not again on a second run.
- [ ] Test: `dry_run=True` makes zero writes and returns the same actions.
- [ ] Implement, run the full suite, commit.

**Done when:** the tests pass.

### T1.5 · Saved views
- **Owner:** Spark · **Size:** M · **Risk:** Low · **Depends:** T1.4
- **Status:** Done
- **Result:** This commit: `opl/configure/views.py` (`apply_views`: global + per-project copies, custom-field id resolution via WP schema, idempotent create/update by name+scope, My-page pin via grids) + shared `opl/configure/common.py` (project/user lookups; T1.6 will reuse) + `tests/test_views.py` (stateful fake: payload mapping, per-project copies, second-run silence, pin-once, dry==live). Suite 80/80 OK (1 skip: ruby). Judgement calls: friendly names map through one helper (builtins passthrough, customs to customField ids); `public: true` on shared views; `orders`/`timelineVisible`/`groupBy` key names are best guesses centralized for easy correction; widget match accepts HAL-nested or top-level query links; exact grid/query shapes MUST be proven live in T1.L and may need adjusting. Timebox M overrun.

**Files:** `opl/configure/views.py`, `tests/test_views.py`

**Produces:** `apply_views(client, model, settings, dry_run) -> list[str]`

**Behaviour:**
- Create or update the saved queries named in `model.views`: one per project
  for `each-project`, one global copy for `global`.
- Map friendly names (`Needs you`, `progress`, `version`, ...) to API names
  using `docs/SETUP-MAPPING.md`, and resolve custom field ids from the API.
- Matching is by name and scope.
- If T1.1 found a way to add "Needs me" to My page, do it. Otherwise record
  in `Result:` that the owner adds it by hand.

**Steps:**
- [ ] Test: the payload for "Needs me" has the custom-field filter, sort and
  columns.
- [ ] Test: "Roadmap" has timeline on and groups by version.
- [ ] Test: a second run makes zero writes.
- [ ] Implement, run the full suite, commit.

**Done when:** the tests pass.

### T1.6 · `opl-configure` command
- **Owner:** Spark · **Size:** M · **Risk:** Medium · **Depends:** T1.3, T1.4, T1.5
- **Status:** Done
- **Review:** (Claude, 2026-09-24) Approved. cp → rails runner → API order, temp script always removed, output redacted, failures name the step.
- **Result:** This commit: `bin/opl-compose` (pinned wrapper), `bin/opl-configure` (3.11+ selection, repo-root cd), `opl/configure/__main__.py` (load → token → dry-plan or cp+rails-runner+apply, temp script LF-only and always removed, subprocess output redacted, non-zero with named step on failure), shared `opl/configure/common.py` (new), `tests/test_configure_cli.py` (order cp→exec→API writes; dry-run silence; missing token names the var), `docs/CONFIGURE.md` + runbook section (53 lines, still one page). Suite 83/83 OK (1 skip: ruby). Judgement calls: compose subprocess goes through bash explicitly (Windows cannot exec shebangs); `python -m` needs repo-root cd in the launcher; test PATH prepends fakebin in native form so `shutil.which("bash")` keeps working; My-page pin and query key shapes from T1.5 carry over for live proof. Timebox M overrun.

**Files:** `bin/opl-compose`, `bin/opl-configure`,
`opl/configure/__main__.py`, `tests/test_configure_cli.py`,
`docs/RUNBOOK.md` (add a short "Configure the tracker" section, keeping the
runbook to one page; move detail into `docs/CONFIGURE.md` if needed)

**Behaviour:**
- `bin/opl-compose` sources `lib/common.sh` and runs `compose "$@"`.
- `bin/opl-configure [--dry-run]` runs `python3 -m opl.configure`, which:
  1. loads the model and settings;
  2. renders the admin script;
  3. copies it into `web` with `bin/opl-compose cp` and runs
     `bin/opl-compose exec -T web bundle exec rails runner <path>`;
  4. runs `apply_api` and `apply_views` with the admin token.
- `--dry-run` prints the rendered script's summary plus planned API actions,
  and runs nothing.
- Exit codes: non-zero on any failure. Secrets are never printed.

**Steps:**
- [ ] Test (fake docker + FakeServer): the full run calls `compose cp`, then
  `compose exec ... rails runner`, then the API writes, in that order.
- [ ] Test: `--dry-run` produces no docker calls and no writes.
- [ ] Test: a missing admin token exits non-zero and names the env var, not a
  value.
- [ ] Implement, write the docs, run the full suite, commit.

**Done when:** the tests pass.

### T1.R · Review phase 1
- **Owner:** Claude/Codex · **Size:** M · **Risk:** Medium · **Depends:** T1.1–T1.6
- **Status:** Done
- **Result:** —
- **Review:** (Claude, 2026-09-24, re-review) Phase 1 reviewed task by task: T1.1–T1.6 are all approved (see each Review). What remains for phase 1 is live proof in T1.L: the Ruby script's model names, the permission identifiers, and the query/grid payload shapes.

### T1.P · Draft the owner's private config (outside the repo)
- **Owner:** Claude/Codex · **Size:** S · **Risk:** Medium · **Depends:** T1.2
- **Status:** Done
- **Result:** (Codex, 2026-09-25) Private draft config created outside the repo (live=false, DRAFT header); loader validation passed. Owner reviews it in T1.O.
- **Review:** —

Write a **draft** of `$OPL_CONFIG_DIR/opl.toml` (default
`~/.config/opl/opl.toml`) from `config/opl.example.toml`, with the owner's
real projects: key, name, repo, visibility, local repo path, base ref, test
environment, deploy signals and test URL, taken from the existing tracker
registry and each repo's CI workflows. Tokens appear only as environment
variable **names**. Mark it DRAFT at the top; the owner reviews it in T1.O.
Never commit it anywhere, and never copy real values into this public repo.

### T1.O · Owner: tokens and external config
- **Owner:** Owner · **Size:** S · **Risk:** High · **Depends:** T0.L
- **Status:** Todo
- **Result:** —
- **Review:** —

**Steps:**
1. Create the admin API token in OpenProject (My account → Access tokens).
2. Write `~/.config/opl/opl.toml` from the example with the real projects.
3. Set the token environment variables.
4. After T1.L, create one token for each of the claude, codex, spark and
   conductor users. Their passwords are random and discarded, so for each:
   Administration → Users → the bot → set a temporary password; sign in
   as the bot; My account → Access tokens → create an API token; store it
   in the env var named in `opl.toml`; sign out; set a new random password
   you don't keep.

### T1.L · Live configure check
- **Owner:** Owner · **Size:** M · **Risk:** High · **Depends:** T1.R, T1.O
- **Status:** Todo
- **Result:** —
- **Review:** —

**Steps** (Claude assisting):
1. Run `bin/opl-configure --dry-run`, then run it for real.
2. Run it again: it should report no changes.
3. Check that the five views appear and that Spark is absent from every
   Private project.

---

## Phase 2: Model connection

**Spec:** DESIGN.md §5 "Model access to OpenProject"; §6 step 2.

### T2.1 · Review and pin the community MCP server
- **Owner:** Claude/Codex · **Size:** M · **Risk:** High · **Depends:** —
- **Status:** Done
- **Result:** (Claude, 2026-09-24) `docs/MCP.md`: approved `openproject-ce-mcp` 0.4.1 (repo renamed to `jtauschl/openproject-ce-mcp`), pinned by git commit and PyPI sha256. Audit: no telemetry, no hosts besides the base URL, no token logging, subprocess only in the unused setup helper, preview/confirm on every write, fail-closed allowlists. Config: per-model tokens, work-package write only, Spark allowlisted to Public projects only (second layer after T1.4 membership). T2.2 is unblocked.
- **Review:** —

**Candidate:** `jtauschl/openproject-mcp`, now `jtauschl/openproject-ce-mcp` (chosen: 0.4.1; see `docs/MCP.md`).

**Audit:**
- where its network calls go;
- how it stores and handles tokens;
- which write tools it exposes;
- dependencies and license.

**Output:** in `docs/MCP.md`, record the chosen commit or version, the
install command, the tools used, and the risks.

### T2.2 · MCP client setup for all three models
- **Owner:** Spark · **Size:** S · **Risk:** Low · **Depends:** T2.1
- **Status:** Done
- **Review:** (Claude, 2026-09-24) Fixed directly by the reviewer and approved. Spark's run moved on without picking up the change request, so the reviewer applied it: every snippet carries the full Required configuration, including all nine `false` flags; all three configs are user-scope (never committed); Codex forwards only the token via `env_vars` and is launched with the token scoped to one process; the Codex TOML uses a `[mcp_servers.openproject.env]` table, because the multi-line inline table was not valid TOML 1.0. Every JSON/TOML snippet was parsed and checked. Open item for T2.O: confirm `${OPL_TOKEN_CLAUDE}` expands in user scope (a fallback is documented).
- **Result:** This commit: "Connect" section in docs/MCP.md with a snippet each for Claude Code (.mcp.json + ${VAR}), Codex CLI (config.toml + env_vars forwarding) and OpenCode (opencode.json + {env:VAR}); tokens by name only, allowlists per T2.1 (Spark Public-only, never *). Formats verified against the live client docs (URLs cited inline). Codex cannot reference another var's value, so the doc exports OPENPROJECT_API_TOKEN from OPL_TOKEN_CODEX in the shell instead. Suite 190/190 green (docs-only change; re-ran to state it honestly). Timebox S overrun (doc reads).

**Output:** add a "Connect" section to `docs/MCP.md`, with a config snippet
each for Claude Code, Codex CLI and OpenCode.

**Rules:** each snippet uses the pinned server and references the token by
env var name only (`OPL_TOKEN_CLAUDE`, `OPL_TOKEN_CODEX`, `OPL_TOKEN_SPARK`).

**Done when:** the snippets match each client's documented config format;
cite the docs URLs.

### T2.3 · Permission check command
- **Owner:** Spark · **Size:** M · **Risk:** Medium · **Depends:** T1.4
- **Status:** Done
- **Review:** (Claude, 2026-09-24) Approved. The probe task is cleaned up with the admin token, and the forbidden-approve and Private-visibility checks fail correctly.
- **Result:** This commit: `opl/permcheck.py` (`run_all` returning Checks, `main` printing PASS/FAIL + exit code), `bin/opl-permcheck`, `tests/test_permcheck.py` (all-pass incl. probe cleanup, approve-allowed FAIL, spark-sees-private FAIL, missing token FAIL, main rc 0), optional `[permcheck]` table in settings loader + example + loader test, `find_project`/`project_field_ids` in `opl/configure/common.py` (apply_api refactored onto the latter, covered by its suite). Suite 89/89 OK (1 skip: ruby). Judgement calls: model users = settings tokens minus conductor; setup lookups use the admin client, permission assertions use each user's own client; probe WP deleted with the admin token; PATCH-via-status-link and grid/query key shapes from T1.4/T1.5 carry the same live-proof flag; added optional `[permcheck] feature` (default "Test feature") since the task's "test feature" needs an identifier. Timebox M overrun.

**Files:** `opl/permcheck.py`, `bin/opl-permcheck`, `tests/test_permcheck.py`

**Behaviour.** For each model user in the settings, using that user's token:
1. can read work packages in a sandbox project named in config
   (`[permcheck] project = "..."`, added to the example);
2. can create a Draft task under the sandbox's test feature, then deletes it
   using the admin token;
3. **cannot** move a Proposed feature to Approved (expects HTTP 422 or 403);
4. for spark only: `GET /api/v3/projects` returns no Private project.

It prints PASS/FAIL per check and exits non-zero on any FAIL.

**Steps:**
- [ ] Tests (FakeServer): all-pass, a model *allowed* to approve (FAIL),
  Spark seeing a Private project (FAIL).
- [ ] Implement, run the full suite, commit.

**Done when:** the tests pass.

### T2.O · Owner: connect the models and run the permission check
- **Owner:** Owner · **Size:** M · **Risk:** High · **Depends:** T2.1, T2.2, T2.3, T1.L
- **Status:** Todo
- **Result:** —
- **Review:** —

---

## Phase 3: Conductor, watch mode

**Spec:** DESIGN.md §2, §3, §4, §5; §6 step 3.

### T3.1 · State model
- **Owner:** Spark · **Size:** M · **Risk:** Medium · **Depends:** T1.2
- **Status:** Done
- **Review:** (Claude, 2026-09-24) Approved. `apply_changes` is pure, stamps `status_since`, and handles PR merges.
- **Result:** This commit: `opl/conductor/state.py` (task code verbatim + `apply_changes`: new World, status changes stamp `status_since`, pr/merge marks merged at now, unknown targets raise) + `tests/test_state.py` (all 5 listed cases + empty-changes purity). Suite 95/95 OK (1 skip: ruby). No judgement calls beyond the specified code.

**Files:** `opl/conductor/__init__.py`, `opl/conductor/state.py`,
`tests/test_state.py`

**Code:** use this exactly; add only what the tests need.

```python
from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime

@dataclass(frozen=True)
class Signal:
    kind: str          # "workflow" | "environment"
    name: str

@dataclass(frozen=True)
class Project:
    key: str
    op_id: int
    repo: str
    visibility: str    # "Public" | "Private"
    has_test_env: bool
    test_signal: Signal | None
    prod_signal: Signal
    at_risk: bool = False

@dataclass(frozen=True)
class Item:
    id: int
    project: str               # Project.key
    type: str                  # "Epic" | "Feature" | "Task"
    status: str
    status_since: datetime
    parent_id: int | None = None
    assignee: str | None = None        # user login
    reviewer: str | None = None
    size: str | None = None            # "S" | "M" | "L"
    risk: str | None = None            # "Low" | "Medium" | "High"
    pr_url: str | None = None
    review_result: str | None = None   # "Pass" | "Changes requested"
    merge_ok: bool = False
    test_result: str | None = None     # "Pass" | "Fail"
    needs_you: bool = False
    action: str | None = None
    models: tuple[str, ...] = ()
    predecessors: tuple[int, ...] = () # ids of items that must be Merged first
    lock_version: int = 0

@dataclass(frozen=True)
class PullRequest:
    url: str
    merged: bool
    merged_at: datetime | None
    checks_green: bool

@dataclass(frozen=True)
class Deploy:
    project: str
    target: str        # "test" | "production"
    at: datetime

@dataclass(frozen=True)
class World:
    now: datetime
    projects: dict[str, Project]
    items: dict[int, Item]
    pull_requests: dict[str, PullRequest]
    deploys: tuple[Deploy, ...] = ()

    def children(self, item_id: int) -> list[Item]:
        return [i for i in self.items.values() if i.parent_id == item_id]

@dataclass(frozen=True)
class Change:
    rule: str          # "enforce" | "stages" | "screens" | "merge"
    target: str        # "item" | "project" | "pr"
    key: str           # str(item id), project key, or PR url
    field: str         # Item/Project attribute name, or "merge" for target "pr"
    new: object
    reason: str        # one human sentence; becomes the comment

def apply_changes(world: World, changes: list[Change]) -> World:
    """Return a new World with item/project changes applied (status changes also
    set status_since=world.now). A "pr"/"merge" change marks that PR merged at now."""
```

**Steps:**
- [ ] Tests for `apply_changes`:
  - a status change updates `status` and `status_since`;
  - a project `at_risk` change;
  - a PR merge sets `merged=True` and `merged_at=now`;
  - the input World is unchanged;
  - `children()` returns only direct children.
- [ ] Implement, run the full suite, commit.

### T3.2 · Build the World from OpenProject
- **Owner:** Spark · **Size:** M · **Risk:** Medium · **Depends:** T3.1, T1.4
- **Status:** Done
- **Review:** (Claude, 2026-09-24) Approved. The journal-first `status_since` with an `updatedAt` fallback is fine; T3.W will show whether the fallback ever fires.
- **Result:** This commit: `opl/conductor/collect.py` (`collect_openproject`; T3.3 appends the GitHub half) + `tests/test_collect_op.py` (canned epic/feature/tasks with customs + relation mapping; missing-project error). Suite 97/97 OK (1 skip: ruby). Judgement calls: match settings projects by identifier then name; HAL-driven links with constructed-URL fallback for activities; journal-first status_since with updatedAt fallback (the fallback the task asks to record); int/str id normalization on all reverse maps; `now` accepted but unused (reserved). Assumed shapes for live proof in T1.L: customField props, activities journal entry shape, relations links, nested-list endpoints. Timebox M overrun.

**Files:** `opl/conductor/collect.py` (the `collect_openproject` part),
`tests/test_collect_op.py`

**Produces:** `collect_openproject(client, settings, model, now) -> tuple[dict[str, Project], dict[int, Item]]`

**Behaviour:**
- Reads projects that appear in settings, and all their work packages of
  types Epic, Feature and Task.
- Resolves custom-field ids by name through the schema endpoints.
- Maps precedes/follows relations into `predecessors`.
- `status_since` is taken from the latest status change in the activity
  journal. If that isn't available, it falls back to `updatedAt` and records
  this in `Result:`.

**Steps:**
- [ ] Test (FakeServer with canned HAL): 1 epic, 1 feature and 2 tasks, with
  custom fields and one relation, map to the expected Items.
- [ ] Test: a project in settings but missing from OpenProject raises a clear
  error.
- [ ] Implement, run the full suite, commit.

### T3.3 · GitHub client and PR/deploy collection
- **Owner:** Spark · **Size:** M · **Risk:** Medium · **Depends:** T3.1
- **Status:** Done
- **Review:** (Claude, 2026-09-24) Approved. Green means combined status success and every check run successful or skipped. The token never appears in errors.
- **Result:** This commit: `opl/github.py` (Bearer client, PR state + combined/check-runs greenness, squash merge, workflow-run and environment deploys), `collect_github` in `opl/conductor/collect.py`, `tests/test_github.py` (merged/open-failing PRs, both deploy kinds, token-free errors, collect wiring). Suite 104/104 OK (1 skip: ruby). Judgement calls: green = combined success AND every check-run success/skipped; workflow matched by name or path; deploys use run updated_at / status created_at; statuses read first page (per_page=100); `collect_github` lives in collect.py per the file map (retargeted prod-workflow included); request plumbing duplicates the OP client deliberately (different auth). Endpoint shapes to prove live in T1.L. Timebox M overrun.

**Files:** `opl/github.py`, `opl/conductor/collect.py` (the `collect_github`
part), `tests/test_github.py`

**Produces:**
- `GitHub(token, base_url="https://api.github.com")` with:
  - `pull_request(url) -> PullRequest`: merged state, and checks green when
    the combined status is success and all check runs are success or
    skipped;
  - `merge(url, method="squash")`;
  - `deploys(project) -> list[Deploy]`: for `workflow` signals, successful
    runs of the named workflow on the default branch; for `environment`
    signals, successful deployments to the named environment.
- `collect_github(gh, projects, items) -> tuple[dict[str, PullRequest], tuple[Deploy, ...]]`

**Steps:**
- [ ] Tests (FakeServer as the GitHub API): a merged PR, an open PR with a
  failing check, workflow-run deploys and environment deploys.
- [ ] Test: the token is not in errors.
- [ ] Implement, run the full suite, commit.

### T3.4 · Rule: enforce
- **Owner:** Spark · **Size:** M · **Risk:** High · **Depends:** T3.1
- **Status:** Done
- **Review:** (Claude, 2026-09-24, re-review) Approved (7a25235). E4 and E5 only judge a reviewer that is set; E6 still covers a missing reviewer after approval. The planning-then-approval test proves both. The rule and scenario suites pass.
- **Result:** Review fix (7a25235): E4/E5 judge only a set reviewer (a missing reviewer during planning is E6's job after approval); E3 unchanged. Added the specified planning-vs-approved test. Suite 176/176 green.

**Files:** `opl/conductor/rules/__init__.py`,
`opl/conductor/rules/enforce.py`, `tests/test_rule_enforce.py`

**Produces:**
- `violations(world) -> dict[int, str]`: item id → reason
- `enforce(world) -> list[Change]`

**Violations**, checked on items that are not Merged or closed:

| # | Condition | Reason text |
|---|---|---|
| E1 | Task whose parent is missing or not a Feature | `Task must sit under a Feature` |
| E2 | Feature whose parent is missing or not an Epic | `Feature must sit under an Epic` |
| E3 | Task assignee `spark` in a Private project | `Spark may not work on Private projects` |
| E4 | Reviewer equals assignee, **unless** Public and risk Low and both are `spark` | `Reviewer must differ from builder` |
| E5 | Risk Medium or High, or the project is Private, and the reviewer is not `claude` or `codex` | `Risk needs a Claude or Codex reviewer` |
| E6 | Task under a Feature that is Approved or later, missing assignee, reviewer, size or risk | `Task needs assignee, reviewer, size and risk` |

**`enforce()`:** for each violating **Task** not already Blocked, emits
`Change(rule="enforce", target="item", key=id, field="status", new="Blocked", reason=<reason>)`.
Non-task violations produce no change here; the screens rule uses
`violations()` for them.

**Steps:**
- [ ] One test per row: the violating case yields the change, and a
  compliant case yields none.
- [ ] Test: an already-Blocked violating task yields no change (idempotent).
- [ ] Test: a Draft task under a Proposed feature missing fields is **not**
  a violation.
- [ ] Implement, run the full suite, commit.

### T3.5 · Rule: stages
- **Owner:** Spark · **Size:** L · **Risk:** High · **Depends:** T3.4
- **Status:** Done
- **Review:** (Claude, 2026-09-24, re-review) Approved (a1b8102 plus a Claude follow-up commit). S0 moves Draft Features to Proposed and Draft Epics to Open, and an all-unknown merge time blocks S4/S5. Follow-up done directly by the reviewer: `_latest_merge` now returns None when **any** merged task's merge time is unknown, not only when all are. A deploy newer than the known merges could still predate the unknown one. Test: `test_one_unknown_merge_time_blocks_deploy_moves`.
- **Result:** Review fixes (this commit): S0 moves Feature/Epic out of the global default (model-driven via new stages(world, model) signature; engine passes it through); unknown merge times block S4/S5 (stale deploys cannot promote). Tests: S0 positives, S0 skip on violation, stale-deploy silence, idempotency. Suite 180/180 green.

**Files:** `opl/conductor/rules/stages.py`, `tests/test_rule_stages.py`

**Produces:** `stages(world) -> list[Change]`

**Moves.** Items with a `violations()` entry are skipped.

| # | From → To | When | Reason text |
|---|---|---|---|
| S0 | Feature Draft → Proposed; Epic Draft → Open | created without an explicit status, so it got the global default (added by review of T1.3) | `Proposed: created without a status` / `Open: created without a status` |
| S1 | Task Draft → Ready | parent Feature is Approved or Building, and every predecessor is Merged | `Ready: feature approved and predecessors merged` |
| S2 | Feature Approved → Building | any child task is In progress, In review, Merged or Blocked | `Building: work started` |
| S3 | Task In review → Merged | its PR is merged | `Merged: pull request merged` |
| S4 | Feature Building → In test | all child tasks are Merged, **and** (`has_test_env` is false, **or** a test deploy exists with `at` ≥ the latest `merged_at` of the child PRs) | `In test: all tasks merged and deployed to test` / `In test: all tasks merged (no test environment)` |
| S5 | Feature In test → In production | a production deploy exists with `at` ≥ the latest `merged_at` of the child PRs | `In production: production deploy seen` |

A feature with no tasks never moves. If it is Approved or later, the screens
rule marks it Unblock with reason `Feature has no tasks`. A Proposed feature
with no tasks is still being planned and is left alone.

**Steps:**
- [ ] Tests: each row's positive case, and each row's closest negative case:
  - S1 with one predecessor still In review;
  - S4 with a test deploy older than the last merge;
  - S5 with no production deploy.
- [ ] Test: a feature with no tasks produces no change.
- [ ] Idempotency test: apply the changes with `apply_changes`, rerun
  `stages`, and get `[]`.
- [ ] Implement, run the full suite, commit.

### T3.6 · Rule: screens
- **Owner:** Spark · **Size:** M · **Risk:** Medium · **Depends:** T3.4
- **Status:** Done
- **Review:** (Claude, 2026-09-24) Approved. The priority order, the 2-vs-3-day reassign, the models list and at-risk all match DESIGN §4.
- **Result:** This commit: `opl/conductor/rules/screens.py` (`screens` with 6 ordered actions + models/at_risk maintenance) + `tests/test_rule_screens.py` (per-action, true precedence, 2-vs-3-day reassign, models, at-risk, idempotency). Suite 137/137 OK (1 skip: ruby). Judgement calls: first match wins, so taskless Approved-or-later features Unblock even when Decide/Confirm shapes match (per T3.5's handoff text); Unblock reason prefers the violation text, then blocked, then no-tasks; display names are capitalized logins; at-risk is project-scoped (Blocked tasks + violations); changes fire on value differences only. Timebox M kept.

**Files:** `opl/conductor/rules/screens.py`, `tests/test_rule_screens.py`

**Produces:** `screens(world) -> list[Change]`

**Action per item.** If several apply, the first in this list wins:
1. `Unblock`: the task is Blocked, the item has a `violations()` entry, or
   it's a Feature that is Approved or later with no tasks
2. `OK merge`: task risk High, status In review, review_result Pass, and not
   merge_ok
3. `Approve`: feature is Proposed
4. `Decide deploy`: feature is In test and test_result is set
5. `Confirm done`: feature is In production
6. `Reassign`: task assignee is claude or codex, status Ready or In review,
   and `now - status_since` ≥ 3 days

**Writes:**
- `needs_you = (action is not None)`; a change is emitted only when the
  value differs.
- Feature `models` = the sorted display names (`claude` → `Claude`, etc.) of
  its child tasks' assignees.
- Project `at_risk` = the project has any Blocked task or any violation.

**Steps:**
- [ ] One test per action, plus a precedence test (a Blocked High-risk task
  in review → Unblock).
- [ ] Reassign test with a fake `now` at 2 days (no action) and at 3 days
  (Reassign).
- [ ] Tests for the models list and at_risk.
- [ ] Idempotency test.
- [ ] Implement, run the full suite, commit.

### T3.7 · Rule: merge
- **Owner:** Spark · **Size:** M · **Risk:** High · **Depends:** T3.4
- **Status:** Done
- **Review:** (Claude, 2026-09-24) Approved. All seven gates match the spec.
- **Result:** This commit: `opl/conductor/rules/merge.py` (`merge`) + `tests/test_rule_merge.py` (all 7 listed gates). Suite 144/144 OK (1 skip: ruby). No judgement calls beyond the specified condition and reason strings. Timebox M kept.

**Files:** `opl/conductor/rules/merge.py`, `tests/test_rule_merge.py`

**Produces:** `merge(world) -> list[Change]`

**Condition:** a task is In review, has a pr_url whose PR exists and is not
merged, review_result is Pass, checks are green, it has no violation, and
(risk is not High, or merge_ok is true).

**Output:** `Change(rule="merge", target="pr", key=pr_url, field="merge", new=True, reason="Merge: review passed and checks green" + (", owner OK" if High))`

**Steps:**
- [ ] Tests:
  - positive Low;
  - High without merge_ok (none);
  - High with merge_ok (one);
  - checks red (none);
  - review "Changes requested" (none);
  - PR already merged (none);
  - a violating task (none).
- [ ] Implement, run the full suite, commit.

### T3.8 · Engine, ownership guard, loop, watch mode
- **Owner:** Spark · **Size:** M · **Risk:** High · **Depends:** T3.2, T3.3, T3.5, T3.6, T3.7
- **Status:** Done
- **Review:** (Claude, 2026-09-24, re-review) Approved (ea8be32). Custom-option links come from the per-project-type schema's `allowedValues` (a multi-select sends a list, and None clears); there is one grouped PATCH per item per cycle; comments are posted only for status changes and merges (the merge comment goes on the PR's task); and the lookups use `/work_packages/schemas/{project}-{type}` and `/types`. The project status link is marked verify-live in SETUP-MAPPING. All five requested tests are present and pass.
- **Result:** Review fixes (this commit): list fields as custom-option links (single/multi/clearing) resolved from per-type schema allowedValues; one PATCH per item per cycle with comments only for status changes and merges; project status via _links href (mapping doc updated, still verify-live); per-type schemas replace the non-existent global /schema path. Tests: option links, multi list, single PATCH, comment gating. Suite 185/185 green.

**Files:** `opl/conductor/engine.py`, `opl/conductor/__main__.py`,
`bin/opl-conductor`, `tests/test_engine.py`

**Produces:**
- `run_once(world, model) -> list[Change]`:
  - runs enforce, stages, screens and merge;
  - drops changes whose new value already equals the current value;
  - when two changes set the same (target, key, field) to different values,
    **drops both** and logs a conflict;
  - **ownership guard:** drops any status change whose (from, to) is not in
    `model.transitions("Conductor", item.type)`, and logs it.
- `apply(changes, world, op_client, gh, live, log_path)`:
  - **watch mode** (default) appends one JSON line per change to
    `<state_dir>/watch.log` with fields time, rule, target, key, field, old,
    new, reason;
  - **live mode** PATCHes OpenProject with lockVersion, posts `reason` as a
    comment on the item **only for status changes and merges** (added by
    review 2026-09-24: screen fields `needs_you`, `action`, `models` and
    `at_risk` change often, so commenting on them would bury real history;
    OpenProject's own change log already records them under the Conductor
    user), merges the PR via `gh.merge`, and sets a project's
    status to At risk or On track (API mechanism from
    `docs/SETUP-MAPPING.md`).
- The CLI is `bin/opl-conductor [--once] [--live]`.
  - Live requires both `--live` **and** `[conductor] live = true`.
  - The loop runs every `interval_seconds`.
  - When OpenProject or GitHub is unreachable, it logs, skips the cycle, and
    keeps running.
  - Secrets are redacted in all output.

**Steps:**
- [ ] Test: an engine conflict drops both changes.
- [ ] Test: the guard drops `Proposed → Approved` and `In production → Done`.
- [ ] Test: watch mode writes JSON lines and makes no HTTP calls.
- [ ] Test: live mode PATCHes and comments (FakeServer).
- [ ] Test: `--live` without the config flag stays in watch mode.
- [ ] Test: an unreachable server is skipped without a crash.
- [ ] Implement, run the full suite, commit.

### T3.9 · End-to-end scenario and idempotency
- **Owner:** Spark · **Size:** L · **Risk:** Medium · **Depends:** T3.8
- **Status:** Done
- **Review:** (Claude, 2026-09-24, re-review) Approved. The lifecycle, the Private block with at-risk, the reassign, and the quiet second run after every step.
- **Result:** This commit: `tests/test_scenarios.py` (full lifecycle incl. dependent-task ordering and per-step Action assertions per DESIGN §4; private-project E3 block + At risk with reason capture; 3-day reassign; settle() asserts a quiet second run_once after every step). Suite 158/158 OK (1 skip: ruby). No rule fixes needed. Timebox L kept.

**Files:** `tests/test_scenarios.py`

**Scenarios**, on in-memory Worlds using `run_once` + `apply_changes`:
1. **Full lifecycle.** A feature is Proposed, then the owner approves it
   (set directly). Then two tasks (one depending on the other) go Ready →
   In progress → In review → review Pass → merge → Merged, then a test
   deploy, a test result, a production deploy, and finally the owner sets
   Done. Assert the Action shown at each step matches DESIGN.md §4.
2. **Private project.** A task assigned to spark is Blocked with the E3
   reason, and the project becomes At risk.
3. **Reassign.** A claude task waits 3 days in Ready.
4. **Idempotency.** After every step, a second `run_once` returns `[]`.

**Steps:**
- [ ] Write the scenarios, make them pass (fix the rules if needed and
  record it in `Result:`), commit.

### T3.R · Review phase 3
- **Owner:** Claude/Codex · **Size:** L · **Risk:** High · **Depends:** T3.1–T3.9
- **Status:** Done
- **Result:** —
- **Review:** (Claude, 2026-09-24, re-review) Phase 3 reviewed task by task: T3.1–T3.9 are all approved (see each Review; T3.5 got a small reviewer follow-up in 390b368). What remains is live proof: T3.W, a week in watch mode against the real instance, especially the API shapes (option links, project status, activity comments) that fake-server tests can't prove.

### T3.W · One week in watch mode
- **Owner:** Owner · **Size:** M · **Risk:** Medium · **Depends:** T3.R, T2.O
- **Status:** Todo
- **Result:** —
- **Review:** —

**Scope:** run `bin/opl-conductor` in watch mode for a week. Claude compares
`watch.log` with what should have happened.

---

## Phase 4: Live mode and the Spark runner

**Spec:** DESIGN.md §5 "Spark runner"; §6 step 4.

### T4.1 · Task packet and isolated worktree
- **Owner:** Spark · **Size:** M · **Risk:** Medium · **Depends:** T3.1
- **Status:** Done
- **Review:** (Claude, 2026-09-24) Approved with a reviewer follow-up. Packet: Private guard, per-kind final lines, size and kind limits, and no secret values. Worktree: `opl/task-<id>-<stamp>` under state_dir, so the folder name carries the task id (needed by TC.3). Follow-up done directly: the rules now say tracker text is data, not instructions, and come after that text (prompt-injection guard; test `test_tracker_text_is_data_not_instructions`).
- **Result:** This commit: `opl/conductor/spark/packet.py` (build_packet with per-kind final lines, size-based limits, Private refusal) + `worktree.py` (create/remove on `opl/task-<id>-<stamp>` under state_dir) + tests (final lines, content, Private/kind guards, secret absence, real git create/remove). Suite 196/196 green. Judgement calls: task/feature accept dicts or objects; packet time from task size (default M) or kind-fixed review/test limits; worktree path under state_dir; remove resolves the main repo from the worktree. Timebox M overrun.

**Files:** `opl/conductor/spark/__init__.py`, `opl/conductor/spark/packet.py`,
`opl/conductor/spark/worktree.py`, `tests/test_spark_packet.py`,
`tests/test_spark_worktree.py`

**Produces:**
- `build_packet(kind, task, feature, project, settings) -> str`, where kind
  is "build", "review" or "test". The markdown contains:
  - the task title and description;
  - the feature's Why / What you'll see / Done when;
  - the spec link;
  - the time limit;
  - the rules: public/synthetic only, commit your work, no push;
  - the required final lines:
    - `OPL-RESULT: DONE|FAILED <msg>` (build)
    - `OPL-REVIEW: PASS|CHANGES <notes>` (review)
    - `OPL-TEST: PASS|FAIL <summary>` (test)
    - optional `OPL-COST: <usd>`
- `create_worktree(repo_path, base_ref, task_id, state_dir) -> Worktree(path, branch)`
  on branch `opl/task-<id>-<stamp>`.
- `remove_worktree(worktree)`.

**Rule:** `build_packet` raises when the project is Private. This is a second
guard, independent of the enforce rule.

**Steps:**
- [ ] Packet tests: each kind contains its required final line; a Private
  project raises; no secret value appears.
- [ ] Worktree tests against a temporary local git repo: the branch is
  created and removal is clean.
- [ ] Implement, run the full suite, commit.

### T4.2 · Supervisor: time limits, stall stop, parallel cap
- **Owner:** Spark · **Size:** M · **Risk:** High · **Depends:** T4.1
- **Status:** Done
- **Review:** (Claude, 2026-09-24) Approved (986ddc2). Hard deadline, stall on no log growth and no worktree mtime change, tree kill (killpg / taskkill /T), OPL_TIME_SCALE for tests, and Slots cap. 5/5 supervisor tests pass.
- **Result:** This commit: `opl/conductor/spark/supervisor.py` (`run_worker` with timeout/stall/tree-kill, cost parsing, OPL_TIME_SCALE; `Slots`) + `tests/fakes/worker_*.py` + `tests/test_supervisor.py` (per-fake outcomes incl. kill proof, slot cap, log content). Suite 202/202 green. Judgement calls: stall = no output bytes AND no workdir mtime change (log file excluded; monotonic vs wall clocks never mixed; mtime walks throttled to 1s); POSIX killpg vs taskkill; spawn failures raise; same-id acquire returns False. Timebox M overrun.

**Files:** `opl/conductor/spark/supervisor.py`, `tests/test_supervisor.py`,
`tests/fakes/worker_*.py`

**Produces:** `run_worker(cmd, workdir, limit_s, stall_s, log_path, clock=time.monotonic) -> RunResult(outcome, duration_s, last_lines, cost_usd)`
- `outcome` is one of `"success"`, `"failed"`, `"timeout"`, `"stalled"`.
- **Stall:** no new stdout/stderr bytes **and** no file mtime change under
  `workdir` for `stall_s` seconds.
- **Stopping:** kill the whole process tree. On POSIX use
  `start_new_session=True` + `os.killpg`; on Windows use
  `CREATE_NEW_PROCESS_GROUP` + `taskkill /T /F`.
- **Scaling:** `OPL_TIME_SCALE` (a float, default 1) scales every limit, so
  tests can run in seconds.
- `Slots(max_parallel)` tracks running tasks, with `acquire(task_id) -> bool`
  and `release`.

**Fake workers:** `worker_ok.py` prints `OPL-RESULT: DONE`, `worker_hang.py`
keeps printing forever, `worker_silent.py` sleeps silently,
`worker_fail.py` exits 1.

**Steps:**
- [ ] One test per fake worker, with the expected outcome within
  scaled time.
- [ ] Test: the 3rd acquire with `max_parallel=2` returns False.
- [ ] Test: the log file contains the output.
- [ ] Implement, run the full suite, commit.

### T4.3 · Build-run outcomes
- **Owner:** Spark · **Size:** M · **Risk:** High · **Depends:** T4.2, T3.8
- **Status:** Done
- **Review:** (Claude, 2026-09-24, re-review) Approved (7987441). The token only travels in GIT_CONFIG_* env and errors are redacted; there is a fresh lockVersion per PATCH, an async tick, the DONE gate, WIP commits naming the branch, a PR body linking the task, and live subject/description in the packet. Three leftovers moved into T4.4's list because they touch the same files: the multi-line section splitter, no network in tests, and non-interactive git.
- **Result:** Review fixes (this commit): packets fetch live subject/description.raw with Why/What/Done section parsing (whole-description fallback); push token via GIT_CONFIG_* env with all shell errors redacted; fresh lockVersion before every PATCH with a 409-enforcing fake; non-blocking tick with reap-on-later-ticks; DONE-line gating via outcomes.parse_final_line; WIP commits on dirty trees with branch named in Blocked comments; PR body links the task and names the feature. Per-attempt logs were already distinct. Tests: packet content, push-env rules, failing-push leak check, version sequence + stale refresh, fast-returning tick, DONE/FAILED/missing lines, WIP commit, outcomes unit tests. Suite 222/222 green.

**Files:** `opl/conductor/spark/outcomes.py`, `opl/conductor/spark/runner.py`,
`tests/test_spark_runner.py`

**Produces:** `SparkRunner(settings, model, op_spark_client, gh, slots)` with
`tick(world)`.

**Starting runs:**
- Candidates are Ready tasks assigned to `spark` in **Public** projects, with
  no violation and no run already active.
- For each, as slots allow: move the task Ready → In progress **with Spark's
  own token**, then create the worktree, write the packet, and run the worker
  with the size limit.

**Outcomes:**
- **Success:** the worktree must be clean and have commits beyond the base
  (otherwise it counts as failed with reason `uncommitted work`). Then:
  - push the branch;
  - open a PR through the GitHub API;
  - set the PR link;
  - move the task In progress → In review with Spark's token.
- **Timeout or stall:** no retry. Keep the branch, move the task to Blocked,
  and comment `timeout|stalled after N min; options: more time, split,
  reassign`.
- **Failed:** retry once, with the error lines added to the packet. A second
  failure moves the task to Blocked with the error summary.

**Added by review of T4.1 (2026-09-24):**
- `remove_worktree` uses `--force`, which throws away uncommitted files. On timeout, stall or failure, first commit whatever is in the worktree (`git add -A && git commit -m "WIP: partial work from run <id>"`) so the branch keeps it, and only then remove. The DESIGN says partial work stays on the branch.
- The packet reads `why`, `what` and `done_when` as separate keys, but OpenProject stores them as one description following the §1 template. Parse the description's `Why:` / `What you'll see:` / `Done when:` sections into those keys, or pass the whole description when they're missing, with a test for both.

**Added by review of T4.2 (2026-09-24):**
- `run_worker` returns `success` whenever the process exits 0. A build run only counts as success with exit 0 **and** a final `OPL-RESULT: DONE` line. `OPL-RESULT: FAILED`, or a missing final line, counts as failed (the same rule T4.4 uses for review and test runs).
- `run_worker` opens its log with `wb`, which overwrites it. Give each attempt its own log path (e.g. `<run-id>-attempt<N>.log`) so the retry doesn't erase the first failure's evidence.

**Pushing:** push with the GitHub token passed only through an in-process git
config header (`-c http.https://github.com/.extraheader=...`). It is never in
argv logs or the remote URL. Tests push to a local bare repo with no token.

**Steps:**
- [ ] Tests covering:
  - the success path with a local bare remote plus FakeServer for GitHub and
    OpenProject;
  - uncommitted work being treated as failed;
  - a timeout giving Blocked with no second run;
  - failure then success on retry;
  - a Private project never being started, even when the Item says spark;
  - the slot cap being respected.
- [ ] Implement, run the full suite, commit.

### T4.4 · Review runs and test runs
- **Owner:** Spark · **Size:** M · **Risk:** Medium · **Depends:** T4.3
- **Status:** Done
- **Result:** Review fixes (this commit): (1) kind-specific `How to do it` packet steps — review says PR branch, `git diff origin/<base>...HEAD` vs Done when + task, run tests, no edits/commits, OPL-REVIEW; test says walk every Done-when item vs target, no code changes, per-item report, OPL-TEST; build keeps commit-as-you-go (Rules no longer tell reviewers to commit); (2) explicit Public check in `_review_candidates`; (3) `GIT_TERMINAL_PROMPT=0` + `GCM_INTERACTIVE=never` on every git subprocess (runner `_sh`, worktree `_git`/fetch/add/remove, outcomes WIP) via `worktree.git_env`, credential helper untouched; (4) push-leak test keeps the github.com URL but redirects it to a local rejecting bare repo via `GIT_CONFIG_GLOBAL` insteadOf + `pre-receive` exit 1 — no network, no sign-in window, leak assertions intact; (5) `_split_sections` captures to the next heading/EOF (multi-line bodies survive), new `tests/test_split_sections.py`. Suite 252 green.
- **Review:** (Claude, 2026-09-25, re-review) Approved (dbb39ec). Per-kind packet steps (review: PR branch, `git diff origin/<base>...HEAD`, run the tests, no edits or commits; test: every Done-when item, no code changes, per-item report); an explicit Public check for reviews; `git_env()` (GIT_TERMINAL_PROMPT=0, GCM_INTERACTIVE=never) on every git call in runner, worktree and outcomes; the push-leak test runs offline (insteadOf → a rejecting local bare repo, GIT_CONFIG_GLOBAL/NOSYSTEM isolation, and asserts nothing was pushed); `_split_sections` keeps multi-line sections (reviewer's test passes). Full suite: 252 tests OK.

**Files:** `opl/conductor/spark/runner.py`, `tests/test_spark_reviews.py`

**Review runs:**
- **When:** a task is In review with reviewer `spark` and no review_result.
- **Run:** a review packet checks out the PR branch in a fresh worktree,
  using the 15-minute limit.
- **PASS:** set review_result to Pass.
- **CHANGES:** set review_result to Changes requested, comment the notes, and
  move the task In review → In progress (Spark's token).

**Test runs:**
- **When:** a feature is In test in a Public project with no test_result.
- **Run:** a test packet with the project's `test_url` (or "build main
  locally" when `has_test_env` is false), using the 20-minute limit.
- **Result:** post the summary as a comment and set test_result to Pass or
  Fail.

**Steps:**
- [ ] Tests for each path with the fake workers printing the
  `OPL-REVIEW` / `OPL-TEST` lines.
- [ ] Test: a missing final line counts as failed.
- [ ] Implement, run the full suite, commit.

### T4.5 · Run records and tuning report
- **Owner:** Spark · **Size:** S · **Risk:** Low · **Depends:** T4.3
- **Status:** Done
- **Result:** `opl/conductor/spark/records.py`: `record_run` appends one JSON line per run (task, kind, size, started, ended, duration_s, outcome, cost_usd) to `<state_dir>/runs.jsonl`; `read_runs` skips malformed lines; `summarize` groups median/max duration per (kind, size) and counts timeouts/stalls; `format_report` renders the tuning text. Runner records every build attempt, review run and test run via `_record` (failure to record only warns, never breaks a run). `opl-conductor runs` prints the report (empty state says "no runs recorded yet"). Tests: roundtrip, required keys, missing file, malformed lines, median/max/timeouts, report text, both `runs` CLI paths. Suite 236 green.
- **Review:** (Claude, 2026-09-25) Approved. One runs.jsonl line per run, recording never breaks a run, and the `runs` report gives the median and max per kind and size plus timeouts.

**Files:** `opl/conductor/spark/records.py`, `tests/test_records.py`

**Behaviour:**
- Append one JSON line per run to `<state_dir>/runs.jsonl`, with task, kind,
  size, started, ended, duration_s, outcome and cost_usd.
- `bin/opl-conductor runs` prints the median and max duration per size and
  kind, and the count of timeouts.

**Steps:**
- [ ] Tests, implement, run the full suite, commit.

### T4.6 · Docs for configure and conductor
- **Owner:** Spark · **Size:** S · **Risk:** Low · **Depends:** T4.5
- **Status:** Done
- **Result:** New `docs/CONDUCTOR.md`: what the loop does, watch vs live (both the config flag and `--live` needed), state_dir table (watch.log, packets/, logs/, runs.jsonl, worktrees/), reading runs.jsonl via `opl-conductor runs`, safe stopping. README quick map gains a conductor row; RUNBOOK gains a 6-line Automation section linking here (64 lines total, one-page test green). Suite 236 green.
- **Review:** (Claude, 2026-09-25) Approved. CONDUCTOR.md covers watch vs live, the log locations, `runs.jsonl`, and a safe stop; its mention of fix runs is accurate now that T4.7 has landed.

**Files:** `README.md`, `docs/CONDUCTOR.md`

**Content:** what the conductor does; watch vs live mode; where the logs
live; how to read `runs.jsonl`; how to stop it safely. The runbook stays one
page and links here.

### T4.7 · Fix-after-review loop for Spark tasks
- **Owner:** Spark · **Size:** M · **Risk:** High · **Depends:** T4.4
- **Status:** Done
- **Result:** Fix candidates (Task + In progress + assignee spark + Review result Changes requested + Public + violation-free + PR URL + no active fix run) run on the existing PR branch via detached worktree, with a build packet plus the reviewer-notes section (latest non-spark journal comment, spark-comment fallback). Success (DONE + new commits) pushes `HEAD:<branch>` to update the PR, clears Review result (`option_href(None)` → `{"href": None}`) and moves to In review with a fresh lockVersion. Failure/timeout: WIP commit then Blocked with branch + error (single attempt, no retry). Two prior `review-changes` rows in runs.jsonl short-circuit the tick straight to Blocked with "review loop: 2 rounds" (no worker spawned). Tests: same-branch push (bare origin, no `opl/*` branch created), clear + move, notes in packet, missing-line Blocked, round-3 guard (no packets written), Private never picked. Best-guess live shapes flagged for T4.R/T1.L: journal author fields and the `{"href": None}` clear. Suite 241 green.
- **Review:** (Claude, 2026-09-25) Approved (065f17d). Fix candidates are In progress + Changes requested, Public only, not active; the existing PR branch is checked out and pushed back (`HEAD:<branch>`) with the token env; review_result is cleared (`href: null`) and the task moves to In review via a fresh lockVersion; the 2-round guard sends it to Blocked; a failed fix goes to Blocked with a comment. 5/5 tests pass with no network. Minor follow-up for later (not blocking): `_reviewer_notes` recognises spark's own comments by an author name/href containing "spark". Match the spark user's id from settings/users instead once T1.4's ids are available at runtime.

**Problem (found in review of T4.4):** a review that returns CHANGES moves
the task In review → In progress with `Review result = Changes requested`.
The build runner only starts **Ready** tasks, so a Spark task sent back by
review is never picked up again: it stalls silently, and no screen action
covers it.

**Behaviour:**
- A task that is In progress, assigned to spark, has `Review result =
  Changes requested`, sits in a Public project with no violation, and has no
  active run is a **fix candidate**.
- A fix run checks out the task's **existing PR branch** (not a fresh branch
  from base), with a packet containing the task, the feature, and the
  reviewer's notes: the latest comment on the task that isn't from spark.
- On success (DONE plus new commits): push the **same branch** (the PR
  updates), clear `Review result`, and move the task to In review with a
  fresh lockVersion.
- Failure and timeout follow T4.3's rules (WIP commit, then Blocked with a
  comment).
- After **two** review rounds that both returned CHANGES, don't start a
  third automatically: set Blocked with "review loop: 2 rounds", so it shows
  in Needs me.

**Tests:** the fix candidate is picked; the same branch is pushed; review_result is cleared; the task moves back to In review; the round-3 guard; a Private task is never picked.

### T4.R · Review phase 4
- **Owner:** Claude/Codex · **Size:** L · **Risk:** High · **Depends:** T4.1–T4.7
- **Status:** Done
- **Result:** —
- **Review:** (Claude, 2026-09-25) Phase 4 fully reviewed: T4.1–T4.7 are all approved (see each Review). What remains is live proof in T4.O: a real worker command, a real push through the owner's credential helper, and a real PR.

### T4.O · Owner: go live
- **Owner:** Owner · **Size:** M · **Risk:** High · **Depends:** T4.R, T3.W, TH.R, TH.14
- **Status:** Todo
- **Result:** —
- **Review:** —

**Steps:**
1. Set the real runner command in the external config.
2. Set `live = true`.
3. Run one small sandbox task in a Public project, with Claude watching.

---

## Phase 4b: Cost tracking

**Spec:** DESIGN.md §7.

### TC.1 · Spike: match usage sessions to task folders
- **Owner:** Spark · **Size:** M · **Risk:** Low · **Depends:** —
- **Status:** Done
- **Result:** `docs/USAGE-MAPPING.md`: ccusage pinned at 20.0.24 (`npx ccusage@20.0.24 <tool> session --json`, own prices ignored); Claude Code confirmed (`~/.claude/projects/<slug>/UUID.jsonl`, trust entry `cwd`); Codex confirmed (`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`, `session_meta.cwd`, skip archived; ccusage sessionId join needs live check); OpenCode marked unknown — needs live check (storage layout leads recorded, no official format docs found). Cache-token and test-fake rules noted for TC.3. `tests/test_usage_mapping.py` (5 tests). Suite 257 green.
- **Review:** (Claude, 2026-09-25) Approved (4d7985a). Synthetic samples only, a pinned ccusage version, honest unknowns (OpenCode, and the Codex id join). A Codex live check against the owner's real logs is queued (private; it reports structure only). One flaw is carried into TC.3: "cwd contains the task id" substring matching would give T5 the usage of T50/T51. See TC.3's matching rule.

**Why:** `ccusage <tool> session --json` gives per-session tokens (`sessionId`,
input/output/cache tokens, `modelBreakdowns`, first/last activity) for Claude
Code, Codex and OpenCode, but **no working folder**. Find, for each tool,
where its local session logs live and how to read a session's working folder
from them. Known leads: Claude Code stores logs under a folder derived from
the working directory; Codex stores `cwd` in its session metadata; OpenCode
is unknown.

**Output:** `docs/USAGE-MAPPING.md` with, per tool:
- the log location;
- how to get session id → folder;
- the exact pinned ccusage command that returns one session's tokens by model;
- a sample of its JSON (synthetic);
- whether cache tokens are reported.

Pin the ccusage version (npm package and version).

**Done when:** all three tools are covered, or a tool is marked
`unknown — needs live check` with the reason.

### TC.2 · Price table, estimate table, cost fields and columns
- **Owner:** Spark · **Size:** M · **Risk:** Low · **Depends:** T1.2, T1.5
- **Status:** Done
- **Result:** `config/prices.toml` (claude Sonnet 4.6 $3/$15/$0.30/$3.75 subscription; codex gpt-5.3-codex $1.75/$14/$0.175/$1.75 subscription; spark Muse Spark 1.3 contributor $0.10/$0.20/$0.002/$0.10 api — each with source-URL comment; writes at input rate where unpublished, noted), `config/estimates.toml` (per model × S/M/L starting guesses, identical across models until TC.4), new `opl/costs_config.py` (loaders + validation incl. every-user-covered and ≥0; `token_cost` over all four bands with billing display-only; `estimate_cost`). pm-model.toml: `Est. cost`/`Actual cost` (float) on Epic/Feature/Task, `Actual tokens` (int) on Task; cost columns on both Feature pipeline views and All projects. Tests: loaders/validation/math/subscription-parity + shipped counts (fields 14) + cost field/view presence; fake WP schema extended. Judgement calls: view "sums" are OpenProject UI column totals for number columns, no API key invented; estimates identical across models pending TC.4. Suite 269 green.
- **Review:** (Claude, 2026-09-25, re-review) Approved (b2cb1b2) with a reviewer follow-up: `[[model]]` globs with cited prices (Sonnet, Opus, Codex, Spark) and `[default]` login → entry used for estimates only. The reviewer loosened the shipped globs (`*sonnet*`, `*opus*`, `*codex*`, plus a `gpt-5*` Codex fallback) because ccusage's model-name form (full vs short) is only confirmed by the TC.1 live check; unmatched models still leave actuals empty.
- **Review:** (Claude, 2026-09-25) **Changes requested.** The sources, dates and billing flags are right, and so are the fields and columns. One structural fix:
  **Price by the model actually used, not by login.** One login runs different models: `claude` may be Sonnet or Opus, and Opus costs a multiple of the Sonnet rate priced here, so actuals for the lead would be understated. ccusage's `modelBreakdowns` name the model per session, so use them. Restructure `config/prices.toml`:
  - `[[model]]` entries: `match` (a glob on ccusage's model name, e.g. `claude-opus-*`, `claude-sonnet-*`, `gpt-5*-codex*`, `*spark*`), with `input`, `output`, `cache_read`, `cache_write`, `billing`, `as_of` and a source comment. Add at least the Claude Opus and Sonnet families, Codex, and Spark, each with its published list price.
  - `[default]`: login → which `match` entry to use for **estimates** (the planner doesn't know the exact model), e.g. `claude = "claude-sonnet-*"`.
  - **Actual cost** = Σ over a session's `modelBreakdowns` of that model's tokens × the price of the first matching entry. If any breakdown's model matches no entry, leave the task's actual **empty** and log the model name. Never guess.
  - The loader validates the defaults (they point at existing entries) and non-negative prices. Tests: an Opus and a Sonnet breakdown in one session get different rates; an unknown model leaves the actual empty; the estimate uses the default entry.
  TC.3 consumes this, so fix it before continuing TC.3.

**Files:** `config/prices.toml`, `config/estimates.toml`,
`config/pm-model.toml`, `opl/model.py` or a new `opl/costs_config.py`, tests.

**`config/prices.toml`:**
- One table per model login: `input`, `output`, `cache_read`, `cache_write`
  (USD per 1M tokens), `billing` = `"api"` or `"subscription"`, and `as_of`.
- Use each model's current public API list prices, citing the source URL in a
  comment. For Spark, use the contributor-rate route's published prices.
- Validation: every model user has prices; all prices are ≥ 0.

**`config/estimates.toml`:** per model × size (S/M/L), the expected
`input_tokens` and `output_tokens` (starting guesses, tuned by TC.4).

**Fields** (float, USD, conductor-maintained):
- Task: `Est. cost`, `Actual cost`, `Actual tokens` (integer)
- Feature and Epic: `Est. cost`, `Actual cost`

Add the cost columns, with sums, to the Feature pipeline and All projects
views.

**Tests:** loaders, validation, model counts updated.

### TC.3 · Conductor costs job
- **Owner:** Spark · **Size:** L · **Risk:** Medium · **Depends:** TC.1, TC.2, T3.8, T4.1
- **Status:** Done
- **Result:** `opl/usage.py`: `parse_sessions` (both ccusage JSON shapes), per-tool `session_folders` (claude cwd / codex session_meta; opencode `{}` until its live check), `match_tasks` (bounded task-id match, no T55-for-T5), `price_sessions` at the task assignee's table, `run_ccusage` with timeout→UsageError, `collect_actuals` across tools. `opl/conductor/rules/costs.py`: task estimates (assignee+size known, unknown skipped), actuals in In progress/In review/Merged only with unknown staying empty, bottom-up feature/epic roll-ups in one pass (fresh values propagate to epics immediately), actuals=None still estimates. Engine: `run_once(..., costs=[])`, `_CUSTOM` cost names, `_apply_item_group` writes floats/ints via custom_prop (comments only ever come from status changes, so screen fields stay silent). `_cycle` loads prices/estimates per loop and degrades (log + skip actuals/costs, loop continues). Item gains est/actual fields; collect populates them. Tests: fake ccusage JSON + fake log homes, subscription-at-API-rates parity, roll-ups, empty-unknown, drop_noops idempotency, engine composition (enforce+costs coexist), ccusage failure paths. Suite 296 green. Review fixes this commit: (1) exact folder matching via task_for_folder, documented in DESIGN §7 and USAGE-MAPPING.md (worktree labels already unified, verified); (2) per-model breakdown pricing with unmatched left empty + logged; (3) feature-level test-run cost included in feature/epic roll-ups. Suite 317 green.
- **Review:** (Claude, 2026-09-25, re-review) Approved (9a6f269). `task_for_folder` uses delimited prefixes on the last two segments (`task-T<id>-`, `fix-`, `review-` → task; `test-` → feature), so T5≠T50 and dated folders match nothing; sessions are priced per `modelBreakdowns` entry at the matching model rate (unknown → actual empty and logged); test cost is booked at feature level and rolled up. Full suite: 317 tests OK.

**Files:** `opl/conductor/rules/costs.py`, `opl/usage.py` (ccusage + log
mapping per TC.1), `tests/test_rule_costs.py`, `tests/test_usage.py`.

**Behaviour:**
- **Task estimate:** tokens from `estimates.toml` for (assignee, size) ×
  `prices.toml`, set whenever Size or Assignee changes.
- **Task actual:** find the sessions whose folder name contains the task id;
  sum their tokens per model × our prices; write `Actual tokens` and
  `Actual cost`. Recompute every loop while the task is In progress or In
  review, and once more after Merged.
- **Roll-ups:** feature = sum of its tasks; epic = sum of its features.
  Actuals sum known values only; unknown stays empty.
- Costs are **screen fields**: no comments. Idempotent.
- ccusage runs with a timeout. On failure, log and skip that loop's actuals
  without failing the whole loop.
- **Matching rule (added by review of TC.1, 2026-09-25).** Never use
  substring matching: `T5` must not match `T50`. Match a session's `cwd` with
  a delimited pattern on its last path segments:
  - `task-T<id>-`, `fix-<id>-` and `review-<id>-` count toward **task** `<id>`
    (build, fix and review cost all belong to the task);
  - `test-<id>-` counts toward **feature** `<id>` (the tester works at
    feature level).

  Unify the worktree labels so each kind uses one documented prefix. Put
  the rule in one function (`opl/usage.py: task_for_folder(path) ->
  (kind, id) | None`), test it against `T5` vs `T50`, and document it in
  DESIGN §7 for sessions the owner starts by hand (folder `task-T<id>-…`).
- **Feature and epic actuals** also include test-run cost booked at feature
  level, plus the sum of their tasks.

**Tests:**
- fake ccusage JSON and fake log folders map to the right task;
- a subscription model is priced at API rates;
- the roll-up sums;
- unknown actual stays empty;
- running twice changes nothing;
- a ccusage failure leaves the other rules running.

### TC.4 · Estimate calibration report
- **Owner:** Spark · **Size:** S · **Risk:** Low · **Depends:** TC.3
- **Status:** Done
- **Result:** New `opl/calibration.py`: `calibrate` groups finished tasks' Actual tokens by (assignee, size) and reports estimate vs median vs p90 (nearest-rank) with raise/lower/ok suggestions; `format_calibration` renders it. `bin/opl-conductor estimates` (via `opl.conductor estimates`) collects the world and prints the report; it never edits estimates.toml (covered by a file-unchanged test). Tasks without actuals or unknown model/size are skipped. Tests: median/p90 math, all three suggestions, empty/unknown handling, report text, never-edits. Suite 306 green.
- **Review:** (Claude, 2026-09-25) Approved (3b6fcbf). Per (login, size): the estimate vs the median and p90 of actual tokens, with a direction suggestion; it never edits files, and tests are included. It suggests a direction rather than numbers, which is acceptable; add numbers later if the owner wants them.

`bin/opl-conductor estimates` prints, per model × size, the median and
90th-percentile actual tokens against the current `estimates.toml` values,
and suggests new values. It never edits the file itself.

### TC.R · Review cost tracking
- **Owner:** Claude/Codex · **Size:** M · **Risk:** Medium · **Depends:** TC.1–TC.4
- **Status:** Done
- **Result:** —
- **Review:** (Claude, 2026-09-25) Cost tracking reviewed task by task: TC.1–TC.4 are all approved. Live proof remains: the Codex live check of TC.1 against real logs (private, structure only), then real actuals appearing in the owner's screens after T4.O.

---

### TM.1 · Independent pre-merge review of the whole branch
- **Owner:** Codex · **Size:** L · **Risk:** High · **Depends:** T4.R
- **Status:** Done
- **Result:** (Codex, 2026-09-25) Private report written: blockers found (R1, R6, R8) plus 11 more; no merge approval. Turned into Phase 8 (TH.*).
- **Review:** —

A second model reviews the entire branch diff against `main` before it's
pushed to the public remote (Claude reviewed task by task). **Read-only:
no edits or commits** on the build branch. Focus, in order:
1. secrets (token paths, logs, errors, argv, test fixtures);
2. anything that could delete or overwrite live data (backup, restore,
   worktrees, compose);
3. the Private-project/Spark boundary;
4. OpenProject/GitHub API shapes likely to fail live;
5. anything in committed files that shouldn't be public.

Write the findings to the private review folder with the PM tooling, each
with file:line, severity and a concrete fix. Claude turns them into tasks.

---

## Phase 8: Pre-merge hardening

**Why:** the independent pre-merge review (TM.1, Codex) and the private live
check of TC.1 against real session logs found problems that fake-server tests
couldn't show. **Nothing is pushed or run live until this phase is done and
TH.R passes.** R-numbers refer to the private TM.1 report. Its findings are
restated here in public-safe form; no private values.

### TH.1 · Worker environment isolation
- **Owner:** Claude · **Size:** M · **Risk:** High · **Depends:** —
- **Status:** Awaiting review
- **Result:** (Claude, 2026-09-25) `supervisor.worker_environment(allow, home)` builds the worker's whole env explicitly: OS basics + `[runner] worker_env` names + HOME/USERPROFILE/APPDATA/LOCALAPPDATA/XDG_*/TEMP pointed into a fresh `<state_dir>/homes/<run>`. `run_worker(env=…)` defaults to an isolated env if a caller forgets. The settings loader refuses any other role's token variable in `worker_env`. The runner passes it for build/fix/review/test runs. Tests: 4 supervisor canaries (secret absent, owner HOME canary unreachable, allowlisted key passed, default isolated), 3 settings guards, 1 runner end-to-end. Full suite 325 OK. Reviewer: Codex (TH.R).
- **Review:** —

**(R1, blocker)** `run_worker` starts the worker with the conductor's whole
environment, so admin, GitHub and other model tokens are inherited by the
Contributor-tier worker.

**Fix:**
- Build an explicit **allowlist** environment for every worker process:
  `PATH`, `SYSTEMROOT`/`TEMP`/`TMP` on Windows, `LANG`, the worker's own API
  key variable (named in config `[runner] worker_env = [...]`), and `HOME`
  / `USERPROFILE` pointing to a **fresh per-run tool home** under
  `<state_dir>/homes/<run>` (so no owner tool configs, credentials or
  histories are visible through HOME).
- Nothing else is passed.

**Tests (canary):**
- a synthetic `OPL_CANARY_SECRET` in the conductor env is absent in the
  worker;
- a canary file in the real HOME is not reachable via the worker's HOME;
- the worker's API-key variable is present.

OS-level isolation (a separate low-privilege user) is TH.14.

### TH.2 · Redact every output sink
- **Owner:** Claude · **Size:** M · **Risk:** High · **Depends:** TH.1
- **Status:** Awaiting review
- **Result:** (Claude, 2026-09-25) New `opl/redaction.scrub(text, secrets)` masks each known value in plain and base64 forms (plain, `x-access-token:`, `apikey:`) plus any Authorization header. `settings.redact` uses it; both API clients scrub error bodies with their own token; the runner scrubs every tracker comment and every packet right before writing (retry errors and reviewer notes included); raw worker logs are chmod 600. Removed the unused `_packet` helper (an unscrubbed prompt path). Tests: scrubber unit tests, reflected-auth error bodies for both clients, and a runner test with a worker leaking a secret plainly and base64 across two failed attempts. Full suite 333 OK. Reviewer: Codex (TH.R).
- **Review:** —

**(R2)** Worker stdout/stderr tails flow into run logs, tracker comments and
the next retry prompt. OpenProject/GitHub client errors include raw response
bodies.

**Fix:** one `redact_all(text, settings)` applied before
- every tracker comment;
- every packet (including "Previous attempt failed");
- every `ApiError` or `RuntimeError` message;
- any log line derived from worker output.

It masks every known secret value, its base64 forms (plain and
`x-access-token:`), and `Authorization:` headers. Raw worker logs stay only
in `<state_dir>/logs` with mode 600.

**Tests:** a fake worker that echoes a synthetic token (plain and base64),
and a fake server that reflects the Authorization header in its error body.
Neither appears in comments, packets or exceptions.

### TH.3 · No secrets in argv; collision-proof backups
- **Owner:** Claude · **Size:** M · **Risk:** High · **Depends:** —
- **Status:** Awaiting review
- **Result:** (Claude, 2026-09-25) `urlencode` reads the value over stdin (builtin printf pipe), so no password is in any argv. `opl-backup` stages in `mktemp -d .partial-backup-<ts>-XXXXXX` (mode 700), verifies its own SHA256SUMS, then publishes with one atomic `mv` to `backup-<ts>-<id>` (refusing an existing target). Retention takes a mkdir lock, only prunes dirs whose manifest says tool opl-backup and this project, and skips symlinks. Tests: an argv-recording python3 shim, two runs with a frozen `date` produce two complete backups, another project's manifest is kept, and a failure leaves no `.partial`. (Symlink pruning isn't tested: symlinks need privileges on Windows dev boxes.) Full suite 335 OK. Reviewer: Codex (TH.R).
- **Review:** —

**(R3)** `urlencode` passes the database password as a Python argv
argument. Read it from stdin instead.

**(R4)** Backup folders use second resolution plus `mkdir -p`, and
`>database.sql` truncates, so two runs in the same second can corrupt a
finished backup.

**Fix:**
- create the backup folder exclusively (fail if it exists; add a random
  suffix);
- set mode 700;
- write into `<dir>.partial`, validate the payload and checksums, then
  rename atomically;
- retention takes a lock, only prunes folders whose manifest has
  `"tool": "opl-backup"` and the same project, and never follows symlinks.

**Tests:**
- a synthetic password never appears in any process argv (fake python
  records argv);
- two backups in the same second don't collide;
- a failed run leaves no `.partial` behind;
- a foreign-manifest folder and a symlink are never pruned.

### TH.4 · Keep failed or dirty review/test worktrees
- **Owner:** Claude · **Size:** S · **Risk:** Medium · **Depends:** —
- **Status:** Awaiting review
- **Result:** (Claude, 2026-09-25) New `worktree.dispose_worktree(wt, state_dir, succeeded)`: refuses any path not under `<state_dir>/worktrees`, keeps the tree on failure or on any tracked/untracked change, and removes it otherwise. `remove_worktree` no longer uses `--force`, so git itself refuses a dirty tree. Review and test runs dispose in `finally` via `_dispose` (a crash keeps the tree) and the failure comment names the kept path. Tests: 4 disposal cases (failed kept, dirty kept, clean removed, outside refused) plus runner-level failed-review-keeps and clean-pass-removes. Full suite 341 OK. Reviewer: Codex (TH.R).
- **Review:** —

**(R5)** Review and test cleanup always runs `git worktree remove --force`,
which throws away evidence when a worker failed or edited files.

**Fix:** remove without `--force`, and only when the run succeeded **and**
the tree is clean. Otherwise keep it and name its location in the (redacted)
comment. Validate that the path is under `<state_dir>/worktrees` before any
removal.

**Tests:** failed → kept; dirty → kept; clean success → removed; a path
outside the state dir → refused.

### TH.5 · Verified authority for Merge OK and Review result
- **Owner:** Claude · **Size:** L · **Risk:** High · **Depends:** TH.7
- **Status:** In progress
- **Result:** (Claude, 2026-09-25) Part 1 done (`ai/hardening-claude` d2a2ea3). The collector reads each task's journal. Merge OK counts only if its latest change is by an Owner-role member of the project, taken from memberships, so no admin token is needed. Review result counts only if its latest change is by the task's Reviewer. It also records the Reviewer's latest `reviewed: <40-hex sha>` comment. Enforce blocks unattributed approvals ("Unauthorised approval"), and merge re-checks both. An unreadable journal on an approved task, or unreadable memberships, skips the cycle. Models lost Blocked → Ready. Part 2 (after TH.8, now landed): merge only while the PR head equals the reviewed SHA; pass it to GitHub's merge; a new push clears Review result; Spark review runs post the SHA.
- **Review:** —

**(R6, blocker)** Merge OK and Review result are plain fields any model with
edit rights can set, so a builder could approve its own work. Approval also
isn't tied to the exact code reviewed.

**Fix:**
- Before merging, the conductor reads the work package's **activity
  journal**:
  - the most recent change that set `Merge OK = true` must be by the
    **owner** user;
  - the most recent change that set `Review result = Pass` must be by the
    **task's Reviewer** user. For Spark-run reviews, that's the spark user
    via the runner.
  - Anyone else → violation (Blocked, "unauthorised approval") and no
    merge.
- **SHA binding:** every review posts `reviewed: <PR head sha>`; review runs
  record it too. Merge only if the PR head **equals** the reviewed SHA, and
  pass that SHA to GitHub's merge (`sha` parameter). A new push clears
  Review result (the screens rule re-requests review).
- Model role workflows: remove `Blocked → Ready`. Models may only go
  `Blocked → In progress`, and **Ready** is only ever set by the conductor
  (see also TH.6).

**Adversarial tests:**
- the builder sets Merge OK → no merge, Blocked;
- the builder sets Review result = Pass → no merge;
- the PR head moves after review → no merge, review re-requested;
- the owner sets Merge OK plus a matching SHA → merge.

### TH.6 · Revalidate before every dispatch
- **Owner:** Claude · **Size:** M · **Risk:** High · **Depends:** —
- **Status:** Awaiting review
- **Result:** (Claude, 2026-09-25) `SparkRunner._dispatch_problem` runs immediately before every build/fix/review/test run and checks: Public project; local repo configured; the checkout's raw `remote.origin.url` slug equals the configured repo (insteadOf not expanded, so the configured identity is what's checked); the parent is a Feature in the same project in Approved/Building (test runs: the feature's parent is an Epic in the same project); predecessors Merged (build). A task that fails moves to Blocked with a "Not started (<kind> run): <reason>" comment, and no worker starts. Tests: manual Ready under Proposed, cross-project parent, unmerged predecessor, foreign origin. Harnesses now use a github.com origin redirected to a local bare repo (`tests/gitfixture.py`), and the sample worlds use real statuses. Full suite 345 OK. Reviewer: Codex (TH.R).
- **Review:** —

**(R7)** The runner trusts status Ready, and a model can set Ready by hand.

**Fix:** immediately before starting any build, fix, review or test run,
re-check:
- the parent is a Feature in **Approved/Building** in the **same project**;
- every predecessor is **Merged**;
- the project is **Public**;
- the checkout's `origin` URL matches the configured `repo`.

Any failure → no run, and Blocked with the reason.

**Tests:**
- a manually set Ready under a Proposed feature;
- a parent in another project;
- an unmerged predecessor;
- a mismatched remote.

### TH.7 · Real OpenProject API shapes and pagination
- **Owner:** Claude · **Size:** L · **Risk:** High · **Depends:** —
- **Status:** Awaiting review
- **Result:** (Claude, 2026-09-25, `ai/hardening-claude` d36118c) New `opl/hal.py` is the one decoder for v3 HAL: schema fields at the root (no `properties`), list/user values from `_links` (`title`/`href`, multi-select lists), scalars and formattables from the root, allowed options (embedded or linked), null links, and journal details parsed from formattable text ("Status changed from A to B"). The collector uses per-project/type schemas; pages work packages (sorted by id, with an explicit filter so closed items are listed too); treats parent `href: null` as no parent; resolves users by id with admin, otherwise by model account name (the conductor token gets 403 on `/users`); and takes status_since from the journal, then createdAt. Any listing or paging error propagates, and the conductor logs "read failed, skipping cycle". The engine lookups, views, project fields and permcheck read the schema root. Project Visibility (a list field) is read and written as an option link; OpenProject's source confirms that `link`-format fields such as Repo and PR link are plain root strings. Fixtures were rebuilt on the documented shapes. Full suite 367 OK. Reviewer: Codex (TH.R).
- **Review:** —

**(R8 blocker, R9)** The collector, views and configure code call
`/api/v3/work_packages/schema` (it doesn't exist), expect a `properties`
wrapper (HAL schemas put field definitions at the schema root), and read
list/user custom fields as plain strings (they're `_links.customFieldN`
with `href`/`title`). Several listings use `get` instead of `get_all`, so
anything past page 1 disappears.

**Fix:**
- Use the per-project/type schema, `/api/v3/work_packages/schemas/{project}-{type}`,
  reading field definitions from the schema root.
- Decode scalar custom fields from the resource, and list, user and link
  fields from `_links` (`title` for display, `href` for identity).
- Use `get_all` on every collection. Treat `parent: {href: null}` as no
  parent.
- **Fail closed:** if a snapshot is incomplete (paging error), skip the
  cycle.
- Build the fixtures from OpenProject's documented v17 HAL shapes now; TH.7a
  later swaps in sanitised live samples and confirms nothing differs.

**Tests:**
- a parent and an unfinished child on different pages;
- a list/user field read via `_links`;
- a null parent;
- a paging failure skips the cycle.

### TH.7a · Sanitised v17 API samples from the live instance
- **Owner:** Claude/Codex · **Size:** S · **Risk:** Low · **Depends:** T1.L
- **Status:** Todo
- **Result:** —
- **Review:** —

From the owner's running OpenProject, after `opl-configure` has run, capture
**structure-faithful** samples of these responses (read-only):
- a work package with custom fields, including list, user, bool, link,
  float and int;
- the per-project/type schema;
- a paged collection;
- the activities/journal of an item with custom-field changes;
- the statuses, types and project status.

Replace every value (names, ids, text, dates, emails) with synthetic ones,
keeping the keys, nesting, `_links` and `_embedded` shapes exactly. Put
them in `tests/fixtures/openproject/` with a README saying they are
sanitised.

### TH.8 · GitHub checks, merge SHA and exact review checkout
- **Owner:** Spark · **Size:** M · **Risk:** High · **Depends:** —
- **Status:** Awaiting review
- **Result:** Checks: required contexts from branch protection when readable, else all check runs (paginated); legacy statuses count only when present — zero statuses is not pending, nothing at all is not green; 404 on status/check-runs endpoints means absent (fail closed). `merge(url, sha=...)` sends the expected head SHA (omitted when None; 422 surfaces as ApiError). `PullRequest` keeps `head_sha`/`head_repo` (filled by `pull_request()`, so collect needs no change — engine/collect/configure untouched). Review runs fetch the exact head SHA from the head repo into an isolated `refs/opl/…` ref via `worktree.checkout_pr_head` (SHA-shaped, abort on fetch failure, checkout verified equal); outcome records carry head SHA/repo for TH.5. Tests: Actions-only green, failing required check, noisy extras pass, zero-statuses-not-pending, nothing-not-green, pagination across pages, merge-sha sent/omitted/moved-head-422, head record incl. fork, exact-SHA checkout equality, fetch-failure abort (no fallback tree/packet), non-SHA refused. Suite 376 green.
- **Review:** (Claude, 2026-09-25) Approved (6eaf9d4). The review checkout is the exact PR head SHA from the head repo, in an isolated ref; fetch failure aborts and the checkout is verified. Checks are required contexts when readable, else every check run (the API default `filter=latest` handles re-runs); zero statuses is not pending, and nothing at all is not green. `merge(sha=)` is in place for TH.5. Follow-ups, Claude at merge: delete the `refs/opl/review-*` ref when the worktree is disposed (today they accumulate and pin objects); document for the owner that a repo with no CI at all never goes green, so nothing merges there until a check exists.

**(R10, R11)**

**Checks:**
- Green means every **required** check passes. Read the branch's required
  checks when available; otherwise all check runs, paginated.
- Legacy commit statuses count only if present: zero statuses ≠ pending.
- Actions-only repos must be able to go green.

**Merge:** `GitHub.merge(url, sha=...)` sends the expected head `sha` (the client side only; binding it to the reviewed SHA is TH.5, Claude).

**Review checkout:**
- fetch the PR's **exact head SHA** from the PR's head repository into an
  isolated ref, and abort on fetch failure (no fallback to a stale local
  branch);
- verify the checked-out SHA equals the PR head;
- keep the PR head SHA and head repo on the PR record.

**Tests:**
- an Actions-only green PR;
- a failing required check;
- a head that moved between check and merge;
- a failed fetch aborts.

### TH.9 · Wire the runner into the conductor, least privilege
- **Owner:** Claude · **Size:** M · **Risk:** High · **Depends:** TH.1, TH.5, TH.6, TH.7
- **Status:** Awaiting review
- **Result:** (Claude, 2026-09-25, `ai/hardening-claude` 2ab9b07) `opl-conductor` uses the conductor token for everything, including `estimates`; the admin token is never loaded. A persistent SparkRunner (spark token, Slots) exists only in doubly opted-in live mode and is ticked once per cycle. `--once` drains the runs it started and concludes them before exiting. Single instance: an OS file lock on `<state_dir>/conductor.lock` (msvcrt/flock), so a second conductor is refused and a crash can't leave a stale lock. Clean shutdown (finally, Ctrl+C, SIGTERM) runs `supervisor.terminate_all()`, which kills every tracked worker's process tree. Tests pass on Windows and WSL: watch never builds a runner; live ticks, drains and shuts down; the admin token is never read (every request carries the conductor token); a second instance is refused; a hanging worker is stopped. Full suite 388 OK. Reviewer: Codex (TH.R).
- **Review:** —

**(R12)** `opl-conductor` never creates or ticks a SparkRunner, and it
mutates OpenProject with the **admin** token.

**Fix:**
- One persistent `SparkRunner` + `Slots`, created **only** in doubly
  opted-in live mode, ticked each cycle.
- Mutations use the **conductor** token. The admin token is never loaded by
  the conductor.
- A single-instance lock file in the state dir; a second conductor refuses
  to start.
- Clean shutdown terminates owned worker process trees.

**Tests:**
- watch mode never starts a worker;
- live mode ticks the runner;
- a second instance is refused;
- shutdown kills a fake hanging worker;
- the admin token isn't read.

### TH.10 · Attempt ceilings and real progress detection
- **Owner:** Spark · **Size:** S · **Risk:** Medium · **Depends:** —
- **Status:** Awaiting review
- **Result:** Attempts persist per (kind, id) in `<state_dir>/attempts.json` (read fresh every tick; corrupt reads as empty, writes never break the loop): one retry for review/test, then the tick Blocks with "needs lead" instead of starting another run — tasks move to Blocked, features (no Blocked status) get the comment plus a warning action. Counts bump only on concluded review/test failures. Stall detection no longer counts log growth as progress — only worktree mtime changes (files and commits, `.git` included); output-spam workers now stall. Tests: ceiling after exactly 2 failures + restart reads the same file (no third run), corrupt file doesn't stop runs, feature ceiling comments without a move, spam-only worker stalls. Suite 390 green.
- **Review:** (Claude, 2026-09-25) Approved with follow-ups (c4010c1), which are TH.16. The attempt count never resets, so a task that failed once in one round is one failure from a permanent block in every later round. A feature at the ceiling is re-commented every tick, i.e. every minute. Owner note: stall now means no worktree file changes; if Spark legitimately reads for longer than `stall`, raise that limit.

**(R13)** Failed review, test and fix jobs stay eligible every tick with no
persisted ceiling, and growing log output counts as progress, so an error
loop defeats the stall stop.

**Fix:**
- (No dependency on TH.9: this lives in the runner and supervisor.)
- Persist attempts per (kind, id) in `<state_dir>/attempts.json`. After the
  ceiling (review 1 retry, test 1 retry, fix per TH.5/T4.7 rules), set
  Blocked with "needs lead". This survives restarts.
- Stall detection counts **worktree file changes and commits** as progress;
  log growth alone does not.

**Tests:**
- the ceiling survives a restart;
- a worker that only spams output is stalled.

### TH.11 · Separate start ref from PR base; idempotent PRs
- **Owner:** Spark · **Size:** S · **Risk:** Medium · **Depends:** —
- **Status:** Awaiting review
- **Result:** `base_ref` stays the local start ref; new `pr_base` setting (default `main`) is the GitHub PR target. `settings.valid_pr_base` + load-time validation refuse remote-tracking refs (`origin/main`) and ref junk while accepting plain branches (`main`, `release/1.x`). `_dispatch_problem` re-checks both before every run: pr_base must be a plain branch whose first segment is not a local remote name, and the start ref must `rev-parse --verify` in the checkout — otherwise Blocked with the reason and no worker. `_conclude_success` reuses an open PR from the same head (`GitHub.find_open_pr`, API failure = none found) and only creates with `pr_base` otherwise. Tests: validation matrix, settings default/accept/reject, bad start ref blocked with no packets, bypassed `origin/main` blocked with no PR POST, re-run reuses the open PR (no POST, link points at it). Suite 357 green.
- **Review:** (Claude, 2026-09-25) Approved (ad57c3f). The start ref and PR base are separate, pr_base is validated at load and again before every run, the start ref must resolve locally, and an open PR from the same head is reused. Follow-ups Claude applies at merge (small, in files Spark is editing for TH.8): reuse a PR only when its `base.ref` equals `pr_base`, and send/assert the `head=owner:branch` query in the test; document `pr_base` next to `base_ref` in `config/opl.example.toml`; drop the duplicated `permcheck = data.get(...)` line in `opl/settings.py`. A failed lookup falling through to create is acceptable, because GitHub refuses a second open PR for the same head and base (422).

**(R14)** A local start ref like `origin/main` is sent as GitHub's PR base.

**Fix:** configure `start_ref` (local) and `pr_base` (a GitHub branch name)
separately and validate both. PR creation first looks for an open PR from
the same head and reuses it.

**Tests:** `origin/main` vs `main`; a re-run after a partial success reuses
the PR.

### TH.12 · Cost tracking against real log shapes
- **Owner:** Spark · **Size:** L · **Risk:** High · **Depends:** TC.3
- **Status:** Awaiting review
- **Result:** `opl/usage.py` rebuilt on the pinned shapes: per-tool parsers (Claude/OpenCode `modelBreakdowns[]` with `modelName`+`cost`; Codex `models` object with session/legacy totals plus `reasoningOutputTokens`); Codex join normalises `sessionId`/`sessionFile` to transcript basename/stem/UUID and reads `session_meta.cwd` (report `directory` never trusted; nested + archived transcripts included, deduped by stable identity); Claude join by filename stem + file `cwd` (never the slug); OpenCode read-only SQLite adapter (`mode=ro`, `query_only`) on `opencode.db`'s `session` table only (never auth files); runs.jsonl manifest gains `worktree`+`commit` (runner captures HEAD while the tree exists) with `match_manifest` (registered worktree only, multi-task stays unknown); per-breakdown pricing with proportional session-cache split, reasoning at output rate, unknown/no-breakdown left empty + logged (never zero); matched/unmatched + ccusage version logged per loop. Wiring `state_dir` into `_cycle` left for TH.9 (Claude owns `__main__`). `USAGE-MAPPING.md` corrected (structure only); TC.1 doc-guard updated to the corrected structure. Tests: all seven fixture shapes (aux Claude id, nested Codex + archive duplicate, SQLite message/part duplication, missing cwd, unknown session) + manifest + collect end-to-end with fake command. Suite 386 green.
- **Review:** (Claude, 2026-09-25) Approved with one should-fix (e7276d3). The per-tool parsers match the pinned shapes. Joins use transcript identity, never `directory` or the slug. SQLite is read-only and touches only `session`. Unknown models are left empty, never zero; coverage is logged. **Should-fix, Claude at merge:** once any run is in `runs.jsonl`, Claude/OpenCode sessions match *only* registered worktrees. That drops the owner-started Claude/Codex task sessions that DESIGN §7 attributes by `task-T<id>-` folder names, and Codex rows still use the prefix rule, so the tools disagree. The fix is one rule for all tools: a registered worktree wins (unique task), else the delimited prefix rule, else unknown. Also: `run_ccusage` passes the conductor's whole environment (tokens) to `npx ccusage`; strip the token variables. Nit: build the SQLite URI with `pathname2url` so paths with `?`, `#` or `%` work. `state_dir` wiring into `_cycle` is done at merge (TH.9 owns `__main__`).

**(Private TC.1 live check.)** TC.3 was built on a synthetic universal shape
that doesn't match the real pinned ccusage 20.0.24 output. Fix, using
**synthetic fixtures only**:

1. **Per-tool parsers:**
   - Claude and OpenCode reports use `modelBreakdowns[]` with
     **`modelName`** and **`cost`**;
   - Codex reports use a **`models` object** keyed by model name (metrics
     `inputTokens`, `outputTokens`, `cacheReadTokens`,
     `cacheCreationTokens`, `reasoningOutputTokens`, `totalTokens`,
     `isFallback`) plus `sessionFile`.
2. **Codex join:** normalise the report `sessionId`/`sessionFile` to the
   transcript file (basename/stem, UUID suffix), then read `cwd` from that
   file's `session_meta` record. **Never** use the report's `directory`
   field. Include archived transcripts, deduplicated by stable identity
   (don't drop them).
3. **Claude join:** use the transcript file identity (report id =
   filename stem) and that file's `cwd`, not the lossy folder slug.
4. **OpenCode:** a read-only SQLite adapter (`mode=ro`, `query_only`) on
   the OpenCode `opencode.db` `session` table (`id → directory`). Unmatched
   report rows stay **unknown**. Never read auth files.
5. **Run manifest:** the runner records `run → task → worktree →
   start/end → commit` in `<state_dir>/runs.jsonl`. Attribute a session to
   a task only by its working folder matching a registered worktree. If a
   session spans several tasks, it's ambiguous and stays unknown.
6. **No double counting:** don't sum cumulative snapshots or several
   storage layers. Keep reasoning and cache counters as reported. An
   unknown model's cost stays unknown, not zero.
7. **Coverage:** record matched/unmatched row counts and the source
   version per loop in the conductor log.

**Tests:** synthetic fixtures for each shape above, including an auxiliary
Claude transcript id, nested Codex paths plus an archive duplicate, a SQLite
session/message/part duplication, a missing cwd, and an unknown report
session.

Correct `docs/USAGE-MAPPING.md` to match (structure only).

### TH.13 · Publication hygiene
- **Owner:** Spark · **Size:** S · **Risk:** Low · **Depends:** —
- **Status:** Awaiting review
- **Result:** Workflow template path now references the private pm-tools repo generically; rescan of all 127 tracked files is clean (drive-letter paths, Unix home dirs); golden file ends with a single newline and its test normalises exactly that (renderer untouched — Claude's area); CONFIGURE.md warns instance-wide/dedicated/backup. New `tests/test_publication.py` (patterns built dynamically so it stays clean itself). Judgement call: TH.13 task text reworded to name the path classes without literals so the scan stays green. Suite 361 green.
- **Review:** (Claude, 2026-09-25) Approved (2ad6615). The workflow comment is generic; CONFIGURE.md warns instance-wide, dedicated only, back up first; the golden file ends with one newline; and the scan test is clean. I also scanned all tracked files for private project names and machine paths: none found. Follow-up (Claude, admin_ruby.py): stop the renderer emitting the extra trailing blank line, so the test's normalisation becomes a no-op.

- Remove the private machine path from `.github/workflows/project-sync.yml`
  (reference the board generically).
- Scan every tracked file for other machine paths (Windows drive-letter
  paths and Unix home directories) and remove them.
- Remove the extra EOF blank line in `tests/golden/admin_small.rb`.
- `docs/CONFIGURE.md`: state clearly that `opl-configure` changes statuses,
  types, roles and workflows **instance-wide**, so it's for a dedicated
  OpenProject instance only; back up first.
- Add a test that fails if a tracked file contains a Windows user path or a
  home path.

### TH.14 · OS-level sandbox for the Spark worker
- **Owner:** Codex (preparation) + Owner (decision and the privileged setup) · **Size:** M · **Risk:** High · **Depends:** TH.1
- **Status:** In progress (owner chose (a), a separate WSL user; owner-run setup and canary pending)
- **Result:** (Codex, committed unchanged by Claude after review, `ai/hardening-claude` fab1d9e) `docs/SANDBOX.md` covers option (a), a dedicated low-privilege WSL user, with owner-run setup, the launch contract through a reviewed helper, the env allowlist across the user switch, and residual risks; option (b), a container, is compared; follow-ups after merge are listed. `bin/opl-sandbox-check` is an owner-run permission canary (PASS/FAIL only, fail closed), with offline tests using a fake sudo. Still needed: the owner picks (a) or (b) and runs the setup; the listed follow-ups (public per-run paths outside private state, launch helper plus narrow sudoers, cross-UID shutdown); then a real canary.
- **Review:** —

**(R1, second half)** An environment allowlist doesn't stop a process
running as the owner's OS user from reading private repos on disk.

Before T4.O, decide and set up one option:
- **(a)** a dedicated low-privilege WSL user that can only read and write
  the public repo's worktrees and its own HOME (recommended);
- **(b)** a container with only the worktree mounted.

Then prove it with a canary: a private file in a sibling repo is
unreadable from a worker run.

### TH.15 · A lowered Risk needs the owner
- **Owner:** Codex · **Size:** M · **Risk:** High · **Depends:** TH.5 part 1
- **Status:** Todo
- **Result:** —
- **Review:** (Claude, 2026-09-25) Approved (58461eb). Replays Risk from the first approval, keeps the peak, requires the owner for any lowering, and skips the cycle on unusable history. Nits for TH.R: (1) `field_changes` treats any detail starting with "Risk " as a Risk change, so a future field such as "Risk notes" would make every cycle fail; match the change pattern instead of the prefix. (2) Journals of affected tasks are fetched twice per cycle (once lenient, once strict); reuse the first read.

Found while doing TH.5. Risk decides whether Merge OK is needed (High) and
which reviewers are acceptable (Medium/High need Claude or Codex). It is a
plain field, so a builder can lower it after approval (High → Low) and
skip both.

**Fix:**
- When the task's parent Feature is in Approved or later
  (`enforce.APPROVED_ONWARDS`), the task's current Risk must not be lower
  than the highest Risk it has had since the feature was approved, unless
  the change that lowered it was made by an Owner-role member.
- Read this from the journal ("Risk changed from High to Low"), the same
  way TH.5 does (`opl/hal.py`, `collect._approvals`, the owners from
  memberships).
- Any other lowering → violation "Risk lowered without the owner"
  (Blocked).

**Tests:**
- the builder lowers High → Low after approval: Blocked, no merge;
- the owner lowers it: fine;
- the planner changes it while the feature is still Proposed: fine;
- raising risk is always fine.

### TH.16 · Runner follow-ups: attempts, refs, backup lock
- **Owner:** Spark · **Size:** S · **Risk:** Medium · **Depends:** TH.10, TH.8
- **Status:** Awaiting review
- **Result:** Attempts: result conclusions (review-pass/changes, test Pass/Fail) reset the (kind, id) count — only result-less runs count. Feature ceiling comments once (flag persisted in attempts.json, later ticks quiet). Refs: `worktree.delete_ref` removes only `refs/opl/` refs; runner drops the ref when its review/test tree is removed, keeps it with kept-as-evidence trees. Backup lock: `flock` on a lock file when available (crash-safe); otherwise mkdir + PID file with takeover only on a verifiably dead PID, skip + header-documented manual recovery otherwise; stale lock test forces the mkdir path. Tests: reset, single ceiling comment over 5 ticks, ref deleted/kept (unit + runner level), stale lock doesn't break backup. Suite 467 green.
- **Review:** (Claude, 2026-09-25) Approved (5286ecd). Attempts reset on a concluded result; the feature ceiling comments once; `refs/opl/*` are deleted with their worktree and kept with kept evidence; the backup lock is flock, or mkdir+PID with takeover only of a verifiably dead holder, and manual recovery is documented. Gap moved to TH.21: after the ceiling, an unblocked task still never gets another Spark review.

1. **Attempts reset:** a review or test run that concludes with a result
   (review-pass, review-changes, test Pass or Fail) resets that (kind, id)
   count in `attempts.json`. Only runs that end with no result count.
2. **Feature ceiling, once:** a feature at the ceiling gets the "needs lead"
   comment once; persist the fact that it was notified, and stay quiet
   after that.
3. **Refs:** delete the `refs/opl/<label>-<stamp>` ref when its review or
   test worktree is removed. Keep the ref when the worktree is kept as
   evidence (TH.4).
4. **Backup lock (Codex E8):** make the retention lock survive a crash
   safely: a process-scoped `flock` when available; otherwise keep mkdir
   plus a PID file and document manual recovery in `bin/opl-backup`'s
   header. Never remove an unverified lock automatically.

**Tests:** reset after a result, a single ceiling comment across many
ticks, the ref deleted on removal and kept on keep, and a stale lock that
doesn't break the next backup.

### TH.17 · Shutdown can't miss a worker that is starting (Codex E4)
- **Owner:** Spark · **Size:** S · **Risk:** High · **Depends:** TH.9
- **Status:** Awaiting review
- **Result:** Module-level stop gate in `supervisor.py`: `terminate_all()` sets it under `_LIVE_LOCK`; `run_worker` checks the gate and spawns+registers inside the same lock hold — when stopping it writes "conductor shutting down" to the log and returns "failed" without ever spawning (spawn failures still raise). `reset_stop_gate()` added for tests (every gate test resets in cleanup so the persistent gate can't leak into other tests). `SparkRunner.shutdown()` loops `terminate_all()` until all active records are done or one bounded deadline passes, returning the total stopped. Tests (`tests/test_shutdown.py`): barrier-held preparation → no spawn + shutdown line; post-spawn shutdown kills the worker; gate reset lets the next run proceed; shutdown waits for records; stuck records stop at the deadline. Suite 472 green.
- **Review:** (Claude, 2026-09-25) Approved (ce14ae4). The gate check, spawn and registration share one lock hold; shutdown repeats `terminate_all()` until every record is done or the deadline passes. Merge fix: the TH.9 shutdown test now reopens the process-wide gate (5108a48).

`supervisor.terminate_all()` only kills workers that are already
registered. A run thread that is still preparing (fetching, creating a
worktree) spawns after the snapshot and survives the conductor. There is
also a small window between spawn and registration.

**Fix:**
- A module-level stop gate in `supervisor.py`. `terminate_all()` sets it
  under `_LIVE_LOCK`.
- `run_worker` checks the gate and spawns and registers **inside the
  same lock**. When stopping, it returns outcome "failed" with a
  "conductor shutting down" line and never spawns.
- A `reset_stop_gate()` for tests.
- `SparkRunner.shutdown()` keeps calling `terminate_all()` until every
  active record is done or one bounded deadline passes.

**Tests:** barriers around preparation and spawn. Shutdown during
preparation starts no worker; shutdown after spawn kills it; nothing
survives.

### TH.18 · Dispatch boundary: real repo identity, privacy, fresh read (Codex E2, E7)
- **Owner:** Spark · **Size:** M · **Risk:** High · **Depends:** TH.6
- **Status:** Awaiting review
- **Result:** Dispatch: origin must be github.com (https, ssh://, scp-like) with the configured slug — foreign hosts and local paths fail even with a matching slug, reading raw config (insteadOf invisible). `GitHub.repo_private` (sole github.py addition, at class end) with per-tick cache; failures refuse fail-closed. Run threads re-read task+parent live: qualifying status, Approved/Building parent (Epic for tests), same project — else no worker and Blocked "Not started (<kind> run)". 422 on create re-looks-up the open PR into pr_base (find_open_pr exceptions propagate for TH.20). Every Spark run shares `<state_dir>/spark-data` as XDG_DATA_HOME (HOME stays per-run; TH.19 reads it). Harnesses gained repo-privacy + HAL + status/type coverage; fix harness already used a github origin. Tests: foreign host, private repo, lookup failure, stale parent, 422 reuse, shared data dir, repo_private true/false/raise. Suite 480 green.
- **Review:** (Claude, 2026-09-25) Approved (a78cc36). Origin must be github.com plus the slug (foreign hosts, lookalike hosts and local paths are refused); GitHub privacy is checked per tick, and a failed lookup refuses; the run thread re-reads task and parent before any worktree or worker; a 422 re-looks-up the open PR; Spark runs share `<state_dir>/spark-data` as XDG_DATA_HOME. Merge fix: `repo_private` now treats a missing or odd `private` field as private (5108a48).

Immediately before any worker starts (build, fix, review, test):
1. **Origin identity:** `remote.origin.url` must be **github.com**
   (https, `ssh://git@github.com/`, or `git@github.com:`) **and** the
   configured `owner/repo`. `https://example.invalid/owner/repo.git`
   fails. Keep reading the raw config, not the insteadOf expansion.
   Tests may keep `url.<bare>.insteadOf`.
2. **Actually public:** read `GET /repos/{owner}/{repo}` and refuse unless
   `private` is false. Add `GitHub.repo_private(owner, repo)` **at the
   end of the GitHub class**, the only change allowed in
   `opl/github.py`. Cache it per tick. A failed lookup refuses (fail
   closed).
3. **Fresh read:** in the run thread, right before spawning, re-read the
   task and its parent with the spark client. The task must still be in
   the status that qualified it, the parent still Approved/Building (or
   the Epic, for tests), and in the same project. Otherwise no worker,
   and the task goes to Blocked with "Not started (<kind> run): <reason>".
4. **PR reuse (E7, runner side):** `GitHub.find_open_pr` will raise on
   lookup failure (TH.20, Claude); let that propagate so the conclusion
   is retried next tick. If `create_pull` answers 422, look up the open
   PR from the head into `pr_base` again and use it; otherwise raise.
5. **Spark cost logs (with TH.19):** give every Spark run the same
   OpenCode data dir, `XDG_DATA_HOME = <state_dir>/spark-data`, while
   HOME and the other dirs stay per run, so the conductor can read Spark's
   OpenCode usage in one place. Put it in `worker_environment` or in the
   runner's `_worker_env`.

**Tests:** foreign host refused; private repo refused; lookup failure
refused; parent un-approved between collect and spawn gives no worker;
a 422 re-lookup reuses the PR; all runs share the data dir.

### TH.19 · Cost tracking correctness (Codex E5, E6; Claude's TH.12 review)
- **Owner:** Claude (taken over; Codex's session stopped) · **Size:** L · **Risk:** High · **Depends:** TH.12
- **Status:** Awaiting review
- **Result:** (Claude, 2026-09-25, `ai/hardening-claude` 9ca3e1f) One attribution rule for every tool (`usage.attributor`): exactly one registered worktree wins (test runs → feature); a path recorded for several items stays unknown with no fallback; unregistered folders use the delimited prefix rule; item types must match; Codex rows use the same rule. Spark's `<state_dir>/spark-data/opencode` is read next to the owner's store, and ccusage gets both via `OPENCODE_DATA_DIR` (from ccusage's docs; the web check was cut short by a tool limit, so confirm during the TC.1 live recheck). Counter convention per model row from `totalTokens` (exclusive / reasoning-in-output / inclusive); an unexplained total is unpriceable. Incomplete means unknown across tools. ccusage gets the environment minus every token variable, and the SQLite URI uses `pathname2url`. `_cost_changes` wiring is done. 17 new synthetic tests; full suite 515 OK (after fixing a machine-path literal in a comment). Reviewer: Codex (TH.R).
- **Review:** —

1. **One attribution rule for every tool (claude, codex, opencode):**
   - A folder under exactly one registered worktree in `runs.jsonl` goes
     to that item: `test` runs → feature, everything else → task.
   - A folder registered to two or more items stays unknown (no
     last-wins).
   - An unregistered folder uses the delimited prefix rule
     (`task_for_folder`, DESIGN §7: owner-started Claude/Codex
     sessions).
   - Anything else stays unknown.
   - This is the owner-approved design. Record disagreement in the
     report, but implement this.
2. **Spark's OpenCode usage** lives in `<state_dir>/spark-data` (TH.18
   item 5). Also run the opencode report against that data dir. Verify
   from the pinned ccusage 20.0.24 source which environment variable
   selects it, and document it.
3. **No secrets to ccusage:** run `npx ccusage` with an environment
   stripped of every configured token variable (the names come from
   settings).
4. **SQLite URI:** build it with `pathname2url`, so paths with `?`, `#`
   or `%` work.
5. **Inclusive counters (E6):** normalise per tool against the pinned
   source. Use `totalTokens` to tell inclusive from exclusive, never
   double-charge cache or reasoning, and test both shapes.
6. **Incomplete means unknown:** if any session attributed to an item
   can't be priced, that item's actual stays unknown (not a partial sum),
   and this is logged.
7. **Wire it:** in `opl/conductor/__main__.py`, change only
   `_cost_changes`. Pass `state_dir`, the stripped env and the
   spark-data dir.

**Tests:** synthetic only, one per item above, plus an end-to-end run
with a fake ccusage.

### TH.20 · Checks semantics, scrub-then-truncate, PR lookup failure (Codex E1, E3, E7)
- **Owner:** Claude · **Size:** M · **Risk:** High · **Depends:** TH.8
- **Status:** Awaiting review
- **Result:** (Claude, 2026-09-25, `ai/hardening-claude` 4c64541) E1: with required checks, each must be present and every matching signal must pass (check runs matched on the named app; legacy statuses by context), and extras don't count. Without requirements, every present check run and legacy status must pass and at least one must exist; in-progress runs and state-less statuses are not green. E3: both clients read up to 64 KiB of an error body, scrub it whole, then cut it to 500. E7: `find_open_pr` raises on lookup failure. Tests cover each case (mixed failing status, empty required list, missing or wrong-app check, failing required status, in-progress run, secret across the cut, lookup failure). Suite: 470, all green except one flaky `test_static` restore test that passes alone (concurrent suites). Also fixed Codex's TH.15 fixture for TH.5 part 2 (d8da252), and moved every branch to it. Reviewer: Codex (night loop / TH.R).
- **Review:** —

- **E1:** green only with evidence. Every present signal counts (check
  runs **and** legacy statuses; a failing status is never outvoted).
  Required checks must be present and passing, matched on the issuing
  app where GitHub names one. An empty required list falls back to "all
  present signals pass, at least one exists". Nothing at all is never
  green.
- **E3:** scrub server error bodies before truncating them
  (`opl/openproject.py`, `opl/github.py`).
- **E7:** `GitHub.find_open_pr` raises on lookup failure instead of
  returning "".

### TH.21 · A lead can give an item fresh retries
- **Owner:** Spark · **Size:** S · **Risk:** Low · **Depends:** TH.16
- **Status:** Awaiting review
- **Result:** Ceiling hits now store `{"at": <iso>, "notified": bool}` in attempts.json (old bare-`true` flags read as notified-without-time and never re-arm). Before blocking at the ceiling, the tick compares the hit time with the item's `status_since` (datetimes and ISO strings both accepted; anything unparseable stays blocked): a later move clears the count and the flag for a fresh pair of attempts. Result conclusions already reset via TH.16. Documented in CONDUCTOR.md ("Attempt ceilings and fresh retries"). Tests: review blocked-then-moved runs again (count back to 1, no "needs lead"); unmoved stays blocked with count intact; feature re-armed after a move. Suite 501 green.
- **Review:** (Claude, 2026-09-25) Approved (5067242). The ceiling-hit time is compared with status_since; legacy flags fail closed, and the runner's own Blocked move can't re-arm. Codex TH.R: it proves a later move, not a human author (documented limit).

After the review/test ceiling (TH.10/TH.16), the count stays at 2. When a
lead unblocks the task, the next dispatch blocks it again straight away,
and the only way out is editing `attempts.json` by hand.

**Fix:**
- Store the time a ceiling was hit.
- If the item's `status_since` is later than that (someone moved it
  since), clear its count and notified flag, so it gets a fresh pair of
  attempts.
- Document this in `docs/CONDUCTOR.md`.

**Tests:**
- blocked at the ceiling, then moved by a person: runs again;
- not moved: stays blocked;
- a feature notified once, then re-armed after a move.

### TH.22 · TH.R fixes in runner and backup (Codex F4, F5, F7)
- **Owner:** Claude (taken over at the owner's request) · **Size:** M · **Risk:** High · **Depends:** TH.R
- **Status:** Awaiting review
- **Result:** (Claude, 2026-09-25, 7ab8769)
  - F4: the run's authorization is read before preparing and stored: task status, project, same parent still Approved/Building (or the Epic), assignee, Reviewer, and for builds and fixes every live predecessor Merged. It is re-read immediately before each worker start and each build retry and must be unchanged. Otherwise no worker starts and the task is Blocked with the reason. Fix runs also check the PR link. E2E scenarios cover un-approval, an added predecessor and a changed reviewer during preparation; all three fail with the guard disabled.
  - F5: without flock, a held retention lock is never taken over. Retention is skipped with a warning, recovery is manual and documented, and the backup still completes.
  - F7: the "needs lead" notice is marked sent only after it posts; a failed post is retried.
  - Suites: Windows 542 OK, Linux-native 542 OK.
  - Codex final2 (pinned at 2710c1c): FAIL.
    - F1, F2, F5, F6, F7 and the stop gate are closed.
    - F3's N1 to N4 were fixed after that pin (1851cd1).
    - F4 had two remainders: the prompt came from a second, unchecked read, and the PR link was not in the snapshot. Nits: refused runs left scratch trees, the backup comment was stale, and there was no in-tree negative test.
  - All fixed in 56ba9af: every packet is built from the guard's own read; the PR link must still equal the planned one, before preparation and at spawn; refused runs remove their untouched tree; the comment is fixed; an in-tree test shows a worker would run with the guard off.
  - Suites at 56ba9af: Windows 548 OK, Linux-native 548 OK.
  - Codex final3 (2e93e2d): FAIL, on D1 only. N1 to N4 and final2 #1, #3, #4 and #5 are closed.
    - D1: a missing PR-link field skipped the check.
    - D2 (nit): a Visibility-only dry run planned nothing.
    - D3 (nit): only one guard-off control.
  - Fixed in 2538257:
    - A review or fix now requires the PR-link field and a non-empty link equal to the planned one, at both checks, and never fails open. The fixtures carry the field.
    - Dry runs plan from what differs.
    - Guard-off controls now exist for all three scenarios.
  - Suites at 2538257: Windows 554 OK, Linux-native 554 OK.
- **Review:** —

1. **F4, fresh check at every spawn:**
   - Move or repeat the TH.18 fresh read so it runs after worktree and
     fetch preparation, immediately before each worker launch and each
     retry.
   - Re-read and compare the task's status, project, parent (same id,
     same project, still Approved/Building, or the Epic for test runs),
     its predecessors (all Merged), and its assignee and reviewer (still
     the ones that qualified the run).
   - Build the packet from the freshly read task and parent, not the
     collected World.
   - Any change means no worker, and Blocked with "Not started (<kind>
     run): <reason>".
   - Test: a barrier mutates the fake server during preparation (parent
     un-approved, predecessor added, reviewer changed) and no worker
     starts.
2. **F5, backup lock fallback:** without flock, never take over a lock
   automatically. If the lock dir exists, skip retention with a warning
   and the documented manual recovery. Keep the flock path.
   - Test: an existing lock dir, with a dead PID, a live PID or an
     unreadable PID, is never removed, and the backup still succeeds
     without retention.
3. **F7, notice before flag:** for a feature at the ceiling, post the
   "needs lead" comment first, and persist `notified` only after the
   POST succeeds. If the POST fails, a later tick tries again.
   - Test: the comment POST fails once, then the next tick posts it
     exactly once.

### TH.23 · TH.R fixes: PR identity, env, costs, CI pages, stop gate (Codex F1, F2, F3, F6)
- **Owner:** Claude · **Size:** L · **Risk:** High · **Depends:** TH.R
- **Status:** Awaiting review
- **Result:** (Claude, 2026-09-25, 1bf1be3, 5a8c34c, 7ef085b)
  - F1: the review link must be github.com/<configured repo>/pull/N (checked before any API call), and GitHub must report the configured repo as the PR's base and head. Forks and unknown identities are refused, and a missing head repo is no longer assumed to be the URL's repo. A refusal blocks the task and nothing is read, fetched or shown to Spark.
  - Stop gate: a per-runner stop event is bound to its run threads, so reopening the process gate never releases an older runner's threads.
  - F2: ccusage also loses every `[runner] worker_env` name, case-insensitively.
  - F3: counter conventions match exactly; the session total must equal the model rows; unpriceable items come back as `{"unknown": true}`, and the costs rule clears stale actuals and roll-ups above them.
  - F6: legacy statuses are read on every page, and an incomplete read is not green. The DESIGN checks policy is now explicit.
  - Also from Claude's review: the conductor tests no longer run the real `npx ccusage` (09a7fff); a new end-to-end pipeline test (8d4477f); `tests/fakebin` kept LF for WSL (40f39a5); an explicit branch in the synthetic-upstream fixture (4b9ad82).
  - Suites at 7ef085b: Windows and Linux-native 537 OK each.
  - Codex TH.23 review (6b26b90): REJECT, with F1, F2, F6 and the stop gate accepted and F3 partial. New findings:
    - N1: an unknown cost was ignored on Blocked tasks.
    - N2: a failed collection rebuilt partial roll-ups.
    - N3: plain schemas carry no allowed values, so every live list-field write would fail. This was a real bug since TH.7, and the fake hid it.
    - N4: the fake GitHub's empty-status answer was wrong.
  - All four were fixed in 1851cd1: options come from the work package or project form when the schema has none; an explicit unknown clears the actual in any status; a failed collection leaves roll-ups alone; the fakes are faithful (plain schemas without options, forms with them). The e2e asserts that list writes went through the form and states its limits.
  - Suites at 1851cd1: Windows 545 OK, Linux-native 545 OK.
- **Review:** —

- **F1 (blocker):** before any review fetch, the PR URL must be
  github.com/<configured owner/repo>/pull/N. The PR's base repo must be
  the configured repo, and its head repo must be the configured repo or
  a public repo (privacy read live, fail closed). Otherwise no fetch and
  no worker.
- **F2:** the ccusage environment also drops every `[runner] worker_env`
  name, case-insensitively.
- **F3:** exact integer convention checks; a session total must equal
  the sum of its breakdowns, else the session is unknown; an item whose
  actual became unknown gets its stale actual cleared (and roll-ups
  treat it as unknown).
- **F6:** read all legacy status pages, or treat an incomplete read as
  not green.
- **Stop gate (Codex note on 8d4477f):** each runner gets its own stop
  event, passed into `run_worker`, so reopening the process-wide gate
  can never release an older runner's threads.

### TH.RE · Early independent review of landed phase 8 work
- **Owner:** Codex · **Size:** M · **Risk:** High · **Depends:** —
- **Status:** Done
- **Result:** (Codex, 2026-09-25) Private review report (kept outside this repo). Blockers E1 (CI green without evidence) and E2 (repo identity/privacy, fresh read) are fixed by TH.20 and TH.18. Should-fix E3, E4 and E7 are fixed (TH.20, TH.17, TH.18/TH.20); E5 and E6 are TH.19; nit E8 is fixed (TH.16).
- **Review:** —

Review what has already landed now, so any fixes happen before TH.R, and
TH.R then only has to cover the difference. Spark branch: TH.1–TH.4, TH.6,
TH.8, TH.11, TH.12, TH.13. `ai/hardening-claude`: TH.7, TH.5 part 1,
TH.9. For each: does it close its R-finding, and are there new problems?
The private report is kept outside this repo, never in it.

### TH.D · Owner decision: merge policy for Medium risk
- **Owner:** Owner · **Size:** S · **Risk:** Medium · **Depends:** —
- **Status:** Done
- **Result:** (Owner, 2026-09-25) **(b)**: during the pilot, Medium and High both need the owner's Merge OK. Implemented by Claude: `rules/merge.py` `NEEDS_OWNER_OK = {"Medium", "High"}`, and the screens rule asks "OK merge" for both; DESIGN §3 is updated. Relaxing to (a) later is that one constant.
- **Review:** —

The design (DESIGN §3, approved in brainstorming) lets the conductor merge
**Medium**-risk tasks after a Claude/Codex review. The older private
operating agreement required the owner's approval for Medium and High. The
owner picks one:
- **(a)** keep the design: Medium auto-merges after review, High needs Merge OK;
- **(b)** Medium and High both need Merge OK.

The merge rule and DESIGN §3 follow the decision.

### TH.R · Final-head independent re-review
- **Owner:** Codex · **Size:** M · **Risk:** High · **Depends:** TH.1–TH.13
- **Status:** Done: **PASS** at 6e75349 (after FAIL, FAIL, FAIL, then fixes)
- **Result:** (Codex, 2026-09-25, reviewed ec8fe98) Private review report (kept outside this repo). Verdict **FAIL**. Blocker F1: the review checkout fetches the PR named by the task's editable PR link, and its head repo, with the GitHub token, without checking the PR belongs to the configured public repo. Should-fix F2 to F7: ccusage keeps the worker credential; cost edge cases (1-token tolerance, session vs breakdown coverage, stale actuals kept); the fresh check is before preparation, not every spawn; the backup mkdir/PID takeover race; legacy status pagination; the ceiling notice is persisted before delivery. Also: reopening the process-wide stop gate (8d4477f) must not release an older runner's still-preparing threads. R3, R5, R9, R12, R14, E3, E4 and E7 resolved; the rest partial pending the fixes above, TH.D, TH.7a and TH.14 evidence. Fixes: TH.22 (Spark), TH.23 (Claude); then TH.R again on a frozen head.
- **Review:** (Codex, 2026-09-25) Five private review reports, kept outside this repo: TH.R (FAIL), TH.23 (REJECT), final 2 (FAIL), final 3 (FAIL) and final 4 (**PASS**, 554 tests OK). The PASS approves the source for publication. It does not authorise pushing or merging, and it does not certify live operation. TH.D, TH.14 (a real sandbox and canary), TH.7a (live API samples, including the form-based option writes) and the owner's push approval remain.

Re-run TM.1 on the **final** head, including TC.2–TC.4 and all TH fixes:
confirm R1–R14 and the TC.1 corrections are resolved, and no new blockers.
Only a pass here allows the public push and merge.

---

## Phase 5: Pilot

### T5.1 · One real feature, Proposed to Done
- **Owner:** Owner · **Size:** L · **Risk:** Medium · **Depends:** T4.O
- **Status:** Todo
- **Result:** —
- **Review:** —

**Scope:**
1. Pick a small feature in a Public project.
2. Claude plans it (Proposed).
3. The owner approves, and the system runs it to Done.
4. Claude writes the pilot report: what worked, what the owner had to do by
   hand, and which limits to tune.

---

## Phase 6: Switch-over

These tasks are not for Spark: they touch private repositories and the
owner's own instructions.

**Rule (owner decision, 2026-09-24): no project is copied over as-is.**
Every project is re-planned with the owner in a Superpowers brainstorming
session **before** anything about it enters OpenProject. That way each
project arrives as a detailed, approved plan, not a dump of old issue
titles. Projects move **one at a time**: plan it, load it, check it, then
the next one.

### T6.0 · Prepare private planning briefs, one per project
- **Owner:** Claude/Codex · **Size:** M per project · **Risk:** Low · **Depends:** —
- **Status:** Done
- **Result:** (Codex, 2026-09-25) 4 private briefs written.
- **Review:** —

Input for T6.1's Superpowers sessions, so each session starts informed.
For each existing project, write a private brief, stored with the private
PM tooling and never in this repo. Each brief covers:
- what the project is for today;
- current code state and recent history;
- the open items in its old tracker, grouped, with obvious duplicates and
  stale items flagged;
- a first guess at epics and features;
- the questions only the owner can answer.

Create nothing in OpenProject. The Result line records the count of briefs
only, no names.

### T6.1 · Per-project planning session with the owner
- **Owner:** Claude/Codex + Owner · **Size:** M per project · **Risk:** Medium · **Depends:** T5.1
- **Status:** Todo
- **Result:** —
- **Review:** —

**For each project, one at a time**, run the Superpowers brainstorming skill
with the owner:
1. **Gather:** the project's open items from its old tracker, its current
   code state, and recent history. Treat old items as input, not as the plan.
2. **Decide with the owner:** what the project is for now, what's done,
   what's dropped, and what's still wanted, one question at a time.
3. **Write the plan** in the DESIGN.md §1 shape:
   - Epics (big areas).
   - Features (shippable things the owner approves), each with **Why /
     What you'll see / Done when**.
   - Tasks under each feature, each with Size (S/M/L, nothing over 90
     minutes of work), Risk, Assignee and Reviewer following DESIGN.md §3,
     and dependencies.
   - Each feature placed in the **Now / Next / Later** bucket.
4. **The owner reviews and approves** the written plan. Only approved
   plans move on to T6.2.

**Output:** one plan document per project, stored privately with that
project, never in this public repo. The Result line here lists which
projects are done, by count only (no names).

### T6.2 · Load each approved plan into OpenProject
- **Owner:** Claude/Codex · **Size:** M per project · **Risk:** Medium · **Depends:** T6.1 (per project)
- **Status:** Todo
- **Result:** —
- **Review:** —

For each approved plan:
1. Create the project with its Repo and Visibility.
2. Create its epics, features (in status **Proposed**, or **Approved** where
   the owner already approved them in T6.1) and tasks (Draft), with every
   field and dependency.
3. Close the old tracker's items with a link to their new home.
4. **The owner checks the project in the Feature pipeline and Roadmap
   screens** before the next project starts.

### T6.3 · Point the global agent instructions at OpenProject
- **Owner:** Claude/Codex · **Size:** S · **Risk:** Medium · **Depends:** T6.2 (all projects)
- **Status:** Todo
- **Result:** —
- **Review:** —

**Condition:** the owner approves the wording.

### T6.4 · Archive the old PM system and close its tasks
- **Owner:** Claude/Codex · **Size:** M · **Risk:** Medium · **Depends:** T6.3
- **Status:** Todo
- **Result:** —
- **Review:** —

**Condition:** only once no worker run still depends on it.

### T6.5 · Owner decision: switch-over complete
- **Owner:** Owner · **Size:** S · **Risk:** Low · **Depends:** T6.4
- **Status:** Todo
- **Result:** —
- **Review:** —
