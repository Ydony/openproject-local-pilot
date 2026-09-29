# T5.1 S1 live fixes: result (issue #23)

Base: `ai/live-test-suite` @ d628d3a (ground truth, not reverted).
Synthetic fakes only; no live OpenProject, credentials, Docker, network,
pushes or merges.

## What changed

1. Saved views in HAL link form (`opl/configure/views.py`).
   - Create/patch bodies use live v17 shape: filters with
     `_links.filter` / `_links.operator` (model `=` -> `%3D`, `open` ->
     `o`), link-valued values (`type`, `status`, `version`, `project`,
     `assignee`) as `_links.values` hrefs resolved by name, scalars
     (custom boolean `true`/`false` -> `t`/`f`, e.g. "Needs you") as a
     top-level `values` list; `_links.columns`, `_links.sortBy`
     (`<field>-<asc|desc>`), `_links.groupBy`, `_links.project`.
     Custom fields stay `customField{id}`.
   - `_norm` compares by hrefs read back, so a second run makes zero
     writes (legacy pre-HAL bodies never compare equal and get updated
     once).
2. Fakes/golden aligned with live v17 (d628d3a).
   - `tests/test_apply_api.py` FakeWorld: versions created on
     `POST /api/v3/versions` with `_links.definingProject`; the
     per-project versions link is GET-only (list-only). Seeds use full
     `https://github.com/<repo>` URLs.
   - `tests/golden/admin_small.rb`: types carry `t.is_default = true`
     with `if t.new_record? || t.changed?`.
   - `tests/test_views.py` + `tests/test_configure_cli.py` query fakes
     preserve `_links` (columns/sortBy/groupBy/project) on POST/PATCH.
   - New tests, one per behaviour: permission name, password classes,
     project list options, Repo full URL, versions collection +
     definingProject, types `is_default`; plus HAL filter/column mapping,
     dry-run fresh, and rails error-line tests.
3. Dry run on a fresh instance (`opl/configure/apply_api.py`,
   `opl/configure/views.py`).
   - Missing `Repo`/`Visibility` in the project schema no longer aborts
     a dry run: it plans `set Repo, Visibility` per project and
     continues with no writes. Missing roles/types/statuses use
     `new:<name>` placeholders in dry runs; would-be projects plan
     creates + field sets; views fall back to placeholder hrefs so the
     plan still lists every create.
4. Readable rails errors (`opl/configure/__main__.py`).
   - `_run_opl_compose` returns the first exception line plus a short
     tail (still redacted) instead of only the last 500 chars of stack
     frames. `_first_error_line` unit-tested.

## Tests

- Configure set: `py -3.13 -m unittest tests.test_apply_api
  tests.test_views tests.test_admin_ruby tests.test_configure_cli`:
  36 tests OK (1 skip: ruby syntax, no ruby on box).
- Full suite: `py -3.13 -m unittest discover -s tests`: 576 tests OK
  (2 skips). No pre-existing unrelated failures (none to name).

## Unverified view mappings (lead to check live)

In `opl/configure/views.py`, marked UNVERIFIED in comments:

- `COLUMN_MAP`: `progress` -> `percentageDone` (alt. `doneRatio`);
  `version` -> `fixedVersion`; `parent` assumed `parent`.
- `FILTER_MAP`: `version` filter -> `targetVersion` (alt. `version`).
- Operator map covers `=`/`open` only; other ops pass through.
- Grid widget pin shape still best-effort.
