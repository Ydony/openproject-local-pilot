# MCP server: review and pinned setup

How Claude, Codex and Spark read and write the tracker (DESIGN.md §5, "Model
access to OpenProject"). This file records the review (task T2.1). The
per-client "Connect" snippets follow in T2.2.

## Decision

**Use `openproject-ce-mcp` version 0.4.1, pinned.** Reviewed 2026-09-24
against the tagged source. Approved for all three models with the
configuration below.

| | |
|---|---|
| Project | [jtauschl/openproject-ce-mcp](https://github.com/jtauschl/openproject-ce-mcp) (formerly `jtauschl/openproject-mcp`, which now redirects) |
| License | MIT |
| Version | **0.4.1** (git tag `v0.4.1`, commit `b02b8e789139183b0b81885a85d9dddd9fcb0d57`, released 2026-09-22) |
| PyPI wheel | `openproject_ce_mcp-0.4.1-py3-none-any.whl`, sha256 `25868565cdcfbffb60bb2141ffa103b9c7a7f73e6c09c04b13a5a3a2d6abbd87` |
| PyPI sdist | `openproject_ce_mcp-0.4.1.tar.gz`, sha256 `c13f2f2ccdaf0170b468fa0f66d57957d79a710c31e69f4dbca8bb582fa8c527` |
| Runtime dependencies | `httpx>=0.27,<1`, `mcp>=2,<3` (Python ≥ 3.10) |
| Install | `uvx openproject-ce-mcp==0.4.1` (no persistent install), or `pipx install openproject-ce-mcp==0.4.1` |

Upgrading is a new review. Changing the pinned version is a Claude/Codex
task, not something a worker does.

## What was checked

| Check | Finding |
|---|---|
| Network calls | Only to `OPENPROJECT_BASE_URL`. The only other hosts in `src/` are example and placeholder strings, plus one GitHub docs link printed by the setup helper. No telemetry or analytics code. |
| Token handling | Read from the `OPENPROJECT_API_TOKEN` environment variable. Sent as HTTP Basic `apikey:<token>`, the same scheme our own client uses. No logging of the token or auth headers found. It warns when a remote base URL uses plain `http://`. |
| Process execution | `subprocess` appears only in `setup_cli.py`, the optional interactive installer: `uv sync`, `venv`, `pip install -e`, `git` checks. The server path (`openproject-ce-mcp`) runs no subprocesses. We don't use the setup helper. |
| Write safety | Every mutation is two calls: a preview, then the same call with `confirm=true`. There is no way to skip the preview. |
| Scope limits | Project allowlists `OPENPROJECT_READ_PROJECTS` / `OPENPROJECT_WRITE_PROJECTS` are fail-closed: empty means no access, and `*` must be explicit. Admin read/write, personal write and user-schedule write are off by default. Attachment uploads are disabled unless `OPENPROJECT_ATTACHMENT_ROOT` is set, and even then refuse credential files. |
| Coverage for our needs | `create_work_package` / `update_work_package`, including custom fields, parent and version; `add_work_package_comment`; `create_work_package_relation`; `list_work_packages` with `custom_field_filters`; and `get_project_work_package_context`, which returns the writable schema and allowed values. Everything the planner, builder, reviewer and tester roles need. |
| Maintenance | Active: 0.4.0 on 2026-09-12, 0.4.1 on 2026-09-22. A security policy with private reporting. Secret-scan and test CI workflows. |

**Limits of this review:** it covered the files listed above, grep audits of
all of `src/`, and the security model. It was not a line-by-line read of all
~160 tools. Defence in depth comes from OpenProject itself: every model
user's role (DESIGN.md §1–§2, `config/pm-model.toml`) blocks what the role
may not do, whatever this server exposes. For example, the Model role has no
delete permission, so `delete_work_package` fails for models.

## Required configuration

Each model runs its own server process with its **own token**. The token is
set in that model's environment by the owner and never written into a
committed file.

**Shared settings (all three models):**

```
OPENPROJECT_BASE_URL=http://127.0.0.1:8080
OPENPROJECT_API_TOKEN=<from the model's own env var, see T2.2>
OPENPROJECT_ENABLE_WORK_PACKAGE_READ=true
OPENPROJECT_ENABLE_WORK_PACKAGE_WRITE=true
OPENPROJECT_ENABLE_PROJECT_READ=true
OPENPROJECT_ENABLE_VERSION_READ=true
OPENPROJECT_ENABLE_MEMBERSHIP_READ=true
OPENPROJECT_ENABLE_PROJECT_WRITE=false
OPENPROJECT_ENABLE_VERSION_WRITE=false
OPENPROJECT_ENABLE_MEMBERSHIP_WRITE=false
OPENPROJECT_ENABLE_BOARD_WRITE=false
OPENPROJECT_ENABLE_MEETING_WRITE=false
OPENPROJECT_ENABLE_ADMIN_READ=false
OPENPROJECT_ENABLE_ADMIN_WRITE=false
OPENPROJECT_ENABLE_PERSONAL_WRITE=false
OPENPROJECT_ENABLE_USER_SCHEDULE_WRITE=false
```

Leave `OPENPROJECT_ATTACHMENT_ROOT` unset, which disables uploads.

**Project allowlists (the important difference):**

| Model | `OPENPROJECT_READ_PROJECTS` / `OPENPROJECT_WRITE_PROJECTS` |
|---|---|
| Claude | Every project identifier, listed explicitly (or `*`) |
| Codex | Every project identifier, listed explicitly (or `*`) |
| Spark | **Only Public project identifiers, listed explicitly. Never `*`.** |

Spark is already excluded from Private projects by OpenProject membership
(T1.4). The allowlist is the second, independent layer, so a mistake in
either one alone still keeps private content away from Spark.

## Connect (T2.2)

One server process per model, each with its **own token** and the **full**
"Required configuration" block. openproject-ce-mcp turns several write groups
on by default, so every `false` flag below must be present, not omitted.
Tokens are never written into a file: each snippet references them by
environment variable *name*.

Keep all three configs in the owner's **user** settings, never committed to a
repo: the project allowlists name projects, and a committed file in a public
repo could leak Private project identifiers.

Run the pinned release with `uvx openproject-ce-mcp==0.4.1` (no persistent
install; the `pipx` alternative is in the Decision section).

### Claude Code (user scope)

Add it once to user scope, which stores it in `~/.claude.json`, not in any
repo:

```sh
claude mcp add-json openproject --scope user '<the JSON below>'
```

```json
{
  "command": "uvx",
  "args": ["openproject-ce-mcp==0.4.1"],
  "env": {
        "OPENPROJECT_BASE_URL": "http://127.0.0.1:8080",
        "OPENPROJECT_API_TOKEN": "${OPL_TOKEN_CLAUDE}",
        "OPENPROJECT_ENABLE_WORK_PACKAGE_READ": "true",
        "OPENPROJECT_ENABLE_WORK_PACKAGE_WRITE": "true",
        "OPENPROJECT_ENABLE_PROJECT_READ": "true",
        "OPENPROJECT_ENABLE_VERSION_READ": "true",
        "OPENPROJECT_ENABLE_MEMBERSHIP_READ": "true",
        "OPENPROJECT_ENABLE_PROJECT_WRITE": "false",
        "OPENPROJECT_ENABLE_VERSION_WRITE": "false",
        "OPENPROJECT_ENABLE_MEMBERSHIP_WRITE": "false",
        "OPENPROJECT_ENABLE_BOARD_WRITE": "false",
        "OPENPROJECT_ENABLE_MEETING_WRITE": "false",
        "OPENPROJECT_ENABLE_ADMIN_READ": "false",
        "OPENPROJECT_ENABLE_ADMIN_WRITE": "false",
        "OPENPROJECT_ENABLE_PERSONAL_WRITE": "false",
        "OPENPROJECT_ENABLE_USER_SCHEDULE_WRITE": "false",
        "OPENPROJECT_READ_PROJECTS": "*",
        "OPENPROJECT_WRITE_PROJECTS": "*"
  }
}
```

`${OPL_TOKEN_CLAUDE}` is expanded from Claude Code's environment when the
server starts. **Check in T2.O** that the token actually reaches the server:
the permission check runs as the claude user. If it doesn't, drop the
`OPENPROJECT_API_TOKEN` line and launch Claude Code with the token scoped to
that one process instead: `OPENPROJECT_API_TOKEN="$OPL_TOKEN_CLAUDE" claude`.
Docs: https://code.claude.com/docs/en/mcp (scopes, `add-json`, environment
variables).

### Codex CLI (user scope)

In `~/.codex/config.toml`, put every static value in the `env` table. List
**only** the token in `env_vars`, so an unset shell variable can never override a static
flag:

```toml
[mcp_servers.openproject]
command = "uvx"
args = ["openproject-ce-mcp==0.4.1"]
env_vars = ["OPENPROJECT_API_TOKEN"]

[mcp_servers.openproject.env]
OPENPROJECT_BASE_URL = "http://127.0.0.1:8080"
OPENPROJECT_ENABLE_WORK_PACKAGE_READ = "true"
OPENPROJECT_ENABLE_WORK_PACKAGE_WRITE = "true"
OPENPROJECT_ENABLE_PROJECT_READ = "true"
OPENPROJECT_ENABLE_VERSION_READ = "true"
OPENPROJECT_ENABLE_MEMBERSHIP_READ = "true"
OPENPROJECT_ENABLE_PROJECT_WRITE = "false"
OPENPROJECT_ENABLE_VERSION_WRITE = "false"
OPENPROJECT_ENABLE_MEMBERSHIP_WRITE = "false"
OPENPROJECT_ENABLE_BOARD_WRITE = "false"
OPENPROJECT_ENABLE_MEETING_WRITE = "false"
OPENPROJECT_ENABLE_ADMIN_READ = "false"
OPENPROJECT_ENABLE_ADMIN_WRITE = "false"
OPENPROJECT_ENABLE_PERSONAL_WRITE = "false"
OPENPROJECT_ENABLE_USER_SCHEDULE_WRITE = "false"
OPENPROJECT_READ_PROJECTS = "*"
OPENPROJECT_WRITE_PROJECTS = "*"
```

Give the token to that one Codex process only. **Don't** `export` it
shell-wide, or every later process would inherit Codex's token:

```sh
OPENPROJECT_API_TOKEN="$OPL_TOKEN_CODEX" codex
```

Docs: https://developers.openai.com/codex/mcp/ (`[mcp_servers.<name>]`,
`command`/`args`/`env`/`env_vars`).

