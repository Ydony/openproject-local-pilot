# Live acceptance test report (T5.1) — 2026-09-27/28

**Result: all 31 cases of `docs/LIVE-TEST-SUITE.md` passed live**, several
only after the bugs they exposed were fixed. OpenProject, the conductor,
GitHub and two real LLMs (Claude, Spark) worked a fake project — "Coffee order
tracker", three features, five tasks — from planning to production and Done.
Every pass is visible in OpenProject (scoreboard project "Acceptance
Results", one comment per case by the testing model's own user) and, for
code, on GitHub (`opl-acceptance-sandbox` PRs #1–#4, all merged by the
conductor at the reviewed commit).

## How it ran

- **Instance:** OpenProject 17 (local, loopback), configured by
  `bin/opl-configure` (second run: no changes).
- **Accounts:** owner, `claude`, `codex`, `spark`, `conductor`, each with its
  own API key; permission check 10/10.
- **Claude and Codex** connect through their own MCP servers (pinned
  `openproject-ce-mcp` 0.4.1), each seeing only the sandbox projects.
- **Spark** runs only as the locked-down `opl-worker` account in a dedicated
  WSL distribution (no Windows drives or programs), launched by the conductor
  through `bin/opl-sandbox-run` → root-owned `opl-spark-launch`
  (clone / run / export). One narrow sudo rule. The OS checker passed on every
  check. Commits come back as data-only git bundles.
- **Conductor** runs continuously in live mode for the sandbox projects only.
- **Codex was out of credits:** its build/review turns were substituted by
  Claude and Spark (A1.2, A2.1); its OpenProject connection was verified.
- **Unattended owner actions:** at the owner's instruction, Claude made the
  owner-only clicks (approve, unblock, Merge OK, deploy decisions, Done,
  delete) with the admin account; each is labelled in OpenProject
  "Owner action performed by Claude on the owner's instruction".

## Results

| Group | Cases | Result |
|---|---|---|
| S Setup and connections | S1–S6 | Pass |
| P Planning | P1–P5 | Pass (Claude created/edited/versioned; Spark answered Claude's question in its result comment; models cannot delete, the owner can) |
| F Full frame | F1–F9 | Pass (approval → Ready/Building → Spark build → PR → review → merge → test deploy → feature test → deploy decision → production → Done; Medium risk waited for Merge OK) |
| R Review cycles | R1–R3 | Pass (changes requested → fix run from the PR's current head → re-review; a push after review voided the Pass) |
| G Gates | G1–G5 | Pass (unapproved work never started; no Spark in private projects; models cannot approve; builder ≠ reviewer; At risk) |
| V Visibility and cost | V1–V3 | Pass (Needs me exact; pipeline/roadmap views; real actual costs per task with roll-ups) |

Cost of the whole fake project (Spark, measured): about **$0.03**.

## Bugs found live and fixed (all on branch `ai/live-test-suite`)

Configure against OpenProject 17: permission renamed to
`add_work_package_comments`; `work_package_assigned` needed to assign models;
`edit_project` needed for At risk; bot password rule; project list-field
options; Repo URL format; versions endpoint; types for later projects; saved
views in HAL link form (#23), `targetVersions`, My page grid, boolean filter
read-back; readable rails errors and fresh-instance dry run (#23); permcheck
lockVersion; conductor project lookup without the identifier filter.

Conductor and Spark route: `~` in `local_repo`; a missing checkout crashed the
whole conductor; OpenCode adapter (#12) and sandbox adapter (#24); real
OpenCode JSON events (#27); detached review worktrees; review scope is the task
(#28); build result comments, no internal paths in comments, At risk read
(#28); stale-snapshot race blocking tasks (#29); tester scope + OpenProject
process evidence (#30); single-model reasoning remainder in costs (#31);
fix runs from the PR's current head (#32); cents rounding showed real costs
as 0.0; Python 3.12 thread crash; Linux shell discovery; Maintenance feature
flagged as "no tasks" (#26); status invalid for a type (#25).

Spark implemented most fixes (#12, #23–#32); the lead reviewed and integrated
them and fixed the rest.

## Installed during the test (owner-authorized)

- OpenCode 1.18.29 (Linux build, checksum verified) in the sandbox
  distribution, root-owned.
- Node.js 22.23.3 (nodejs.org, sha256 `df450af89261115e…` verified),
  user-local in the sandbox distribution; `ccusage` 20.0.24 via npx.
- `openproject-ce-mcp` 0.4.1 via uvx (the reviewed, pinned version).

## Open limitations and recommendations

1. **Codex** turns not exercised (no credits): repeat A1.2 / A2.1 with Codex.
2. **Claude's own usage** is not in the actual costs (the conductor reads
   usage only inside the sandbox). Estimates also differ widely from actuals:
   calibrate.
3. **OpenProject folds** same-user changes made within minutes into one
   activity entry with the first timestamp: "In progress" is not separately
   visible, and displayed times can be earlier than the change.
4. **Global views** include non-conductor projects (here the scoreboard).
5. `edit_project` is broader than the project status alone.
6. After a delete, the conductor skips one cycle (fail-closed read race),
   then continues.
7. A same-cycle "OK merge" appeared once for a review voided in that cycle;
   cleared on the next cycle.
8. The branch is local only: publishing it needs the owner's OK.

Next (issue #22): a Superpowers session to plan moving the first real project.

## Codex continuation — 2026-09-28

### Task 1: offline regression verification

Tested branch head `84c5ae8` with Python 3.13 on Windows. No application
code changed. No software installed, and no live services changed.

- Initial full discovery: **673 tests in 435.953s**, 66 failure reports,
  3 errors, 2 skips. The process selected the Windows WSL Bash launcher,
  which could not interpret the test harness's Windows paths.
- With Git Bash selected: **673 tests in 523.839s**, 9 failure reports,
  2 skips. All remaining failures were backup/restore fixtures: `cygpath`
  was missing from the process PATH, leaving Windows drive paths in shell
  archive operations.
- After adding both Git's `bin` and `usr/bin` to the process PATH, reran
  the entire affected group: `test_static`, `test_configure_cli`,
  `test_sandbox_check`, `test_sandbox_run`. **98 tests in 156.503s:
  OK (1 skipped)**. This covers every failing test from both full runs.
  Other tests passed in full discovery. This is full discovery plus a
  successful affected-group rerun, not a claim of a third all-green full run.
- Expected skips in full discovery: Ruby syntax check (Ruby absent from
  Windows PATH), and POSIX file modes (Windows). Non-failing ResourceWarnings
  remain in calibration and usage-mapping tests.

For future Windows runs, start in the worktree and select the existing Git
utilities before running discovery (adjust the installation directory if
Git is installed elsewhere):

```powershell
$gitRoot = Join-Path $env:ProgramFiles 'Git'
$env:PATH = "$gitRoot/bin;$gitRoot/usr/bin;" + $env:PATH
$env:GIT_TERMINAL_PROMPT = '0'
$env:GCM_INTERACTIVE = 'never'
py -3.13 -m unittest discover -s tests
```

This is a per-process setup, not a persistent system PATH change.

### Task 2: Codex connected; independent Claude review blocked

The connection is now verified through MCP as `codex`, user 6. Codex
created Epic #80 / Feature #81 / Tasks #82 and #83 through its own MCP.
Owner approval of #81 was separately labelled; an initial lock conflict
was corrected in Activity and the fresh-lock retry succeeded.

- **Spark build / Codex review: passed live.** Task #83, PR #5, reviewed
  SHA `154517763e94e5705a95df47f431702b00d61dde`. Codex independently checked
  the only changed file, exact YES+LF bytes and green CI, posted evidence
  on GitHub and via MCP, and set Pass. After the separately labelled owner
  Merge OK, the conductor merged the PR and set #83 Merged. Recorded Spark
  usage: 55,219 tokens, $0.0029.
- **Codex build: delivered.** Task #82, PR #8, SHA
  `68eec3ef18b7eea049559ea8dbdc0558ff69379b`. Only `codex-acceptance.txt`
  added, exact YES+LF verified, CI green. Codex updated the task through
  MCP to In review. PR remains open; no self-review or Merge OK.
- **Claude independent review: blocked.** The actual Claude Code attempt
  failed authentication: OAuth session expired and could not be refreshed.
  No model turn ran, usage zero. This is not a review Pass. Claude needs
  to sign in before the prepared review can resume.
- **Scoreboard pending.** Codex's MCP read of #65 failed; the configured
  project allowlist excludes Acceptance Results. Do not treat per-task
  evidence as completed scoreboard publication.

Task 3 has not started: the owner's requested task order is preserved.
No public push, PR, merge, or real-project migration was performed.

### Task 2 completed by Claude — 2026-09-29

- **Codex build / Claude review: passed live.** Claude (own MCP, user 5)
  independently checked PR #8: one commit
  `68eec3ef18b7eea049559ea8dbdc0558ff69379b`, only `codex-acceptance.txt`,
  raw bytes `59 45 53 0A`, CI green. Posted the review on GitHub and
  `reviewed: <sha>` + Review result Pass in OpenProject. The conductor set
  "OK merge / Needs you"; the owner approved in chat, and the Merge OK was
  set with the owner account and labelled. The conductor merged PR #8 at
  17:22:25Z (merge `8e5cefdd`), #82 Merged, and then passed the feature
  test of #81. **A1.2 and A2.1 are now proven with Codex itself.**
- Before this, OpenProject and the conductor were found stopped (a
  Windows/WSL restart). This led to the stack start below.

### Stack start after a Windows restart — 2026-09-29

New `bin/opl-conductor-start` (start/status/stop, detached, key file
read without executing it) and `windows/opl-stack.ps1` (one manual
command, owner or LLM: OpenProject, health wait, conductor). Tests:
`tests/test_conductor_start.py` 5 OK on Linux (skipped on Windows).
Live from Windows: status → stop → start → status all correct, stack up
with exit 0. Not exercised: the `bin/opl-start` fallback (the containers
came back by themselves each time).

### Permission and status audit — 2026-09-29

Read-only: for all 25 work packages in the three sandbox projects and every
account, the edit form (validates, saves nothing) was asked which statuses
and fields that account may change. Model transitions on active items were
already proven live by F/R/G and were not repeated.

- **As designed:** models cannot approve, reject, park or finish anything;
  the conductor may move Approved → Building and In test → In production
  only; Spark gets 404 in the Private Sandbox.
- **Bug (#33, Spark):** `opl-configure`'s My page pin dropped the existing
  widgets' ids, so OpenProject replaced them and deleted their views: the
  owner's "assigned to me" / "created by me" widgets showed "could not be
  found".
- **Bug (#34, Spark):** the permission-check fixture "Test feature" sits in
  the owner's Needs me (Approve, then Unblock after the owner approved it).
- **Bug (#35, Spark):** as an admin, the owner can move epics to
  OpenProject's seeded statuses (New, In specification, …), because
  unmanaged seeded roles keep their default workflows for Epic/Task.

Fix status: all three fixed by Spark, reviewed and integrated. #34 is
verified live (the fixture left Needs me at 18:01Z). #33 adds
`bin/opl-configure --repair-my-page`; the lead corrected the widget
identifiers (OpenProject's defaults are `work_packages_assigned` /
`work_packages_created`), and the live dry run plans exactly the two
repairs. #35 removes unmanaged roles' workflows for the managed types.
**Applied and verified live (2026-09-29, owner's instruction):**
`bin/opl-configure --repair-my-page` (dry run first: exactly the workflow
cleanup for Epic/Feature/Task plus two widget repairs). Afterwards every
My page widget's view resolves (assigned → 38, created → 39, Needs me →
31, all HTTP 200), and the owner is offered only Open/Closed on epics (#40,
#71; before: about ten seeded statuses). The scoreboard (#65–#70 Done, #64
Closed) was closed with labelled owner actions.
- **Gap (fixed in the suite, not the code):** the scoreboard used
  checklists in feature descriptions, which cannot be ticked, and named
  nobody to close groups, so it stayed Proposed although all cases passed.
  `docs/LIVE-TEST-SUITE.md` now uses one task per case and a closing step.
- **Gap (owner, one click):** Codex is not a member of "Acceptance
  Results", so it could not record its results there (its MCP allowlist
  is now widened; the membership needs the owner).
- **Platform limit, handled by the conductor:** OpenProject Community
  Edition cannot restrict single custom fields by role, so every model can
  technically write Action, Needs you, Merge OK or Review result. The
  conductor blocks unauthorised approvals ("Unauthorised approval", G3
  passed live) and resets Action / Needs you every cycle.
