# BUGFIX-30 result (issue #30, T5.1 F7)

Synthetic fixtures only. No credentials, live APIs, Docker, pushes or merges.

## Fix 1 — test scope is the feature, not the epic

- `opl/conductor/spark/packet.py`: the test steps now say to judge only
  this feature's Done-when list (feature list plus test target is the
  whole test scope) and that the epic text is context about the whole
  epic, not acceptance criteria — sharing the review-scope sentence
  shape merged for #28.
- Tests: `tests/test_spark_packet.py`
  (`test_test_scope_is_the_feature_not_the_epic`).

## Fix 2 — per-task process evidence in the test packet

- `opl/conductor/spark/packet.py`: new `format_process_evidence()`
  formats one factual bullet per task from collected fields only
  (Review result + by-reviewer or not, `reviewed: <sha>` + SHA or not,
  Merge OK + by-owner or not, PR merged at the reviewed SHA / merged
  elsewhere / not merged / no link); `build_packet()` renders it as a
  `## Process evidence` section when provided, and the test steps tell
  the tester to judge process criteria from that evidence.
- `opl/conductor/spark/runner.py`: `_test_evidence()` builds the
  section from the conductor's own collected data (world items plus
  collected PR states — no live reads, no tokens) and `_run_test`
  passes it into every test packet.
- Tests: packet unit tests (positive/negative/unattributed/moved-head/
  unmerged/sort/no-tasks/secret-absence) plus
  `tests/test_spark_reviews.py`
  (`test_test_packet_carries_scope_and_process_evidence`, via the
  existing `worker_test_pass.py` fake: packet `test-2.md` holds the
  scope lines and the Task 5 evidence, no token).

## Tests

- Targeted: `test_spark_packet` + `test_spark_reviews` (46) OK.
- Full suite: `py -3.13 -m unittest discover -s tests`: 665 tests OK
  (2 pre-existing environment skips, unrelated to this change).
