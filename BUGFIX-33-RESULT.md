# BUGFIX-33 result (issue #33: configure's My page pin deletes default widgets' views)

Synthetic fixtures only. No credentials, live APIs, Docker, pushes or merges.

## Symptom

After `bin/opl-configure` pinned Needs me, the two default My page widgets
showed "The embedded view could not be loaded". Cause (`opl/configure/views.py`,
`_pin_my_page`): the PATCH rebuilt widgets from
identifier/startRow/endRow/startColumn/endColumn/options only, dropping `id`,
so OpenProject replaced the widgets and deleted the queries they owned.

## Fix

- `opl/configure/views.py` (`_pin_my_page`): keeps each existing widget's `id`
  (everything except HAL `_links` is sent back) so the pin only adds one widget.
- Read-back check (`_verify_my_page_queries`): after the write every
  `work_packages_table` widget's `queryId` must `GET /api/v3/queries/<id>` 200,
  else loud `ApiError`; also checked on the already-pinned no-op path.
- Repair mode (`apply_views(..., repair_my_page=True)`,
  `bin/opl-configure --repair-my-page`): a widget whose query is gone gets a
  fresh hidden query reusing `_query_body` + `_users_by_login` (status open +
  assignee/author = owner; created-hint picks author), then the pin PATCH.
  Documented in `docs/CONFIGURE.md` and CLI help.
- `author` added to `BUILTIN_QUERY_FIELDS`/`LINK_VALUE_FIELDS`; user lookups
  shared via `_users_by_login` (reused, not duplicated).
- `tests/test_views.py` fake grid now models replace-on-missing-id (id-less
  PATCH destroys old widgets' queries; id-less widgets get fresh ids) plus
  per-query `GET /api/v3/queries/<id>`; `tests/test_configure_cli.py` got the
  same single-query GET + replace model for its own grid fake.

## Tests

- New `tests.test_views` (4): pin keeps ids + queries resolve; fake deletes on
  id-less PATCH (old code fails); read-back raises on gone query; repair posts
  one hidden query and all widgets resolve.
- Targeted: `tests.test_views` (11) + `tests.test_configure_cli` (5) OK.
- Full suite: `py -3.13 -m unittest discover -s tests`: 682 tests; see commit
  message for green/skips or pre-existing failures named there.

## Lead review (Claude)

The run hit the 30-minute limit during the final full suite, so the lead
committed it. Review fix: OpenProject's default My page widgets are
`work_packages_assigned` / `work_packages_created`, not
`work_packages_table`; repair and read-back now cover every
`work_packages_*` widget (`_is_wp_widget`), and the test fixtures use the
real identifiers. `test_views` + `test_configure_cli`: 16 OK.