### OpenCode, for Spark (user scope)

In the owner's user config, `~/.config/opencode/opencode.json`, not in a
repo. The allowlist holds **Public project identifiers only, never `*`**.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "openproject": {
      "type": "local",
      "command": ["uvx", "openproject-ce-mcp==0.4.1"],
      "enabled": true,
      "environment": {
        "OPENPROJECT_BASE_URL": "http://127.0.0.1:8080",
        "OPENPROJECT_API_TOKEN": "{env:OPL_TOKEN_SPARK}",
        "OPENPROJECT_ENABLE_WORK_PACKAGE_READ": "true",
        "OPENPROJECT_ENABLE_WORK_PACKAGE_WRITE": "true",
        "OPENPROJECT_ENABLE_PROJECT_READ": "true",
        "OPENPROJECT_ENABLE_VERSION_READ": "true",
        "OPENPROJECT_ENABLE_MEMBERSHIP_READ": "true",
        "OPENPROJECT_ENABLE_PROJECT_WRITE": "false",
        "OPENPROJECT_ENABLE_VERSION_WRITE": "false",
        "OPENPROJECT_ENABLE_MEMBERSHIP_WRITE": "false",
        "OPENPROJECT_ENABLE_BOARD_WRITE": "false",
        "OPENPROJECT_ENABLE_MEETING_WRITE": "false",
        "OPENPROJECT_ENABLE_ADMIN_READ": "false",
        "OPENPROJECT_ENABLE_ADMIN_WRITE": "false",
        "OPENPROJECT_ENABLE_PERSONAL_WRITE": "false",
        "OPENPROJECT_ENABLE_USER_SCHEDULE_WRITE": "false",
        "OPENPROJECT_READ_PROJECTS": "<Public identifiers only, comma-separated>",
        "OPENPROJECT_WRITE_PROJECTS": "<Public identifiers only, comma-separated>"
      }
    }
  }
}
```

`{env:OPL_TOKEN_SPARK}` resolves from OpenCode's environment. Docs:
https://opencode.ai/docs/mcp-servers/ (`type: local`, `command` array,
`environment` with `{env:VAR}` templates).

## Risks accepted

- **Third-party code with write access.** Mitigated by the pinned version
  and hashes, the fail-closed allowlists, per-model tokens, and OpenProject
  role permissions.
- **Prompt injection through tracker text** (a work package description
  telling a model to do something). The preview/confirm flow and role
  permissions bound the damage. Models still treat tracker text as data,
  not instructions.
- **Loopback plain HTTP.** Acceptable: traffic never leaves the machine.
