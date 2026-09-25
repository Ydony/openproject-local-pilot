# OpenProject Local Pilot

Reusable, public-safe automation for running OpenProject locally with Docker
Engine inside WSL 2. The project deliberately contains no credentials, private
project data, personal domains, or machine-specific paths.

The current implementation scope is defined in [SPEC.md](SPEC.md). GitHub
Issues and the linked project board are the execution source of truth.

## Toolkit quick map

Non-technical owners: read [docs/RUNBOOK.md](docs/RUNBOOK.md) (one page).

| Path | Purpose |
|---|---|
| `bin/opl-setup`, `opl-start`, `opl-stop`, `opl-status`, `opl-logs`, `opl-update` | Phased lifecycle (loopback-only, staged seeding) |
| `bin/opl-backup`, `bin/opl-restore-test` | Timestamped backups + disposable restore proof |
| `lib/common.sh` | Shared guards (bounded waits, no secret output) |
| `compose/docker-compose.override.yml` | Loopback-only publish template |
| `tests/test_static.py` (`tests/run.sh`) | Static/synthetic suite, stdlib only, no Docker daemon needed |
| `tests/live_docker.sh` | Optional `[live]` checks, skipped unless `RUN_LIVE_DOCKER=1` |
| `bin/opl-conductor` (`opl/conductor/`) | Check-and-fix loop (watch by default, live with `--live`); see [docs/CONDUCTOR.md](docs/CONDUCTOR.md) |
