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
```

Every step is idempotent: a second run prints nothing and changes nothing.
Exit code is non-zero on any failure, with the failing step named.

## What it changes, in order

1. Renders the admin Ruby script from the model and runs it with
   `bin/opl-compose exec -T web bundle exec rails runner` (statuses, types
   enabled in every project, roles with exact permissions, per-pair
   workflows, custom fields activated for their types, project fields, and
   the status-based progress mode).
2. `apply_api`: missing projects (+ Repo/Visibility), users (bot accounts
   get a random discarded password; API tokens stay manual), memberships
   (owner everywhere, `all` users everywhere, spark Public-only; spark
   memberships in Private projects are removed), versions, and the
   Maintenance epic (Open) + feature (Approved) per project.
3. `apply_views`: saved queries (global once, per-project copies), matched
   by name and scope and updated in place; pins Needs me to My page.

## Failure recovery

- `compose cp/exec` failed: Docker Engine (WSL 2) not running, or the
  runtime dir missing — run `bin/opl-setup`, `bin/opl-start`, retry.
- `role/type/status ... not found`: the admin script did not apply — read
  its error above and retry (it is safe to re-run).
- API shapes changed upstream: payload keys live in small mapping helpers
  (`opl/configure/views.py`, `apply_api.py`); adjust and re-run.
