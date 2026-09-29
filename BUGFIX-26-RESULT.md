# BUGFIX-26 result (issue #26, T5.1)

Synthetic fixtures only. No credentials, live APIs, Docker, pushes or merges.

## Decision from DESIGN.md

The configure side is right and the screens rule is over-broad. DESIGN.md
section 1 creates a standing Maintenance epic containing a Maintenance
feature per project, empty until bugs and chores arrive; CONFIGURE.md pins
it as Open + Approved. The Needs-me table (section 4) lists `Unblock` only
for Blocked items, rule violations and Spark timeouts — an empty Approved
Maintenance feature is the designed resting state, not a violation. An
ordinary Approved feature with no tasks is still stuck (Proposed already
means "feature and its tasks drafted", Approved means "work may start", and
nothing can reach Building without a task), so the no-tasks rule is kept
for everything except the configured pair.

## Fix (smallest change)

- `opl/conductor/state.py`: `Item` gains `subject: str = ""` (default keeps
  all existing constructions working).
- `opl/conductor/collect.py`: the collector carries each work package's
  `subject` into the world (it already had it in the element).
- `opl/conductor/rules/screens.py`: the `Unblock: Feature has no tasks`
  branch now skips `_is_maintenance_feature()` — a Feature with subject
  `Maintenance` whose parent is the Epic with subject `Maintenance`, mirroring
  the pair `opl/configure/apply_api.py` creates. The bare word alone does not
  exempt: a `Maintenance`-named feature under any other epic, or any other
  subject, is still flagged.
- Tests: `tests/test_rule_screens.py` (live reproduction: configure's exact
  pair is quiet; ordinary empty Approved, bare-word-under-ordinary-epic, and
  legacy subject-less items still `Unblock`) plus `tests/test_collect_op.py`
  (subjects `E/F/T1` arrive on items via the existing FakeServer).

## Tests

- Targeted: `test_rule_screens + test_collect_op + test_state +
  test_scenarios + test_engine`: 66 tests OK.
- Full suite: `py -3.13 -m unittest discover -s tests` — see commit message
  for the count; the one pre-existing unrelated failure
  (`test_publication.test_no_tracked_file_has_a_machine_path`, flagging a
  `a Windows drive path` path inside `tests/test_opencode_adapter.py` on Windows) is named,
  not hidden.
