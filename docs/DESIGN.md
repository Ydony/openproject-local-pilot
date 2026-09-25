# Multi-LLM project management on OpenProject: design

- **Date:** 2026-09-24
- **Status:** Approved by the owner; this is the canonical copy
- **Build plan:** [BACKLOG.md](BACKLOG.md). Every task names the section of this file it implements.
- **Toolkit scope:** [SPEC.md](../SPEC.md) covers the operator toolkit (phase 0). This file covers the whole system.

## Goal

One local OpenProject instance where the owner can see, across every project,
what is planned, being built, in test, live and done. Claude, Codex and Spark
plan, pick up, build and review the work under rules that are enforced rather
than just written down. The owner approves features and makes deploy
decisions, and nothing else needs them.

## Decisions at a glance

| Topic | Decision |
|---|---|
| Owner control | Owner approves each **feature**. Models run everything inside an approved feature. |
| Hierarchy | **Epic → Feature → Task** |
| Stages | Split: **tasks track the build, features track the release** |
| Assignment | Model chosen **at planning**, visible at approval; no automatic handoff |
| Reviews | **By risk** |
| Test check | A model walks through the feature's "Done when" list; the owner decides the deploy |
| Screens | **Needs me, Feature pipeline, Roadmap, All projects** |
| Edition | OpenProject **Community** (free); design around its gaps |
| Feature details | Plain language in OpenProject; technical spec in the repo, linked |
| Architecture | **OpenProject is the single source of truth, plus a small "conductor" program** |
| Spark runs | Time limit per run based on task size |

## 1. Structure

### Projects

One OpenProject project per product. Each project has two project fields:

- **Repo**: the repository URL
- **Visibility**: `Public` (public or fake-data repo) or `Private`

Visibility is the main routing rule (section 3).

### Item types and nesting

| Type | Meaning | Parent |
|---|---|---|
| **Epic** | A large area, e.g. *User module* | None (top level) |
| **Feature** | A shippable unit the owner approves, e.g. *Registration* | Must be an Epic |
| **Task** | One model's unit of work, e.g. *Build signup form* | Must be a Feature |

Every project has a standing **Maintenance** epic containing a **Maintenance**
feature. Bugs and chores go under it, so no item is ever left without a parent. The
conductor flags any item that breaks these nesting rules.

### Users

| User | Role | Project membership |
|---|---|---|
| Owner | Owner (admin) | All projects |
| Claude | Model | All projects |
| Codex | Model | All projects |
| Spark | Model | **Public projects only** (see below) |
| Conductor | Automation | All projects |

- **Assignee** (built-in) is the model doing the task.
- **Reviewer** is a user field naming a different model, or the owner.
- Spark is a member of Public projects only, so it cannot read Private work items
  through OpenProject at all. Its contributor terms allow training on what it
  receives, so private content must never reach it by any route.

### Fields

| On | Field | Values |
|---|---|---|
| Project | Repo | URL |
| Project | Visibility | Public / Private |
| Feature | Priority (built-in) | P0 to P3 |
| Feature | Risk | Low / Medium / High |
| Feature | Spec link | Link to the technical spec in the repo |
| Feature | Models | Multi-select Claude / Codex / Spark; maintained by the conductor |
| Feature | Bucket | Version: Now / Next / Later |
| Feature | Test result | Pass / Fail; set when the tester's report is posted |
| Task | Assignee (built-in) | Claude / Codex / Spark |
| Task | Reviewer | User |
| Task | Size | S / M / L |
| Task | Risk | Low / Medium / High |
| Task | PR link | URL |
| Task | Review result | Pass / Changes requested; set by the reviewer |
| Task | Merge OK | Checkbox; owner only; used for Medium- and High-risk tasks (pilot, TH.D) |
| Any | Needs you | Checkbox; set by the conductor |
| Any | Action | Approve / OK merge / Decide deploy / Unblock / Reassign / Confirm done; set by the conductor |

The **feature description** uses a fixed template written for the owner:

```
Why:           <the problem this solves, one or two sentences>
What you'll see: <what changes on screen or in behaviour>
Done when:     <checklist the tester walks through>
```

Dependencies use OpenProject's built-in **precedes / follows** relations.

## 2. Stages

### Feature stages

| Stage | Meaning | Moved there by |
|---|---|---|
| Proposed | A model has drafted the feature and its tasks | Any model |
| Approved | Owner said yes; work may start | **Owner only** |
| Building | At least one task is In progress | Conductor |
| In test | All tasks merged, and the test deploy has been seen if the project has a test environment | Conductor |
| In production | The production deploy has been seen | Conductor |
| Done | Owner confirmed it works live | **Owner only** |
| Parked / Rejected | Not now / not ever | Owner only |

**Projects without a test environment still use In test.** The tester runs the
walkthrough against a local build of the main branch.

