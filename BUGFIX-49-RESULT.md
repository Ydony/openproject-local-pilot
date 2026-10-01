# BUGFIX-49 result (issue #49: rejected push recorded as success)

Synthetic fixtures only. No credentials, live APIs, Docker, pushes or merges.

## Requirement

A rejected `git push` or failed PR creation is a run failure: comment the
reason (secrets redacted) plus the fix for recognised causes, Blocked with
Unblock for permission/auth rejections, `outcome: push-failed` in
runs.jsonl, keep the local branch as evidence. One retry only when a retry
can help.

## Fix

- `opl/conductor/spark/runner.py`:
  - New `_is_push_permission_error()` / `_push_fix_hint()`: workflow-scope
    (`workflow`, `refusing to allow`) plus 401/403, credential and
    permission signals are permission (no retry); anything else is
    transient (one retry). Workflow hint is "the conductor's GitHub token
    needs the Workflows: Read and write permission".
  - `_attempt` (build) and `_run_fix` no longer record `success` /
    `fix-success` before publishing; they carry `started`/`result` to the
    conclude step.
  - New `_push_once()` / `_push_with_retry()`, `_ensure_pr_once()` /
    `_ensure_pr_with_retry()` (find-or-create with the existing 422
    fallback), and `_handle_publish_failure()` (records `push-failed`,
    comments `Push/ PR creation/ Publish failed on branch X: <reason>`
    plus `Fix:`, moves to Blocked, keeps the worktree/branch).
  - `_conclude_success` pushes (retry), opens PR (retry), patches PR link +
    In review, then records `success`; any step failing records
    `push-failed` and Blocks instead of raising. `_conclude_fix_success`
    pushes `HEAD:<branch>` (retry) then clears review result, same
    failure handling (`fix-success` recorded only after push).
  - Reuses `_sh` redaction, `_record`, `_comment`/`_move`, `_push_env`,
    `RunResult`, `ApiError` status; no new dependencies.
- `tests/test_spark_runner.py`: updated
  `test_failing_github_push_leaks_no_secret` (now asserts Blocked,
  redacted `Push failed` comment, `push-failed` with no `success`, branch
  kept); new `PushFailureTests` (workflow-scope push Blocked with
  Workflows hint, 403 PR creation Blocked with branch pushed, success
  records `success`, classifier + permission-no-retry / transient-retry).
- `tests/test_spark_fix.py`: new
  `test_rejected_fix_push_blocked_push_failed` (workflow-scope fix push
  Blocks with hint, `push-failed`, no `fix-success`).

## Tests

- Targeted: `test_spark_runner` + `test_spark_fix` + `test_outcomes` +
  `test_records` (64) OK.
- Full suite: `py -3.13 -m unittest discover -s tests`: 709 tests OK
  (skipped=7, pre-existing environment skips). No failures to name.
