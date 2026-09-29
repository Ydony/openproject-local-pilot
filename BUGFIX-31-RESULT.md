# BUGFIX-31 result (issue #31: single-model reasoning remainder)

Synthetic fixtures only. No credentials, live APIs, Docker, pushes or merges.

## Symptom

Single-model OpenCode (Spark) sessions matched the right task/feature but
priced nothing: `usage: session … total does not match its model rows`
(`opl/usage.py`, the session-total check). ccusage counts unreported
reasoning output in the session `totalTokens` but in no row field (no
`reasoningOutputTokens`): rows sum to 33,876, the session total is 33,951
(75-token remainder).

## Fix

- `opl/usage.py` (`price_sessions`): when a session has exactly one model
  breakdown and its total exceeds the row sum by a positive remainder,
  attribute the remainder to that model as reasoning/output tokens (output
  price) so the total matches exactly. Multi-model mismatches and row sums
  above the total stay unknown as before; unknown is never zero. Docstring
  updated; accepted remainders log an INFO line, mismatches keep the
  existing WARNING.
- `tests/test_usage.py` (`ReasoningRemainderTests`, 3): the issue's exact
  sample (tokens 33,951; cost = `config/prices` Spark rates for
  input/output/cache + 75 output tokens = 0.001063106), a two-model
  remainder that stays unknown, and a rows-above-total case that stays
  unknown.
- `tests/test_usage_attribution.py`
  (`test_session_total_must_match_its_model_rows`): now uses two model
  rows, since a single-model positive remainder is priced per #31; the
  multi-model unknown path is still covered.

## Tests

- Targeted: `tests.test_usage` + `tests.test_usage_attribution` (51) OK;
  `ReasoningRemainderTests` (3) OK.
- Full suite: `py -3.13 -m unittest discover -s tests`: 656 tests OK
  (2 pre-existing environment skips, unrelated to this change).
