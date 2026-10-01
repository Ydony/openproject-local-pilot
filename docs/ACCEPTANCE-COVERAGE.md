# Acceptance coverage: which check proves which function

Maps each implemented function in DESIGN.md / the conductor spec to the
synthetic checks that cover it. All run offline with fakes (no OpenProject,
GitHub, Docker or model): `py -3.13 -m unittest discover -s tests`. Scripted
workers prove the conductor's behaviour, not real LLM connectivity; that is
proven only by the live pilot (docs/LIVE-TEST-REPORT.md,
docs/LIVE-PILOT-CHECKLIST.md).

End to end: `test_e2e_pipeline` (one task from an approved feature to a
merged PR with the real conductor code against fake OpenProject and GitHub)
and `test_scenarios` (conductor scenarios on in-memory worlds).

| Function | Checks |
|---|---|
| Setup and idempotency (types, statuses, roles, workflows, fields, views, priorities) | `test_model`, `test_admin_ruby`, `test_apply_api`, `test_views`, `test_configure_cli`, `test_static` |
| Permissions and who may do what | `test_permcheck`, `test_rule_enforce`, `test_risk_provenance`, `test_conductor_main` (least privilege) |
| Lifecycle, views, screens (Needs you, Action, Models, At risk) | `test_rule_stages`, `test_rule_screens`, `test_state`, `test_hal`, `test_collect_op` |
| Approval and dependencies (feature approval, predecessors, Ready too early) | `test_rule_stages`, `test_rule_enforce`, `test_scenarios` |
| CI, review SHA, merge gate (reviewed SHA, owner/lead Merge OK, conflicts) | `test_rule_merge`, `test_pr_checks`, `test_pr_refs`, `test_github`, `test_risk_provenance`, `test_collect_op` |
| Spark runs: build, review, test, fix, rework, rebase delivery | `test_spark_runner`, `test_spark_reviews`, `test_spark_fix`, `test_fix_head`, `test_spark_worktree`, `test_spark_packet`, `test_outcomes` |
| Sandbox route and worker isolation (account, launcher, environment) | `test_sandbox_run`, `test_sandbox_check`, `test_supervisor`, `test_opencode_adapter`, `test_git_env` |
| Privacy (Private projects refused, no secrets in packets, logs or errors) | `test_spark_packet`, `test_redaction`, `test_publication`, `test_settings`, `test_spark_runner` |
| Costs and usage (estimates, actuals, usage unknown vs zero) | `test_costs_config`, `test_rule_costs`, `test_usage`, `test_usage_attribution`, `test_usage_mapping`, `test_calibration`, `test_records` |
| Conductor operation (locking, shutdown, heartbeat, supervision, start script) | `test_engine`, `test_shutdown`, `test_conductor_heartbeat`, `test_conductor_start`, `test_conductor_main` |
| Provider refusals and failure handling (pause, no retry, branch names) | `test_spark_runner`, `test_opencode_adapter` |
| Temporary autonomy and handover to the owner | `test_collect_op`, `test_rule_merge`, `test_rule_screens`, `test_settings` |

## Not covered offline (needs the live pilot or the owner)

- Real OpenProject 17 behaviour of workflows and the API (live pilot).
- A real model's ability to finish a task (live Spark runs).
- Root-owned launcher installation and the worker account (owner step in
  docs/SANDBOX.md; `opl-sandbox-check` verifies it on the host).
