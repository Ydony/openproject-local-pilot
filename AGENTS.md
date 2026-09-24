# OpenProject Local Pilot: instructions for coding agents

**Project board:** https://github.com/users/Ydony/projects/5 - work items, epics (milestones) and status live here. This is the source of truth for what is done and outstanding; do not recreate it as a markdown checklist.

Standing cross-agent PM rules are in `C:/Projects/AI team and PM Tools/AGENTS.md`. Use `python "C:/Projects/AI team and PM Tools/pm-tools/pm.py"` with project key `opl` to create epics/tasks/relations for this repo.

## Project-specific rules

- This repository is public and synthetic-only. Never add credentials, tokens,
  personal domains, private work packages, database dumps, or machine-specific
  user paths.
- Use the official OpenProject `stable/17` Compose bundle as an unmodified
  upstream input. Add behavior only through overrides and wrapper scripts.
- Bind the application to `127.0.0.1` by default. Public exposure, tunnels,
  DNS, SMTP, SSO, and service-user tokens are out of scope.
- Scripts must be idempotent, must not print secret values, and must use bounded
  waits with useful diagnostics.
- Initial startup must be staged so database seeding does not run concurrently
  with every Rails service on memory-constrained laptops.
- Tests must use synthetic fixtures. A test may validate generated Compose
  configuration without requiring access to a live or private deployment.