### Epic status

OpenProject requires every item to have a status. Epics use only **Open** and
**Closed**. The owner closes an epic once all its features are Done or Rejected.

Production deploys stay the owner's decision. The conductor only records that
one happened.

### Task stages

| Stage | Meaning | Moved there by |
|---|---|---|
| Draft | Planned inside a Proposed feature | Planner model |
| Ready | Feature approved **and** every predecessor task merged | Conductor |
| In progress | The assigned model has started | The assignee (the conductor checks it really is the assignee) |
| In review | Work finished; PR link attached | The assignee |
| Merged | Review passed and the PR merged | Conductor |
| Blocked | Stuck; a reason is required | Anyone |

- A failed review (`Review result = Changes requested`) moves the task back to
  In progress. The reviewer makes that move and leaves notes.
- Epics have no stages of their own. They show a progress bar built from their features.

### Enforcement

OpenProject workflows (allowed stage changes per role and type) enforce the
"moved there by" columns. The conductor is its own user, so every automatic
change is attributed to it.

### Progress values

Progress is calculated from each task's stage, using OpenProject's status-based
progress mode:

| Draft | Ready | In progress | Blocked | In review | Merged |
|---|---|---|---|---|---|
| 0% | 0% | 30% | 30% | 70% | 100% |

## 3. Who builds and who reviews

### Who builds

The planner sets the assignee on each task. The conductor refuses to start any
task whose assignment breaks these rules, and flags it.

| Visibility | Builder | Rule |
|---|---|---|
| Private | Claude or Codex | Spark is **never** allowed (hard block) |
| Public | **Spark by default**, including hard tasks | Claude or Codex only when the owner chooses |

- **Planning** (drafting features and tasks) is done by whichever model the owner
  is talking to.
- **Stale work:** a Claude or Codex task sitting in Ready or In review for 3 days
  appears in Needs me with Action *Reassign*. Nothing is reassigned automatically.

### Who reviews

The reviewer is always a different model from the builder, with one exception:
for Low-risk Public work, a **separate Spark session** reviews Spark's work.

| Risk | Public | Private |
|---|---|---|
| Low: self-contained, easy to undo | Separate Spark review run plus automated checks | Claude and Codex review each other |
| Medium: shared code or something users see | Claude or Codex, **plus owner Merge OK** (pilot) | Claude and Codex review each other, **plus owner Merge OK** (pilot) |
| High: login/security, payments, deleting data, database changes, production setup | Claude or Codex, **plus owner Merge OK** | Same |

- The planner sets the risk. The owner sees it when approving and can raise it.
- **Merging:** the conductor merges a PR once `Review result = Pass`, the
  repository's required checks are green, and (for Medium and High risk)
  `Merge OK` is ticked. *Owner decision TH.D (2026-09-25):* during the
  pilot, Medium needs Merge OK as well as High. Letting Medium merge on
  review alone, as first designed, is one line in
  `opl/conductor/rules/merge.py` (`NEEDS_OWNER_OK`) once live safety is
  proven.
- **What "checks green" means:**
  - When branch protection lists required checks, each one must be
    present and passing: a check run from the named app, or a legacy
    status of that context. Other checks don't count, as on GitHub.
  - Without readable or non-empty requirements, every check run and
    every legacy status must pass, and at least one must exist.
  - All pages are read; an incomplete read is never green.
- **Who may approve:** both approvals are plain fields, so the conductor
  checks the activity journal. `Merge OK` counts only if its latest change
  was made by a member with the **Owner** role in that project, and
  `Review result = Pass` only if its latest change was made by the task's
  **Reviewer**. Anyone else's approval blocks the task ("Unauthorised
  approval"). To recover, clear the field and have the owner or reviewer
  set it again. Models can leave Blocked only to In progress; Ready is
  set by the conductor alone.
- **A review covers one commit:**
  1. Before setting `Review result = Pass`, the reviewer posts a comment
     containing the line `reviewed: <full 40-character PR head SHA>`.
     Spark review runs do this themselves.
  2. The conductor merges only while the PR head still equals that SHA,
     and asks GitHub to merge exactly that commit (GitHub refuses if the
     head moved in between).
  3. If the head has moved since, or the Pass has no such line, the
     conductor clears Review result with a comment saying why, and the
     task is reviewed again.

### Tester role

In In test, a tester walks through the feature's **Done when** list against
the test environment (or a local build of main), posts the report as a comment
on the feature, and sets its **Test result** field. The tester is Spark for Public projects and Claude or
Codex for Private ones. "Tester" is a role rather than a fixed model, so a
future dedicated testing agent can take it over.

## 4. Screens

All four are OpenProject's standard screens saved as named views. There is
no custom interface.

### Needs me

Shown across all projects and also placed on the owner's "My page".
OpenProject filters cannot combine conditions with OR, so the conductor ticks
**Needs you** and sets **Action** instead:

