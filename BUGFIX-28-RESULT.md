# BUGFIX-28 result (issue #28, findings 2, 4, 5)

Findings 1 and 3 were already merged; this covers only 2, 4 and 5.
Synthetic fixtures only. No credentials, live APIs, Docker, pushes or merges.

## Finding 2 — build success leaves no result comment (hop H6)

- `opl/conductor/spark/outcomes.py`: new `final_summary()` reuses the
  `OPL-RESULT` pattern to return the trailing text of the last DONE line
  (single line, 500-char cap); `parse_final_line` unchanged.
- `opl/conductor/spark/runner.py`: a successful build attempt carries
  `summary`; `_conclude_success` posts a short comment as the builder
  (`_comment`, so redacted) with the summary and the PR link, then records
  the action.
- Tests: `tests/test_outcomes.py` (`SummaryTests`, 5) and
  `tests/test_spark_runner.py`
  (`test_success_posts_result_comment_with_summary_and_pr`: comment holds
  the DONE text and the PR link, no worktree path).

## Finding 4 — no internal paths in OpenProject comments

- `opl/conductor/spark/runner.py`: review/test failures keep the kept-tree
  path in a separate `kept` field; the OpenProject comment says "work kept
  for the lead" with no filesystem path, while the path goes to the private
  log (`logger.warning`) and stays in `runs.jsonl`.
- Tests: `tests/test_spark_reviews.py`
  (`test_failed_review_keeps_its_worktree_and_says_where`) asserts the tree
  is kept, the comment says "work kept for the lead", and no path appears.

## Finding 5 — read project At risk from its status link

- `opl/conductor/collect.py`: new `_project_at_risk()` reuses
  `hal.link_href`/`hal.tail` to read `_links.status` (`at_risk`/`on_track`);
  missing/unknown reads as on track. The world now carries the real value,
  so `screens` + `drop_noops` emit no project change on a second cycle.
- Tests: `tests/test_collect_op.py` reads `at_risk`/`on_track` links and
  proves a second cycle with a matching status writes no project change.

## Tests

- Targeted: `test_outcomes` + `SuccessTests` (19) OK; `test_collect_op`
  (24) OK; review keep-tree tests (2) OK.
- Full suite: `py -3.13 -m unittest discover -s tests`: 651 tests OK
  (2 pre-existing environment skips, unrelated to this change).
