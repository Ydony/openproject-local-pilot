# BUGFIX-25 result (issue #25, T5.1)

Synthetic fixtures only. No credentials, live APIs, Docker, pushes or merges.

## 1. Type-invalid statuses are violations (enforce rule + planner guidance)

- `opl/conductor/rules/enforce.py`: `violations()` now flags any item whose
  status is not one of its type's statuses (per `config/pm-model.toml`),
  e.g. `Epic has Task status Draft; set Open`. The per-type lists mirror the
  shipped TOML (`TYPE_STATUSES`/`TYPE_DEFAULT`, like `APPROVED_ONWARDS`);
  callers holding a loaded `opl.model.Model` may pass it as
  `violations(world, model)` so the live file drives the check
  (`opl/conductor/rules/stages.py` does). The closed-task skip is now
  Task-only, so a Feature sitting in `Merged` is still flagged. `enforce()`
  keeps its shape: only violating Tasks get a `Blocked` change; Epic/Feature
  violations surface via the screens rule as `Unblock`/`Needs me`, never
  silently fixed (stages skips violating items, so S0 no longer auto-moves
  them; the S0 code stays as a fallback with a comment).
- Planner guidance in `docs/DESIGN.md` (structure section) and `docs/MCP.md`:
  anyone creating items must set the initial status explicitly
  (Epic → Open, Feature → Proposed, Task → Draft).
- Tests: `tests/test_rule_enforce.py` (Epic/Feature Draft, Task in an Epic
  status, Feature-in-Merged not skipped, unknown status, shipped-TOML parity
  via `opl.model.load`), `tests/test_rule_screens.py` (Draft Epic → Unblock
  with the reason), `tests/test_rule_stages.py` (S0 yields to the violation:
  no silent Proposed/Open move).

## 2. Base URLs use http://localhost:8080

- `docs/MCP.md`: all four `OPENPROJECT_BASE_URL` snippets now
  `http://localhost:8080`, plus one sentence explaining why (OpenProject
  answers its API only on its configured host name; `127.0.0.1` returns
  HTTP 400 except for health checks) and the planner-guidance note above.
- `config/opl.example.toml` and `config/opl.pilot.example.toml`:
  `[openproject] url` now `http://localhost:8080`.
- `docs/RUNBOOK.md`: pilot URLs now `http://localhost:8080` with the same
  one-sentence reason.
- Untouched on purpose: bind addresses (`PORT=127.0.0.1:…`, proxy ports,
  `OPL_LOOPBACK`), `test_url` (`:3001`, not the OpenProject API), synthetic
  test fixtures, and `docs/BACKLOG.md` (lead-owned).

## Tests

- Targeted: `tests.test_rule_enforce + test_rule_screens + test_rule_stages +
  test_engine + test_settings + test_model`: 105 tests OK.
- Full suite: `py -3.13 -m unittest discover -s tests` — see commit messages
  for the count; pre-existing unrelated failures (if any) are named there,
  not hidden.
