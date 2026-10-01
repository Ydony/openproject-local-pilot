# BUGFIX-42 result (issue #42: per-project runtime/setup/test declaration)

Synthetic fixtures only. No credentials, live APIs, Docker, pushes or merges.

## Requirement

Optional `[[project]]` fields in `opl/settings.py`: `runtime` (list,
e.g. `["node@22"]`), `setup`/`test` (commands, e.g. `npm ci` /
`npm test`). Validate types; unknown runtimes allowed but reported.
Document in `docs/CONFIGURE.md` + `docs/CONDUCTOR.md` with an example.

## Fix

- `opl/settings.py`: `Project` gains `runtime: tuple = ()`,
  `setup: str = ""`, `test: str = ""` (defaults = undeclared, so all
  existing positional constructions keep working). New `_opt_runtime()`
  (must be a list of non-empty strings) and `_opt_command()` (must be a
  string) wired into `_settings_from_data()`; wrong types raise
  `SettingsError` naming project + field. New `KNOWN_RUNTIMES`
  (`node@20`, `node@22`, `python@3.11`–`3.13`) plus
  `unknown_runtimes(project)` helper: unknown entries load fine and are
  returned for callers to report.
- `config/opl.example.toml`: commented `runtime`/`setup`/`test` example.
- `docs/CONFIGURE.md`: new "Per-project build/test readiness" section
  with TOML example and validation rules. `docs/CONDUCTOR.md`: new
  "Project build/test readiness" section (source of truth for later
  readiness checks).
- `tests/test_settings.py`: new `ReadinessTests` (7 synthetic tests) —
  defaults, parsing, unknown-allowed-but-reported, known-not-reported,
  `runtime`/`setup`/`test` type rejection.

## Tests

- Targeted: `py -3.13 -m unittest tests.test_settings`: 20/20 OK.
- Full suite: `py -3.13 -m unittest discover -s tests`: 704 tests OK
  (skipped=7) in ~484 s. No failures to name.