| Action | When |
|---|---|
| Approve | A feature is Proposed |
| OK merge | A High-risk task has passed review |
| Decide deploy | A feature is In test and its Test result is set |
| Unblock | Anything is Blocked, including rule violations and Spark timeouts |
| Reassign | A Claude or Codex task has been waiting 3 days |
| Confirm done | A feature is In production |

Filter: `Needs you = true`, sorted by priority.

### Feature pipeline

Per project, plus one view across all projects. Features grouped by stage, with
columns Epic, Priority, Risk, total progress, and Models.

### Roadmap

- Features are grouped into **Now / Next / Later** (OpenProject versions) per project. The Roadmap page shows each group with a progress bar.
- A Gantt view draws the precedes/follows arrows.
- The views show order and leave dates out, because estimated dates for AI-built work are guesses.

### All projects

All active features across projects, grouped by project, with stage and
progress. OpenProject's project list sits on top with its On track / At
risk status. The conductor sets **At risk** automatically when a project has anything
Blocked.

## 5. Conductor

### Shape

A small Python program running on the same machine as OpenProject. Both are
localhost-only. It runs a **check-and-fix loop** about once a minute:

1. Read the current state from OpenProject and GitHub.
2. Work out what the state should be according to the rules below.
3. Apply only the differences.

The loop is idempotent: two runs in a row change nothing on the second. After a
crash, the next loop simply catches up. GitHub cannot reach localhost, so the
conductor polls rather than receiving webhooks.

### Rules

Each rule is a separate module with one job.

| Rule | Does |
|---|---|
| Enforce | Checks nesting, blocks Spark in Private projects, requires reviewer ≠ builder and a reviewer that matches the risk table. A violation becomes Blocked with the reason plus *Unblock* in Needs me. It is never silently "fixed". |
| Stages | Moves only the stages section 2 gives the conductor: Ready, Building, Merged, In test, In production. It reads merges and deploys from GitHub. |
| Screens | Maintains Needs you, Action, Models, and the project At-risk marker. |
| Merge | Merges when the conditions in section 3 are met. |
| Spark runner | Starts Spark on Ready tasks, and on reviews and test checks, that are assigned to Spark (below). |

**Per-project configuration** lives outside every repo and holds:

- the repo
- the test deploy signal and the production deploy signal (for example a named GitHub Actions deploy workflow succeeding, or a GitHub deployment to a named environment)
- whether the project has a test environment

### Spark runner

For each Ready task assigned to Spark:

1. Create an isolated copy of the repository on a new branch.
2. Give Spark the task, its feature description, the spec link, the Done-when list, and Spark's own OpenProject access.
3. Enforce the cost cap and the time limit (below).
4. On success: commit, push, open a PR, attach the PR link, move the task to In review, and start the review (a Spark run for Low-risk Public work).

At most **2 Spark runs at a time**.

| Run | Time limit | Stall stop |
|---|---|---|
| Size S task | 20 min | no file change or output for 10 min |
| Size M task | 45 min | same |
| Size L task | 90 min | same |
| Review | 15 min | same |
| Test check | 20 min | same |

- **Timeout or stall:** no automatic retry. Partial work stays on the branch,
  and the task goes to Blocked with *Unblock* in Needs me. The options are give more
  time, split, or reassign.
- **Failure** (checks fail or a crash): one automatic retry with the error
  included. A second failure goes to Blocked.
- **Planning rule:** a task expected to exceed 90 minutes must be split before
  its feature can be approved.
- The conductor records every run's real duration and cost, so the limits can
  be tuned against real data.

### Claude and Codex

Claude and Codex are **not** started automatically in this version, because
they spend the owner's credits. When the owner opens a session and asks "what's
next", the model reads its own queue (Ready and In review items assigned to it)
from OpenProject.

### Model access to OpenProject

All three models use the same **community OpenProject MCP server**
(`openproject-ce-mcp` 0.4.1, pinned; see MCP.md), each logged in with its own token. The
server has write access to the tracker, so its code is reviewed and a version
pinned before use.

### Errors

- Every automatic **stage change and merge** leaves a comment on the item
  explaining why, e.g. *"Ready: feature approved, #12 merged."* Screen-field
  updates (Needs you, Action, Models, At risk) are left to OpenProject's own
  change history, so comments stay meaningful.
- If OpenProject or GitHub is unreachable, that loop is skipped and logged, and
  the next loop catches up.
- The conductor never changes a stage owned by the owner or a model.

### Secrets

The owner creates the five tokens: four OpenProject users plus GitHub. They are
stored outside every repository and never printed or logged. No model ever
sees another user's token.

## 6. Rollout and testing

### Build order

Each step must meet its "done when" before the next starts.

