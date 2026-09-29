# BUGFIX-27 result (issue #27, T5.1 S5)

Synthetic fixtures only. No credentials, live APIs, Docker, pushes or merges.

## Symptom

`bin/opl-sandbox-run` exited 0 but printed only `[tool] tool` lines plus
`usage unknown`: no assistant text, no `OPL-RESULT:` line, so the conductor
marked every successful run failed. Cause: the converter assumed guessed
top-level fields; real OpenCode 1.18 nests everything under `part`.

## Fix (one shared parser)

- `opl/conductor/spark/opencode.py` (#12) now owns the JSON-lines converter
  (`TOOL_TYPES`, `extract_usage`, `render_event`, `convert_line`,
  `format_usage`): `text` comes from `part.text`, the tool line from
  `part.state.title` (falling back to `part.tool`), usage sums
  `part.tokens.input/output` plus `part.cost` across `step_finish` events,
  `step_start` is silent. Legacy flat shapes are still accepted.
- `opl/conductor/spark/sandbox_run.py` imports and reuses that parser
  instead of its own copy (`convert_line is opencode.convert_line`).
- `tests/fakes/sandbox_launcher_fake.py` emits the real shape (`text` with
  `part.text`, `tool_use` with `part.state.title`, `step_finish` with
  `part.tokens`/`cost`).
- `tests/test_sandbox_run.py`: `test_real_opencode_sample_lines` feeds the
  issue's 7 sample lines and asserts `OPL-RESULT: DONE` is printed, tool
  lines are `[tool] read` (empty title falls back to tool) and
  `[tool] yes.txt`, and usage sums to `input=7763 output=158` (not
  "unknown"); `test_shared_parser_with_opencode` locks the single parser.

## Tests

- Targeted: `tests.test_sandbox_run` (28) + `tests.test_opencode_adapter`
  (21) OK.
- Full suite: `py -3.13 -m unittest discover -s tests`: 636 tests OK
  (2 pre-existing environment skips: POSIX-only mode test and a
  ruby/bash-conditional, unrelated to this change).
