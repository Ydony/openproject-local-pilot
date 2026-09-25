# Owner runbook: OpenProject localhost pilot (one page)

All commands run inside WSL 2 from this folder. The pilot lives at
`http://127.0.0.1:8080` on your machine only — nobody else can reach it.

Needs (installed by the lead): Docker Engine inside WSL 2, git, curl,
python3 (3.11+).

## First time ever

1. `bin/opl-setup` (safe to run twice; never replaces your passwords),
   then `bin/opl-start` (takes a while: database first, app in stages).
2. Open `http://127.0.0.1:8080` and sign in with user `admin`,
   password `admin`. It forces you to choose a new password at once —
   do that before anything else.

## Every day

- `bin/opl-start` to begin, `bin/opl-stop` to end (data is kept).
- Am I up? `bin/opl-status`:
  - `ready` — working.
  - `starting` — wait a minute, then check again.
  - `stopped` — nothing is running; run start.
  - `unhealthy` — something crashed; see below.
  - (Right after `bin/opl-stop`, `stopped` is normal, not a fault.)

## Backup / Restore test / Update

- Backup: `bin/opl-backup` → prints `BACKUP_DIR=backups/backup-<date>`.
  Keeps the newest 5 automatically. Back up before every update.
- Restore test (proves the backup works, never touches the live pilot):
  `bin/opl-restore-test backups/backup-<date>` → look for `RESTORE_PROOF=PASS`.
- Update: `bin/opl-backup`, then `bin/opl-update` (fetches the new version
  AND its images), then `bin/opl-start`.

## Configure the tracker

- First: `bin/opl-configure --dry-run` (prints the plan, changes nothing).
- Then: `bin/opl-configure` (applies statuses, projects, users, views).
- Details: `docs/CONFIGURE.md`. Safe to re-run; prints only real changes.

## Automation (conductor)

- `bin/opl-conductor --once` runs one safe watch-only cycle (changes
  nothing, plans go to `<state_dir>/watch.log`).
- Details, logs, tuning and stopping: `docs/CONDUCTOR.md`.

## When something is wrong

| What you see | Do this |
|---|---|
| `starting` for > 10 min | `bin/opl-logs web proxy`, then stop + start again |
| `unhealthy` | `bin/opl-logs`, note the failing service, ask the lead |
| Start says port busy | Something already uses 8080; ask the lead before changing it |
| disk-space warning | Backups pile up: keep the newest, delete older `backups/` folders |

## Who does what (boundaries)

- **Owner (you):** run the commands above, keep backups, report errors.
- **Lead:** installs Docker/WSL, changes ports/passwords, restores real data,
  and edits the optional tuning file if your machine needs it (you never
  need to touch it).
- **Public Spark worker (automation):** maintains these scripts with fake
  test data only — it never sees your machine, your data, or your passwords.
