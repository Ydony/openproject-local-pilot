# Live acceptance test suite (T5.1)

## Aim

Prove that the modules talk to each other as designed, on the real systems:
**OpenProject ↔ conductor ↔ GitHub ↔ LLMs (Claude, Codex, Spark)**. The suite
is run like a real project on a fake one. It does not retest code logic that
the offline suite already covers (see "Cited, not rerun").

**Pass rule.** A test case passes only when its result is **visible** where the
owner looks: on the OpenProject work package (Activity, fields, relations, a
saved view) and, for code, on GitHub (branch, commit, PR, checks). A log line,
a script's exit code or an LLM saying "done" is never a pass. If the toolkit
does not make a step visible, the case **fails** and becomes a fix task.

## Who does what

| Actor | Role in the suite |
|---|---|
| Owner | Watches OpenProject. Approves features, ticks Merge OK, decides deploys, and tells Claude or Codex "work now". Grants access when asked. Never runs test steps. |
| Claude | Lead and tester: sets up, creates the fake project with the owner, reviews, runs test cases, records results. |
| Codex | Builder and reviewer on its assigned tasks; acts only when the owner tells it to work. |
| Spark | Builder, Low-risk reviewer and feature tester for the Public project; started only by the conductor. |
| Conductor | Runs continuously in live mode for the sandbox project only; moves the stages it owns, starts Spark, merges when allowed. |

Claude and Codex are **not** started automatically (DESIGN §5). When the owner
says "work", the model reads its own queue (Ready and In review items assigned
to it) through its own OpenProject connection and acts on it.

## Fixtures

- **OpenProject project "Conductor Acceptance Sandbox"** (Public), configured by
  `bin/opl-configure` with the agreed model: Epic → Feature → Task, the task and
  feature stages, the Owner / Model / Conductor roles and all custom fields.
- **OpenProject project "Private Sandbox"** (Private), for the Spark refusal case.
- **OpenProject project "Acceptance Results"** (not managed by the conductor):
  one Feature per test group below and **one Task per case under it** (subject
  `<ID> <case>`), so every case has its own visible state. The tester who runs
  a case posts on **that case's task** (`<ID> PASS|FAIL`, what was seen, link)
  and sets its **Test result**. When a group's cases all pass, the tester sets
  the group feature's Test result to Pass and the owner (or the lead on the
  owner's instruction, labelled) moves it to Done; the owner closes the epic
  after the last group. The owner reads the whole suite's state here. Every
  testing model must be a member of this project and have it in its MCP
  allowlist.
  *(Changed after the 2026-09 run: that run used checklists in the feature
  descriptions, which OpenProject cannot tick, and named nobody to close the
  groups, so the scoreboard stayed "Proposed" although all cases passed.)*
- **GitHub repository `<owner>/opl-acceptance-sandbox`**: public, synthetic only,
  one required check (a trivial CI job), and two no-op workflows used as the
  test and production deploy signals.
- Service users `claude`, `codex`, `spark`, `conductor`, each with its own API
  token held only in its own environment; the owner's account has the Owner role.

No real project data, names or credentials appear in any fixture.

## Test cases

Each case: **ID · tester · what happens · visible in OpenProject · visible on
GitHub**. "Tell X" means the owner tells that model to work; nothing else is
done by hand.

### S — Setup and connections (Claude, owner grants access)

| ID | What happens | Visible in OpenProject | Visible on GitHub |
|---|---|---|---|
| S1 | Backup, then `opl-configure --dry-run`, then real, then real again | Types, stages, roles, fields present; second run reports no changes | — |
| S2 | Service users created; owner creates their tokens | Users listed under Administration → Users; memberships per project | — |
| S3 | Claude and Codex connect through their MCP servers; each reads its queue | Each model posts "connected as <user>" on a setup task, authored by its own user | — |
| S4 | Conductor starts in watch mode, then live mode for the sandbox only | Conductor posts its start comment on the setup feature; nothing else changes in watch mode | — |
| S5 | Spark launch canary through the sandboxed worker user | Canary result comment on the setup task | — |
| S6 | Synthetic repo, required check and deploy workflows exist | Repo link on the sandbox project | Repo, branch protection, workflows |

### P — Planning with the owner (Claude)

| ID | What happens | Visible in OpenProject | Visible on GitHub |
|---|---|---|---|
| P1 | Owner says "create tasks"; Claude asks the owner questions, then creates Epic A → Features A1, A2 → Tasks with assignee, reviewer, risk, size, Done-when | Hierarchy, all fields filled, authored by `claude` | — |
| P2 | Claude edits a field and adds a precedes/follows relation | Change and relation in Activity | — |
| P3 | Claude sets Now / Next / Later versions | Roadmap page groups the features | — |
| P4 | Claude comments on a task and another model answers it | Both comments, each by its own user | — |
| P5 | Claude parks a throwaway feature and deletes a throwaway task | Parked in Activity and its tasks never become Ready; the deleted task is gone from every view and no work starts for it | No branch for either |

