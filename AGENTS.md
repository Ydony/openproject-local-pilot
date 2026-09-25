# OpenProject Local Pilot: instructions for coding agents

**Work list:** during the build, [docs/BACKLOG.md](docs/BACKLOG.md) is this
repository's plan and task list. Follow its Protocol section and record each
task's status in that file. The system design is [docs/DESIGN.md](docs/DESIGN.md).
The OpenProject tracker this repository builds will replace the backlog file
once it runs.

**Project board:** https://github.com/users/Ydony/projects/5 is the owner's
overview. Workers do not update it; the lead reconciles it from the backlog.

## Project-specific rules

- This repository is public and synthetic-only. Never add credentials, tokens,
  personal domains, private work packages, private project or repository names,
  database dumps, or machine-specific user paths.
- Use the official OpenProject `stable/17` Compose bundle as an unmodified
  upstream input. Add behavior only through overrides and wrapper scripts.
- Bind the application to `127.0.0.1` by default. Public exposure, tunnels,
  DNS, SMTP, and SSO are out of scope.
- Tokens: code uses API tokens only by reading environment variables whose
  names come from the external config (`$OPL_CONFIG_DIR/opl.toml`, never
  committed). Code never creates, stores, commits, logs, or prints a token.
  Creating tokens is the owner's job.
- Scripts must be idempotent, must not print secret values, and must use bounded
  waits with useful diagnostics.
- Initial startup must be staged so database seeding does not run concurrently
  with every Rails service on memory-constrained laptops.
- Python code: 3.11 or newer, standard library only.
- Tests must use synthetic fixtures and fakes. No test may need Docker, network
  access, OpenProject, or GitHub. Never start, stop, or remove Docker
  containers, volumes, or images on the development machine.
