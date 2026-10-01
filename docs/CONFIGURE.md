# Configure the tracker (`opl-configure`)

Applies `config/pm-model.toml` (statuses, types, workflows, roles, fields)
to the localhost pilot, then creates projects, users, memberships, versions,
the Maintenance epic/feature per project, and the saved views.

> Statuses, types, roles and workflows apply **instance-wide**: run this
> only against a **dedicated** OpenProject instance that holds nothing
> else. Back up first (`bin/opl-backup`).

## Prerequisites

- The pilot is set up and running (`bin/opl-setup`, `bin/opl-start`).
- Python 3.11+ (`bin/opl-configure` picks `python3`, `py -3`, then `python`).
- `~/.config/opl/opl.toml` exists (copy `config/opl.example.toml`; override
  `OPL_CONFIG_DIR` to move it). Token entries are environment variable
  *names* — export the variables with the real tokens, e.g.
  `OPL_TOKEN_ADMIN`. Secrets are never printed.
- The owner account exists (first sign-in as `admin`), and its API token is
  in the admin env var.

## Run

```sh
bin/opl-configure --dry-run   # plan only: prints the admin-script summary
                              # and every planned action as "would ..."
bin/opl-configure             # 1. rails admin script via web container
                              # 2. projects/users/memberships/versions
                              # 3. saved views + Needs-me pin on My page
bin/opl-configure --repair-my-page  # same as above, but first recreates a
                              # hidden open + assigned/created-by-me query
                              # for any My page widget whose query is gone
```

Every step is idempotent: a second run prints nothing and changes nothing.
Exit code is non-zero on any failure, with the failing step named.

## What it changes, in order

1. Renders the admin Ruby script from the model and runs it with
   `bin/opl-compose exec -T web bundle exec rails runner` (statuses, the
   P0-P3 priority renames (seeded Immediate/High/Normal/Low renamed in
   place, keeping positions, colours and default), types
   enabled in every project, roles with exact permissions, per-pair
   workflows, custom fields activated for their types, project fields, and
   the status-based progress mode).
2. `apply_api`: missing projects (+ Repo/Visibility), users (bot accounts
   get a random discarded password; API tokens stay manual), memberships
   (owner everywhere, `all` users everywhere, spark Public-only; spark
   memberships in Private projects are removed), versions, and the
   Maintenance epic (Open) + feature (Approved) per project, plus a
   standing **Project setup** feature (Approved, under the Maintenance
   epic) whose Done-when checklist mirrors `bin/opl-project-check`. It is
   created once, never duplicated, and the conductor leaves it alone like
   Maintenance. After creating a project, `opl-configure` prints the next
   step: run `bin/opl-project-check <project key>`. A project counts as
   migrated only when that check passes.
3. `apply_views`: saved queries (global once, per-project copies), matched
   by name and scope and updated in place; pins Needs me to My page.
   The pin keeps each existing widget's `id` (otherwise OpenProject
   replaces the widgets and deletes their owned queries) and then
   verifies every work-package widget's `queryId` still resolves
   (`GET /api/v3/queries/<id>`); it fails loudly otherwise.

## Per-project build/test readiness (`[[project]]`)

Each project may declare what a worker needs to build and test it
(issue #42). All three fields are optional; absent (or empty) means
undeclared:

```toml
[[project]]
key = "demo-public"
name = "Demo public project"
repo = "example-owner/demo-public"
visibility = "Public"
has_test_env = false
prod_signal = { kind = "environment", name = "production" }
runtime = ["node@22"]  # list of runtime specs
setup = "npm ci"       # shell command, run from the checkout
test = "npm test"      # shell command, run from the checkout
```

- `runtime`: must be a list of non-empty strings. Entries outside
  `KNOWN_RUNTIMES` in `opl/settings.py` (currently `node@20`,
  `node@22`, `python@3.11`–`3.13`) are allowed but reported via
  `opl.settings.unknown_runtimes()` so a missing Node.js (or similar
  gap) surfaces early instead of failing a run late.
- `setup` / `test`: must be strings holding shell commands. They run
  from the project checkout; empty means undeclared. Wrong types fail
  fast with a `SettingsError` naming the project and field.

### Provisioning the declared runtimes (issue #45)

The conductor installs each declared `runtime` itself (Node.js only) under
`[conductor] runtimes_dir` (default `/opt/opl-runtimes`), verifying the
publisher's checksum, and gives Spark runs the installed `bin` directory plus
the project's `setup` command. See SANDBOX.md, "Runtimes", for the one-time
owner steps (create the directory, install launcher version 3). Until a
project declares a runtime, nothing changes for it.

```toml
[conductor]
runtimes_dir = "/opt/opl-runtimes"   # optional; absolute path
```

## Checking that a project is ready (issue #43)

```sh
bin/opl-project-check <project-key>     # or --all
```

Read-only. Prints `PASS`, `FAIL`, `WARN` or `SKIP` per item and, for every
`FAIL`/`WARN`, the exact fix. Exit status 1 when anything fails.

- OpenProject: the project exists; claude, codex and conductor are members
  (spark too, but only on Public projects, and never on a Private one).
- GitHub (conductor token): repo readable; push permission (read from the API,
  nothing is written); Actions, Deployments, Statuses and branch protection
  readable; a PR check exists (required checks, or a workflow on
  `pull_request`); token expiry (warning under 14 days, failure once expired).
- Spark (Public projects): `local_repo` exists, is clean and fetchable; the
  sandbox launcher answers; declared runtimes are installed; a smoke run
  executes the declared `setup` and `test` as the worker account in a fresh
  clone with no model call (launcher `probe` mode, version 3: reinstall the
  launcher as in SANDBOX.md).
- Claude/Codex (Private projects): `local_repo` exists and each declared
  runtime is on `PATH`.

The MCP allowlist is not checked: the toolkit has no configured location for
it.

## Failure recovery

- `compose cp/exec` failed: Docker Engine (WSL 2) not running, or the
  runtime dir missing — run `bin/opl-setup`, `bin/opl-start`, retry.
- `role/type/status ... not found`: the admin script did not apply — read
  its error above and retry (it is safe to re-run).
- API shapes changed upstream: payload keys live in small mapping helpers
  (`opl/configure/views.py`, `apply_api.py`); adjust and re-run.