Fake work used below (all Public, synthetic):
- **A1.1** (Spark, Low, reviewer Spark review run): "Confirm you read this task:
  write `YES` to `yes.txt`, comment, open a PR."
- **A1.2** (Claude, Low, reviewer Spark review run, follows A1.1): "Write the
  status you see on this task, and when you saw it, to `task-status.txt`."
- **A2.1** (Spark, Medium, reviewer Claude): a one-line README change.

Codex substitution (2026-09-27 run): Codex was out of credits, so its build
and review turns moved to Claude and Spark as above. Its OpenProject
connection was verified live (S3). Repeat A1.2 and A2.1 with Codex as
builder/reviewer when it is available; the report lists them as Not run.

### F — The full frame (everyone)

| ID | What happens | Visible in OpenProject | Visible on GitHub |
|---|---|---|---|
| F1 | Owner approves Feature A1 | Approval by the owner in Activity; conductor moves A1 to Building and A1.1 to Ready | — |
| F2 | Conductor starts Spark on A1.1 | A1.1 In progress (by `spark`), then In review with PR link and result comment | Branch, commit with `yes.txt` = `YES`, PR |
| F3 | Spark review run reviews A1.1 | `reviewed: <sha>` comment, Review result Pass, by the reviewer | PR review/comment at that SHA |
| F4 | Checks green → conductor merges | A1.1 Merged (by `conductor`) | PR merged at the reviewed SHA |
| F5 | A1.2 becomes Ready only now; owner tells Claude to work | A1.2 Ready after A1.1 Merged; In progress → In review with PR link, by `claude` | Branch, `task-status.txt`, PR |
| F6 | Spark review run reviews A1.2 | Review result Pass with `reviewed: <sha>`, by `spark`; A1.2 Merged | PR merged |
| F7 | All A1 tasks merged → A1 In test; Spark tests the Done-when list | A1 In test; test report comment; Test result set; Needs me shows "Decide deploy" | — |
| F8 | Owner decides deploy; deploy workflows run | A1 In production, then Needs me "Confirm done"; owner confirms → Done; Epic A progress updates | Deploy workflow runs |
| F9 | Medium task A2.1: Spark builds, owner tells Claude to review | Needs me shows "OK merge"; merges only after owner ticks Merge OK | PR merged only after Merge OK |

### R — Review cycles (Codex, Claude)

| ID | What happens | Visible in OpenProject | Visible on GitHub |
|---|---|---|---|
| R1 | Reviewer sets Changes requested with notes | Task back to In progress with notes | Review comment |
| R2 | Builder fixes and pushes; review again | New `reviewed: <sha>` on the new head; Pass | New commit on the PR |
| R3 | A push after Pass | Conductor clears Review result with the reason; task needs a fresh review | Head moved past reviewed SHA |

### G — Gates and refusals (Claude runs, owner watches)

| ID | What happens | Visible in OpenProject | Visible on GitHub |
|---|---|---|---|
| G1 | Feature B1 left Proposed with a Spark task | Task stays Draft; Needs me shows "Approve" | No branch |
| G2 | Spark task in Private Sandbox | Blocked with reason; Needs me "Unblock" | No branch |
| G3 | A model tries to approve a feature / tick Merge OK | Refused or blocked as "Unauthorised approval"; the tester's comment records it | — |
| G4 | Reviewer = builder | Blocked with the rule's reason | — |
| G5 | Anything Blocked | Project shows At risk | — |

### V — Visibility and cost (Claude)

| ID | What happens | Visible in OpenProject | Visible on GitHub |
|---|---|---|---|
| V1 | Owner opens Needs me | Every open action from F/G above, and nothing else | — |
| V2 | Owner opens Feature pipeline and All projects | Features by stage with progress | — |
| V3 | Spark and model usage recorded | Actual tokens/cost on A1.1, A1.2, A2.1, or explicit "unknown" (never 0); Feature and Epic roll-ups | — |

## Cited, not rerun

Covered by the offline suite (merged in #6) and cited by revision in the report:
timeouts, stalls, retry ceilings, cancellation and restart; the race guards
(changes during preparation); cost-attribution maths. Covered live on
2026-09-27 by T0.L: start/stop/status, host binding, backup and restore into a
disposable copy.

## Order and stop points

1. S1–S6. Stop if any connection fails; fix before continuing.
2. P1–P5 with the owner.
3. F1–F9, then R1–R3, then G1–G5, then V1–V3.
4. Claude writes the report into "Acceptance Results": every case Pass/Fail
   with links. Any Fail gets a fix task; the suite is rerun for the failed
   group only.
5. Close the scoreboard (see Fixtures): group features Done, epic Closed.
   The suite is not finished while any scoreboard item is still open.

No real project is touched. Live mode is on only for the sandbox projects and
only while the suite runs. After a full pass: a Superpowers session plans
moving the first real project (#22).
