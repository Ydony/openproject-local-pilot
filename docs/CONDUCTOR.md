# Conductor: what it does and how to live with it

The conductor (`bin/opl-conductor`, code in `opl/conductor/`) is a
check-and-fix loop over the OpenProject tracker. Once a minute it collects
the tracker state, derives one change per broken rule (stages, screens,
merge), and applies it. Spark build, review, test and fix runs (see
`opl/conductor/spark/runner.py`) do the heavy work in background threads;
the loop itself never blocks on them.

## Watch vs live mode

- **Watch (default):** evaluates the rules and appends the planned changes
  to `<state_dir>/watch.log`, but writes nothing to any API. Safe to run
  any time: `bin/opl-conductor --once` runs a single cycle and exits.
- **Live:** additionally needs `"live": true` under `[conductor]` in the
  config AND the `--live` flag (`bin/opl-conductor --once --live`).
  Without both, every cycle stays watch-only. In either mode, an
  unreachable API or an incomplete read (any failed page) only logs
  "read failed, skipping cycle": the conductor never acts on a partial
  snapshot.

## For reviewers: record the commit you reviewed

A Pass is bound to one commit (TH.5). As the task's **Reviewer**, first
post a comment with this line, then set `Review result = Pass`:

```
reviewed: <full 40-character PR head SHA>
```

Only the Reviewer's own comments count, and the latest such line wins.
If the PR gets a new push before the merge, or the line is missing, the
conductor clears Review result and says why in a comment. Review again,
and post the new SHA. A Pass or Merge OK set by anyone other than the
Reviewer or an Owner-role member blocks the task ("Unauthorised
approval").

## Before a Spark worker starts

The runner checks a run twice:
1. When it picks the run up.
2. Again **immediately before each worker start and each retry**, after
   the worktree and fetch are ready.

Both checks read the tracker live:
- the task's status;
- its project and parent (the parent must still be Approved/Building);
- the assignee and Reviewer;
- for builds and fixes, every predecessor (all must be Merged, including
  any added in the meantime).

For reviews and fixes, the PR link must be this project's own pull
request and must still be the link the run was planned with. The
worker's prompt is built from the second read itself, so it describes
exactly the state that was approved. If anything changed or can't be
read, no worker starts; the untouched scratch tree is removed, and the
task goes to Blocked with "Not started (<kind> run): <reason>".

## Tokens, one instance, and shutdown

- The conductor reads and changes OpenProject with the **conductor**
  token only. It never loads the admin token, so run it without
  `OPL_TOKEN_ADMIN` in its environment (`opl-configure` is the only admin
  user). Without admin rights, `/api/v3/users` answers 403. Users are then
  matched by their display name against the model's accounts, and "the
  owner" is whoever holds the Owner role in the project.
- The Spark runner exists only in live mode (config **and** flag). It acts
  in OpenProject as the spark user (`OPL_TOKEN_SPARK`), so its moves and
  review results are attributed to Spark. It is ticked once per cycle. With
  `--once`, the conductor then waits for the runs it started and concludes
  them before exiting.
- One conductor per state dir: `<state_dir>/conductor.lock` is held with an
  OS file lock for the process's lifetime, so a second conductor refuses
  to start. A crash never leaves a stale lock.
- Ctrl+C or SIGTERM stops the process tree of every running worker. Runs
  killed this way are not concluded, and their worktrees stay as
  evidence.

## Where the logs live

### Risk changes after approval (TH.15)

For an active Task under an Approved-or-later Feature, the collector replays
the complete English activity journal from the Feature's first approval.
Current Risk below its highest value in that period requires the author of
the lowering to hold the project's Owner role. Otherwise enforcement blocks
the task with **Risk lowered without the owner**, and merge proposes nothing.
Owner permission is not blanket permission for subsequent builder lowerings.
A partial raise does not erase an unauthorized drop; restoring the peak does.
Planning changes before approval do not count. Reapproval does not reset the
historical peak. Membership authority is evaluated using current memberships,
as with Merge OK (not a historical role audit).

An unreadable parent/task journal or malformed, inconsistent Risk history
aborts collection and skips the cycle. Security history never falls back to
updatedAt. A Feature created approved uses createdAt if no approval transition
is present. Keep the API journal language English; localized journal support
and historical membership auditing are not provided by this parser.

Everything the conductor writes lives under `[conductor] state_dir`:

| Path | Content |
|---|---|
| `watch.log` | One JSON object per planned/applied change (time, rule, target, key, field, old, new, reason). |
| `packets/` | The exact prompt each Spark run received (`task-<id>-<attempt>.md`, `review-<id>.md`, `test-<id>.md`). |
| `logs/` | Per-run worker output (`run-<id>-<attempt>.log`, `review-<id>.log`, `test-<id>.log`). |
| `runs.jsonl` | One JSON line per run (see below). |
| `worktrees/` | Scratch git checkouts for runs; review/test ones are removed afterwards. |

## How to read `runs.jsonl`

Each line has `task`, `kind` (`build`/`review`/`test`/`fix`), `size`,
`started`/`ended` timestamps, `duration_s`, `outcome` (`success`,
`failed`, `timeout`, `stalled`, `review-pass`, `review-changes`, …) and
`cost_usd`. For tuning the time limits, don't eyeball it:

```text
bin/opl-conductor runs
```

prints the median and max duration per kind and size plus the
timeout/stall count, e.g. `build size S: n=12 median=74.0s max=180.0s`.
Raise a limit when the median creeps towards it; investigate when
timeouts grow.

## Attempt ceilings and fresh retries

Review and test runs that end with no result (a crash, a missing final
line) get **one retry**; the count lives in `<state_dir>/attempts.json`
and survives restarts. After that the item is out of automatic runs: a
task moves to Blocked, a feature gets one "needs lead" comment and then
stays quiet.

A run that concludes with a result (review Pass/Changes, test Pass/Fail)
clears its count — only result-less runs count.

To give an item a fresh pair of attempts, move it in the tracker
(any status change works). The conductor compares the ceiling hit time
with the item's `status_since`: a move since the ceiling clears the count
and the notification, and the next cycle starts a run. Anything the
conductor cannot compare stays blocked.

## How to stop it safely
- Foreground loop: `Ctrl-C`. It stops immediately — changes are applied
  one by one, so whatever is already written stays consistent and the
  rest simply doesn't happen. Background Spark runs are abandoned
  mid-flight; their branches, packets and logs stay on disk for
  inspection, and the next start begins fresh runs.
- Prefer `--once` for a single supervised cycle instead of leaving the
  loop running unattended.
- Never delete `<state_dir>` while the conductor runs; the packets, logs
  and worktrees under it are its working memory.
