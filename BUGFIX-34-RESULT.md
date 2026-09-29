# BUGFIX-34 result (issue #34: conductor flags the permcheck fixture)

Synthetic fixtures only. No credentials, live APIs, Docker, pushes or merges.

## Symptom

The owner's Needs me list permanently showed "Test feature" (Proposed) with
Action "Approve". It is the `bin/opl-permcheck` fixture (`[permcheck]
feature`, default "Test feature", in the `[permcheck] project`) under the
Maintenance epic; `opl/conductor/rules/screens.py` treated it as real work.

## Fix

- `opl/permcheck.py`: new `DEFAULT_FEATURE = "Test feature"` plus
  `fixture_reference()` returning (project, feature) from a Settings or plain
  dict. `run_all()` reuses it (no second hard-coded subject).
- `opl/conductor/rules/screens.py`: new `_is_under_maintenance_epic()` reused
  by `_is_maintenance_feature()` and new `_is_permcheck_fixture()` (settings
  project + feature name, under the Maintenance epic; Settings resolves a
  project name to its world key, plain dicts match the key directly).
  `_action()` returns None first for the fixture, so Approve and any
  violation-derived Unblock are suppressed and stale Action/Needs you are
  cleared by normal maintenance. `screens(world, permcheck=None)` stays
  backward compatible (None = no exemption).
- `opl/conductor/engine.py`: `run_once(..., permcheck=None)` passes through
  to screens; `opl/conductor/__main__.py` passes `settings`.
- `docs/CONDUCTOR.md`: new "Standing items the conductor leaves alone"
  covering the Maintenance pair and the permcheck fixture.
- `tests/test_rule_screens.py`: 8 synthetic tests — fixture quiet, unconfigured
  still Approve, same subject in another project still Approve, same subject
  outside the Maintenance epic still Approve, stale Action/Needs you cleared,
  default feature when missing, Settings name→key resolution, and
  `run_once` threading. permcheck itself untouched (fixture stays Proposed).

## Tests

- Targeted: `test_rule_screens + test_permcheck + test_engine`: 49 OK.
- Full suite: `py -3.13 -m unittest discover -s tests`: 686 tests OK
  (skipped=7). No failures to name.
