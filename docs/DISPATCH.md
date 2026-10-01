# Who starts work: dispatch audit and design (issue #10)

## What works today

| Model | Who starts its tasks | How |
|---|---|---|
| **Spark** | The conductor | The Spark runner picks Ready tasks assigned to `spark` (builds), In review tasks with Changes requested (reworks), reviews and tests assigned to it, and starts the single `[runner] command` (the sandbox adapter) per run. It never works on Private projects. |
| **Claude** | A person's session | Claude reads its queue (Ready and In review items assigned to it) through the MCP and works there. The conductor never starts a Claude run. |
| **Codex** | A person's session | The same, with its own MCP token. |

What the conductor does for Claude and Codex tasks: sets Ready when the
feature is approved and predecessors are merged, enforces the rules, merges
reviewed PRs, and raises `Reassign` after a task waits three days in Ready or
In review.

## What does not work, and why it is deliberate

The runner selects work by `assignee == "spark"` and has one runner command.
There is no per-assignee model dispatch. That is a choice, not a gap to
close quietly: Claude and Codex run in interactive sessions with the owner's
own tools and checkouts, and they work on Private projects, where a
conductor-spawned process must never read the code. Giving the conductor a
route to start them would put Private code, long-lived credentials and an
unbounded process lifecycle behind an automatic trigger.

## Minimal design if a second automatic model is ever wanted

Do this only after a decision card from the owner. Smallest safe change:

1. `[runner.models.<login>]` tables, each with its own `command`, its own
   `worker_env` allowlist and `visibility = "Public"`. Today's `[runner]`
   becomes the table for `spark`.
2. The runner selects candidates for each configured login (the existing
   filters stay: Public project, no violation, approved feature, predecessors
   merged) and starts that login's command.
3. Role attribution and the approval check at spawn are unchanged: the check
   already re-reads the task live before every worker start.
4. A login with `visibility = "Public"` is refused on a Private project in
   code, not by configuration alone.
5. No automatic merge change: merges keep needing Pass from the reviewer, a
   full SHA at the PR head, green checks, and Merge OK for Medium/High.

Acceptance examples (synthetic contract tests, scripted workers):

- Two configured logins, one Ready task each: each command starts once with
  its own environment, and neither sees the other's allowlist.
- A task assigned to a configured login in a Private project: no run starts.
- The feature is un-approved after the packet is built: no worker starts
  (live check at spawn).
- A login without a table: its Ready tasks are left alone, as today.