| Step | What | Done when |
|---|---|---|
| 0. Hosting | Fix the OpenProject pilot toolkit blockers: setup always aborting, database password mismatch, stop reported as unhealthy, restore touching live data paths | OpenProject starts reliably; a backup restores into a throwaway copy |
| 1. Setup as code | A script creates types, stages, workflows, fields, users, the four screens and the Now/Next/Later versions | A fresh OpenProject is rebuilt with one command. The owner creates the tokens. |
| 2. Model connection | Review and pin the MCP server; connect all three models | Each model can read and draft a task, and **cannot** approve a feature. Spark cannot see Private projects. |
| 3. Conductor, watch mode | All rules run but only **log** intended changes | A week of logs matches what should have happened |
| 4. Conductor, live + Spark runner | Real changes enabled; Spark runs with limits | Timeout, stall and retry behave as specified |
| 4b. Cost tracking | Price table, estimates, actuals from ccusage, roll-ups (§7) | Every task, feature and epic shows est. and actual cost; actuals match ccusage for a test session |
| 5. Pilot | One small feature in one Public project, Proposed to Done | Every stage passes with the owner only approving and deciding |
| 6. Switch-over | **One project at a time:** re-plan it with the owner in a Superpowers brainstorming session (epics, features with Why / What you'll see / Done when, sized tasks), get the owner's approval, load it into OpenProject, and have the owner check it. Nothing is copied over as-is. Then update the global instructions to point at OpenProject, and move the old PM system (`pm.py`, its data, its docs) into an `archive/` folder and close its tasks | Only after the pilot passes **and the owner decides**; every project has an approved plan; no worker run still depends on the old tooling |

### Testing

- **Conductor rules:** automated tests on made-up state. Each rule has
  given-state → expected-change cases, and every rule has an idempotency test
  (running twice changes nothing).
- **Permissions:** checked against a throwaway OpenProject project. Models
  cannot approve features, the conductor cannot mark anything Done, and Spark
  cannot read a Private project.
- **Spark runner:** tested with a fake Spark that hangs, goes silent, or
  fails, to prove the time limits and retry rules without spending anything.
- **Pilot:** step 5 is the end-to-end acceptance test.

## 7. Cost tracking

Every task, feature and epic shows an **estimated cost** and an **actual
cost** in USD. Every model is priced **as if paid per use**, including models
on a subscription, so costs are comparable across models.

| Piece | How |
|---|---|
| Prices | `config/prices.toml`: per model, the input, output, cache-read and cache-write price per 1M tokens, plus `billing` (api / subscription, for display only) and an `as_of` date. This is the only source of prices. |
| Estimate (task) | Its Size → the expected tokens for the assigned model (`config/estimates.toml`) × that model's prices. Computed by the conductor whenever Size or Assignee changes. |
| Actual (task) | The real tokens used in the task's sessions × our prices. Tokens come from **[ccusage](https://ccusage.com/)** (pinned version), which reads the local logs of Claude Code, Codex and OpenCode (Spark). ccusage's own prices are ignored. |
| Session → task | Every task is worked in its **own worktree folder, whose name contains the task id**. This applies to Spark runs (T4.1) and to Claude/Codex sessions the owner starts. The tools' logs record each session's folder, so the conductor matches sessions to tasks by folder. Delimited prefixes (never substrings): `task-T<id>-`, `fix-<id>-` and `review-<id>-` count toward **task** `<id>` (build, fix and review cost all belong to the task); `test-<id>-` counts toward **feature** `<id>`. Sessions the owner starts by hand must use a folder with one of these prefixes (e.g. `task-T12-…`). Matching lives in one function, `opl/usage.py: task_for_folder`. |
| Feature / Epic | Estimate = sum of their tasks'; actual = sum of their tasks' known actuals. Kept current by the conductor. |
| No actual known | The field stays empty. Never guessed. |

OpenProject's built-in cost module is **not** used: its API cannot write cost
entries. Plain number fields, written through the API, are simpler and still
total up in list views. Cost fields are screen fields: they change without
comments (§5).

## To verify during setup (unconfirmed assumptions)

These come from general knowledge of OpenProject and were not checked against
current documentation during the design session:

- Community Edition supports per-type workflows, status-based progress with
  roll-up, custom fields of the listed kinds, saved views, Gantt, and the
  Roadmap/versions page.
- Stage-driven drag-and-drop boards are Enterprise-only. The Feature pipeline
  therefore uses a grouped list.
- Whether a parent's roll-up progress includes its own stage value or only its
  children's.
- The community MCP server covers everything the models need (read, create,
  update, relations, comments) and respects per-user permissions.

## Out of scope (future)

- A dedicated low-cost testing agent taking over the Tester role
- Claude and Codex starting automatically within an owner-set credit budget
- A custom-designed dashboard on top of OpenProject
- OpenProject Enterprise
- Access from other machines or cloud agents
