# Specification: local operator toolkit

> **Scope:** this file covers the operator toolkit (phase 0 of
> [docs/BACKLOG.md](docs/BACKLOG.md)). The wider system (tracker setup, model
> connection, conductor) is specified in [docs/DESIGN.md](docs/DESIGN.md),
> which governs those parts, including their use of user accounts and API
> tokens.

## Outcome

Provide a small, understandable toolkit that lets a non-technical owner start,
stop, inspect, back up, and test recovery of a localhost-only OpenProject 17
pilot running on Docker Engine inside WSL 2.

## Boundaries

The toolkit is generic and public. It must not modify Windows or WSL settings,
install Docker, operate a real deployment, access a real database, create user
accounts, handle API tokens, or publish a service to the internet. Those are
lead/owner responsibilities outside this repository.

The official `opf/openproject-deploy` `stable/17` bundle remains pristine.
Wrapper scripts may clone or update it into an ignored runtime directory and
apply a separate Compose override.

## Required deliverables

1. Lifecycle scripts for setup, phased start, stop, status, logs, and update.
   Startup must run database/cache/seeding first, verify a successful seeder
   exit with a bounded wait, then start web/proxy, wait for HTTP health, and
   finally start background services. The published port must be loopback-only.
2. Backup and disposable restore-test scripts. Backups must include a PostgreSQL
   dump and application assets, use timestamps and checksums, stay outside Docker
   volumes, use retention controls, and never print secret values. Restore proof
   must target disposable names and must not overwrite the active pilot.
3. Automated static/synthetic checks for shell syntax, secret placeholders,
   loopback binding, unchanged upstream files, bounded waits, and destructive
   restore safeguards. Live Docker tests may be optional and clearly labelled.
4. A one-page owner runbook with Start, Stop, Status, Backup, Restore Test,
   Update, common failure recovery, disk-space warning, and the exact boundary
   between owner, lead, and public Spark worker responsibilities.

## Acceptance criteria

- A fresh clone contains only placeholders and public synthetic data.
- Running setup twice is safe and does not replace an existing secrets file.
- Startup cannot launch all memory-heavy services during first-time seeding.
- Status distinguishes stopped, starting, ready, and unhealthy states.
- Backup exits non-zero on failure and produces a manifest plus checksums on
  success.
- Restore proof refuses the active Compose project/volumes and cleans up only
  disposable resources it created.
- No command defaults to deleting volumes, resetting Docker, changing DNS, or
  exposing a port beyond `127.0.0.1`.
- The runbook is usable without understanding Docker internals.

## Verification expected from the worker

Run every repository-provided static or synthetic test. Report commands,
results, remaining live-environment checks, and any assumptions. Do not claim a
real deployment, backup, or restore was tested unless the lead later performs
that check on the private host.
