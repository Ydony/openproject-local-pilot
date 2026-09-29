# BUGFIX-35 result (issue #35: owner sees seeded statuses on epics)

Synthetic fixtures only. No credentials, live APIs, Docker, pushes or merges.

## Symptom

Read-only audit (2026-09-29, OP17): owner (admin) can move Epics to seeded
statuses (New, In specification, ...), not just Open/Closed (DESIGN.md §2).
Models and conductor see only designed transitions. Cause: admin sees the
union of all roles' workflows; `opl/configure/admin_ruby.py` reset only
managed (role, type) pairs, leaving seeded roles' (Member, ...) default
workflows for Epic/Task.

## Fix

- `opl/configure/admin_ruby.py`: new `Workflow cleanup (unmanaged roles)`
  section — per managed type (`Epic`, `Feature`, `Task`): keep
  `Role.where(name: <managed>).pluck(:id)`, delete
  `Workflow.where(type_id: cleanup_type.id).where.not(role_id: managed_ids)`
  only `if stray.exists?` (idempotent, prints count only on change).
  Never touches other types. New `describe_workflow_cleanup(model)` reuses
  `model.types` (no duplication) for the dry-run plan.
- `opl/configure/__main__.py`: dry run prepends that list as
  `would remove workflows for unmanaged roles on type <T>`.
- `tests/golden/admin_small.rb`: regenerated (one cleanup block for `T1`).
- `tests/test_admin_ruby.py`: scoped-delete guard now allows
  `stray.delete_all`; new tests cover Epic+Task (+Feature), keep-list,
  `exists?` guard, `describe_*` scope, and cleanup-section type scoping.
- `tests/test_configure_cli.py`: dry run lists cleanup for all three
  managed types and no other type.

## Tests

- Targeted: `test_admin_ruby + test_configure_cli`: 22 OK (1 ruby-syntax
  skip, no ruby on PATH).
- Full suite: `py -3.13 -m unittest discover -s tests`: see commit message
  for green/skips or pre-existing failures named there.
