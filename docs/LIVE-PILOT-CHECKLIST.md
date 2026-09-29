# Live-pilot operator checklist (T5.1, synthetic template)

For the lead/owner live run. Offline fake tests do NOT prove the live
pilot; they only prove the adapter wiring. Stop at the first red step.

## Setup (lead, once)

- [ ] Install OpenCode in WSL (`curl -fsSL https://opencode.ai/install | bash`).
- [ ] `opencode run --help` shows `run`, `--model provider/model`,
      `--variant`, `--dir`. If flags differ, update `bin/opl-opencode-run`
      (security review first) and re-run synthetic tests.
- [ ] On Windows, set `OPENCODE_GIT_BASH_PATH` to Git's `bash.exe`.
      Confirm `wsl.exe` is NOT used as Git Bash
      (`opl-opencode-run` never confuses them; see `discover_shell`).
- [ ] Copy `config/opl.pilot.example.toml` to `$OPL_CONFIG_DIR/opl.toml`;
      set real repo, `local_repo`, `base_ref`, `pr_base` (plain branch).
- [ ] Owner creates tokens; export only names from the config
      (`OPL_TOKEN_*`, `OPL_WORKER_API_KEY`). Never paste values.
- [ ] `bin/opl-conductor --once` (watch) is clean; `watch.log` shows no
      unexpected changes.

## Invocation (two tiny tasks)

- [ ] Task A (build probe): Ready + Spark + Public + Approved feature.
      Expect `yes.txt` containing `YES` in the worktree branch.
- [ ] Task B (status probe): after the PR, expect OpenProject status
      `In review` with PR link, then reviewer/merge flow per DESIGN §3.
- [ ] Live command: `bin/opl-conductor --once --live`
      (config `live = true` AND the flag; otherwise watch-only).
- [ ] Evidence per run: `packets/task-<id>-<n>.md`, `logs/run-<id>-<n>.log`
      (chmod 600), `runs.jsonl` line, worktree branch. Usage via
      `bin/opl-conductor runs` or `unknown` when ccusage has no match.

## Stop points (do not proceed past a stop)

- Timeout/stall (`S/M/L` + 10 min stall): task goes Blocked, partial work
  stays on the branch. Lead decides: more time, split, or reassign.
- Second failure / ceiling: `attempts.json` blocks further auto-runs;
  move the item by hand for a fresh pair (see CONDUCTOR.md).
- `Not started (<kind> run): <reason>`: dispatch guard refused
  (privacy, origin, PR base, parent/predecessor). Fix config/tracker.
- Any secret value in logs/packets/argv: stop, scrub, report.
- Private content near Spark, or a request to relabel Spark as
  Claude/Codex: stop. Missing real-model route is a blocker (below).

## Requires lead security review (NOT implemented here)

- Any second-model (Claude/Codex) automatic routing or sandbox change:
  new role attribution, token scoping, approval/privacy bypass, launch
  helper, narrow sudoers, cross-UID shutdown, container boundary.
  See `docs/SANDBOX.md`; owner-run canary `bin/opl-sandbox-check`.
- New authorization architecture, MCP permission changes, Enterprise,
  tunnels, DNS, SMTP, SSO, or public exposure (all out of scope).

## Remains unproven (lead verifies live)

- Genuine API/model/GitHub flow: real OpenProject tokens, real
  `opencode run` model call, real branch/PR, real status/result/link,
  real usage/cost. Offline fakes prove wiring only.
