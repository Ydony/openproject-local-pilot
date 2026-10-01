# Issue #55 result: temporary autonomy mode (part 1: merging)

- `opl/settings.py`: optional `[project.autonomy]` table (`lead`, `until`),
  validated (spark refused, date required); `autonomy_active(project, today)`.
- `opl/conductor/collect.py`: while active, the lead's latest Merge OK counts
  as the owner's (`merge_ok_by_owner`), marked `merge_ok_by_lead`.
- `opl/conductor/rules/merge.py`: merge reason names the autonomy lead.
- `opl/conductor/__main__.py`: start log shows active/ended autonomy.
- Docs: `docs/CONDUCTOR.md` "Temporary autonomy".
- Tests: settings (parse/validate/expiry), collect (active, expired, other
  model, owner), merge reason.

Not in this part: assigning a finished feature to the owner with a summary
comment (follow-up), and dropping tasks (issue #51, needs configure).
