"""The top-level tests that may stay in `tests/`: what stays by design and what is registered.

Read by `scripts/structure/tests_location.py` (the rule is in its header). Two tables, both keyed by
repo-relative POSIX path:

- `BY_DESIGN`: a directory (trailing slash) or a file with no package to move to. Each carries a
  one-line reason. It repeats `placement.TOP_LEVEL_*` (`scripts/structure/placement.py`), which
  `scripts/structure/tests/test_tests_location.py` keeps in sync, plus `tests/ui/`, whose every
  test checks a ui artifact and so has no package home either.
- `ALLOWED`: one test file with a category and a one-line reason a reviewer can check.
  `contract`: it reads repository artifacts no package owns (workflows, `pyproject.toml`,
  `db/schema.sql`, migrations, `ui/`, `schedules/`, `deploy/`, skill scripts, the test harness
  itself) or scans the whole tree. `integration`: it spans units that may not import each other,
  so no package may hold it. An entry whose file is gone, that no longer needs one (it moved under
  `BY_DESIGN`, or is also frozen in the baseline) or that has another category or an empty reason
  fails the lint. Debt that is still to move is not listed here: it is frozen in the
  `tests_location` baseline section.
"""

from __future__ import annotations

from typing import Literal

Category = Literal["contract", "integration"]

BY_DESIGN: dict[str, str] = {
    "tests/e2e/": "end-to-end tests drive the whole stack from outside every package",
    "tests/factories/": "shared test-data factories",
    "tests/fixtures/": "shared fixture plugins loaded by the root conftest.py",
    "tests/integration/test_cluster_instance.py": "real-process proof with its own CI wiring",
    "tests/integration/test_grafana_native_runtime.py": "real-process proof with its own CI wiring",
    "tests/integration/test_schedule_runner_cleanup.py": "real-process proof with its own CI wiring",
    "tests/ui/": "tests of ui artifacts (Android overlay and update check, app CI and release workflows); ui/ is not a Python package",
}

ALLOWED: dict[str, tuple[Category, str]] = {
    "tests/agent/test_claim_auto_resurrect.py": (
        "integration",
        "agent and ops are peers: the claim node's resurrect batch is driven through ops.agents.wake.resurrect_agent",
    ),
    "tests/agent/test_compact_contract.py": (
        "contract",
        "the compaction triggers point at the ava.self.compact contract in commands/compact.md",
    ),
    "tests/agent/test_corpse_reap.py": (
        "integration",
        "the hosted corpse reaper's recrash trigger runs against real rows the ops lifecycle writes: spans agent, base, ops, no one of which may import all the others",
    ),
    "tests/agent/test_hosted_resurrection_fences.py": (
        "integration",
        "agent and ops are peers: both sides write the same lifecycle row and fence each other",
    ),
    "tests/agent/test_lifecycle_finalizers.py": (
        "integration",
        "dead-letter cleanup must not overwrite a durable lifecycle command: the agent finalizer and the delivery watchdog share the row: spans agent, base, services.wake.delivery_watchdog, no one of which may import all the others",
    ),
    "tests/agent/test_maintenance.py": (
        "integration",
        "maintenance drain over real ownership/claim rows and the compiled graph, driven through the cli and the agent host: spans agent, base, cli, services.agent_runner.agent_host, no one of which may import all the others",
    ),
    "tests/agent/test_maintenance_legacy_cold.py": (
        "integration",
        "maintenance classifies persisted agent ENDs that ops lifecycle rows decide: spans agent, base, ops, no one of which may import all the others",
    ),
    "tests/agent/test_maintenance_lifecycle_wait.py": (
        "integration",
        "maintenance waits for in-flight agent work that ops lifecycle rows describe: spans agent, base, ops, no one of which may import all the others",
    ),
    "tests/agent/test_resurrect_lifecycle_fence.py": (
        "integration",
        "agent and ops are peers: resurrect fences against a pending terminate across both",
    ),
    "tests/ava/test_migration_baseline.py": (
        "contract",
        "asserts the baseline schema and trigger contracts of db/schema.sql and migrations/",
    ),
    "tests/ava/test_self_evolution_schedules.py": (
        "contract",
        "loads schedules/*.py and locks the weekly schedule's /api/events contract",
    ),
    "tests/base/test_agents_meta_spawner.py": (
        "contract",
        "asserts the agents_meta spawner-lineage constraints the schema enforces",
    ),
    "tests/base/test_bare_ava_contract.py": (
        "contract",
        "the host converge step wires the bare ava launcher script, scripts/ava-launcher.sh",
    ),
    "tests/base/test_config_contract.py": (
        "contract",
        "the frontend config-group keys (ui/web/src/app/control/_config_groups.ts) are real backend field aliases",
    ),
    "tests/base/test_config_lite_table.py": (
        "contract",
        "drift locks of the generated config index, base/host/env/config_lite_table.json, against its generator and the live registry",
    ),
    "tests/base/test_connect_helpers_contract.py": (
        "contract",
        "the migration restores the column the baseline schema, db/schema.sql, gives a fresh database",
    ),
    "tests/base/test_crash_row_writers.py": (
        "contract",
        "scans every production module for writers of the crash-row predicate fields",
    ),
    "tests/base/test_no_secrets_on_argv.py": (
        "integration",
        "one guard over every launch path (cli, schedule manager, sdk) for secrets on a command line: spans ava, base, cli, services.wake.schedule_manager, no one of which may import all the others",
    ),
    "tests/base/test_poll_until.py": (
        "contract",
        "tests the harness's poll_until helper, tests/base/poll_until.py",
    ),
    "tests/base/test_proc_contract.py": (
        "contract",
        "scans the git-driving modules of the whole tree for subprocess.run timeouts",
    ),
    "tests/base/test_recovery_breaker.py": (
        "integration",
        "the recovery breaker's durable streak and halt write are shared by the agent and ops sides: spans agent, base, ops, no one of which may import all the others",
    ),
    "tests/base/test_runner_role.py": (
        "contract",
        "asserts the ava_runner capability matrix declared in db/schema.sql",
    ),
    "tests/base/test_scan_contract.py": (
        "contract",
        "scans every first-party skill of the repository for critical findings",
    ),
    "tests/base/test_schedule_timezone.py": (
        "contract",
        "scans the schedules/ templates for the cluster wall clock",
    ),
    "tests/base/test_shutdown.py": (
        "integration",
        "signal registration proven against real supervised daemons of agent host, agent ops and ava-root: spans base, services.agent_runner.agent_host, services.agent_runner.agent_ops, services.supervision.ava_root, no one of which may import all the others",
    ),
    "tests/base/test_ui_contributions_contract.py": (
        "contract",
        "the themable token set equals ui/web/src/app/globals.css :root",
    ),
    "tests/base/test_uvicorn_stdlib_intercept.py": (
        "integration",
        "stdlib logging of a real uvicorn server reaches the base handler through the gateway and the memory-search service: spans base, gateway, services.derived.memory_search, no one of which may import all the others",
    ),
    "tests/ci/test_backend_test_gate.py": (
        "contract",
        "reads the require-test-gate action and .github/workflows/ci.yml",
    ),
    "tests/ci/test_collection_guard.py": (
        "contract",
        "tests the collection guard plugin, tests/fixtures/collection_guard.py",
    ),
    "tests/ci/test_collection_roots.py": (
        "contract",
        "checks pyproject testpaths and ci.yml against every tests/ directory in the tree",
    ),
    "tests/ci/test_leak_guard.py": (
        "contract",
        "tests the root leak guard plugin, tests/fixtures/leak_guard.py",
    ),
    "tests/ci/test_leak_guard_cost.py": (
        "contract",
        "tests the cost of the root leak guard plugin, tests/fixtures/leak_guard.py",
    ),
    "tests/ci/test_owned_gateway_port.py": (
        "contract",
        "runs the real-uvicorn inherited-socket proof with the e2e harness (tests/e2e/_proc.py), wired by .github/workflows/e2e-owned-port.yml",
    ),
    "tests/ci/test_path_scopes.py": (
        "contract",
        "tests the PATH_SCOPES plugin, tests/fixtures/path_scopes.py",
    ),
    "tests/ci/test_pgbouncer_pin.py": (
        "contract",
        "keeps .github/actions/install-pg-redis and scripts/provision/database.sh on one PgBouncer pin",
    ),
    "tests/ci/test_prepush_hooks.py": (
        "contract",
        "runs .pre-commit-config.yaml and the scripts/prepush-*.sh hooks as processes",
    ),
    "tests/ci/test_prepush_selection.py": (
        "contract",
        "runs the scripts/prepush-*.sh and precommit-eslint.sh hooks against .pre-commit-config.yaml in throwaway repositories",
    ),
    "tests/ci/test_pyright_test_environments.py": (
        "contract",
        "checks the pyright test environments in pyproject.toml against every tests/ directory",
    ),
    "tests/ci/test_redis_pin.py": (
        "contract",
        "keeps the install paths that read scripts/provision/database.sh on one Redis series",
    ),
    "tests/ci/test_shard_counts.py": (
        "contract",
        "checks .github/workflows/ci.yml against scripts/ci/shard_counts.py",
    ),
    "tests/ci/test_structure_codegen.py": (
        "contract",
        "keeps ci.yml, .pre-commit-config.yaml and ui/web/openapi.json in step with the codegen coverage",
    ),
    "tests/ci/test_tests_dir_scope.py": (
        "contract",
        "checks .pre-commit-config.yaml and pyproject.toml against every lint that decides about test files",
    ),
    "tests/ci/test_workflow_paths.py": (
        "contract",
        "scans every .github/workflows file for unfiltered pull_request/push triggers",
    ),
    "tests/cli/test_converge_lgtm_contract.py": (
        "contract",
        "the rendered LGTM configs equal the repository's deploy/lgtm provisioning files (loki.yaml, datasources.yml) and the native Loki limits match the container rollback config",
    ),
    "tests/cli/test_converge_redis_bridge.py": (
        "integration",
        "cli converge drives the redis bridge service end to end: spans cli, services.redis_bridge, no one of which may import all the others",
    ),
    "tests/cli/test_main_dispatch_contract.py": (
        "contract",
        "scans the repository root for entry points that declare the database gate exemption",
    ),
    "tests/cli/test_maintenance_readiness.py": (
        "integration",
        "cli start against a real gateway app: cli and gateway are peers",
    ),
    "tests/cli/test_otel_bootstrap_relay.py": (
        "integration",
        "the cli bootstrap and the gateway publish one relay routing: spans base, cli, gateway, no one of which may import all the others",
    ),
    "tests/cli/test_pgbouncer_config.py": (
        "integration",
        "cli config generation and the direct-connection exemption of the backup service: spans base, cli, services.backup.dump, no one of which may import all the others",
    ),
    "tests/cli/test_pgbouncer_wire.py": (
        "integration",
        "a real PgBouncer in front of a throwaway Postgres, exercised through the cli, the gateway and the ttl reaper: spans base, cli, gateway, services.upkeep.ttl_reaper, no one of which may import all the others",
    ),
    "tests/gateway/test_alerts_api.py": (
        "integration",
        "/api/alerts through the gateway app against the base ingest core and the shared session database fixtures",
    ),
    "tests/gateway/test_caller_protocol_path.py": (
        "integration",
        "a real cli -> authenticated HTTP -> ownership transaction -> hosted claim proof: spans agent, base, cli, gateway, no one of which may import all the others",
    ),
    "tests/gateway/test_cluster_endpoints.py": (
        "integration",
        "/api/cluster/* endpoints over ops cluster state and the delivery watchdog: spans base, gateway, ops, services.wake.delivery_watchdog, no one of which may import all the others",
    ),
    "tests/gateway/test_delivery_publish.py": (
        "integration",
        "gateway chat delivery against the delivery watchdog's transaction and event ordering: spans base, gateway, services.wake.delivery_watchdog, no one of which may import all the others",
    ),
    "tests/gateway/test_labels.py": (
        "integration",
        "thread-label endpoint and the labeler service share the same rows: spans base, gateway, services.derived.labeler, no one of which may import all the others",
    ),
    "tests/gateway/test_okf_graph_contract.py": (
        "contract",
        "every .ava.okf.md bundle of the repository parses identically through the adapter and the legacy parser",
    ),
    "tests/gateway/test_schemas_wire_format.py": (
        "contract",
        "freezes the wire schemas against ui/web/openapi.json and ui/web/src/lib/types.ts",
    ),
    "tests/integration/test_hosted_lifecycle_integration.py": (
        "integration",
        "agent and ops are peers: a hosted force settles against a prior restart across both, and neither imports the other",
    ),
    "tests/integration/test_impersonation_audit_root_inventory.py": (
        "contract",
        "scans every production module for audit-construction roots against a classified inventory",
    ),
    "tests/integration/test_impersonation_notifications.py": (
        "integration",
        "lease reminders and termination notices across agent, ops, cli and the delivery watchdog: spans agent, base, cli, ops, services.wake.delivery_watchdog, no one of which may import all the others",
    ),
    "tests/lifecycle/db_authority/test_api_tokens.py": (
        "integration",
        "the machine API token acceptance matrix across the cli, the gateway, ops and agent ops: spans base, cli, gateway, ops, services.agent_runner.agent_ops, no one of which may import all the others",
    ),
    "tests/lifecycle/db_authority/test_backup_owner.py": (
        "integration",
        "logical backup maintenance on an authenticated home across the cli, the backup scheduler and scripts: spans base, cli, scripts, services.backup.scheduler, no one of which may import all the others",
    ),
    "tests/ops/test_agent_wake_hosted.py": (
        "integration",
        "real-database resurrection: the ops status transition and the agent's hosted wake: spans agent, base, ops, no one of which may import all the others",
    ),
    "tests/ops/test_resurrection_admission.py": (
        "integration",
        "agent and ops are peers: admission and resurrect race on the same rows",
    ),
    "tests/ops/test_schema_mismatch.py": (
        "integration",
        "schema status from an installed image through the cli, the gateway and ops: spans base, cli, gateway, ops, no one of which may import all the others",
    ),
    "tests/plugins/test_grafana_dashboard_render.py": (
        "contract",
        "compares the rendered dashboard with deploy/lgtm/config/grafana/provisioning/dashboards/ava-ops-main.json",
    ),
    "tests/plugins/test_plugin_metrics_logql.py": (
        "contract",
        "checks the shipped plugin metrics against the deploy/lgtm dashboard queries",
    ),
    "tests/schedules/test_adversarial_eval_weekly.py": (
        "contract",
        "tests schedules/adversarial-eval-weekly-schedule.py and schedules/manifest.json",
    ),
    "tests/schedules/test_agent_directory_consumers.py": (
        "contract",
        "scans the schedules/ templates for directory-search paging",
    ),
    "tests/schedules/test_agent_status_guard.py": (
        "contract",
        "scans the schedules/ templates for AgentStatus dependency guards",
    ),
    "tests/schedules/test_c9_daily_report.py": (
        "contract",
        "tests schedules/c9-daily-report-schedule.py and schedules/manifest.json",
    ),
    "tests/schedules/test_catchup.py": (
        "contract",
        "tests the schedules/ templates and schedules/daily_host.py catch-up behavior",
    ),
    "tests/schedules/test_debt_sweep_daily.py": (
        "contract",
        "tests schedules/debt-sweep-daily-schedule.py",
    ),
    "tests/schedules/test_dev_ci_metrics.py": (
        "contract",
        "tests schedules/dev-ci-metrics-schedule.py",
    ),
    "tests/scripts/structure/test_lint_common_contract.py": (
        "contract",
        "the framework directory list of scripts/structure/lint_common.py equals the packages pyproject.toml declares",
    ),
    "tests/scripts/structure/test_placement_contract.py": (
        "contract",
        "scans the tests of every tool under scripts/ for the home of their sample trees",
    ),
    "tests/scripts/test_alert_rules.py": (
        "contract",
        "validates deploy/lgtm/config/grafana/provisioning/alerting/rules.yml",
    ),
    "tests/scripts/test_alert_notification_policy.py": (
        "contract",
        "validates the notification policy of deploy/lgtm/config/grafana/provisioning/alerting/contact.yml",
    ),
    "tests/scripts/test_alert_rules_signals.py": (
        "contract",
        "validates the signal rules of deploy/lgtm/config/grafana/provisioning/alerting/rules.yml against the declared events",
    ),
    "tests/scripts/test_alert_single_path.py": (
        "contract",
        "scans every production package for alert writers: its subject is the repository, which no package owns",
    ),
    "tests/scripts/test_audit_branch_protection_contract.py": (
        "contract",
        "the branch-protection audit accepts the real .trunk/trunk.yaml declaration of the full gate",
    ),
    "tests/scripts/test_ci_artifact_policy.py": (
        "contract",
        "reads the artifact-publishing steps of .github/workflows/ci.yml",
    ),
    "tests/scripts/test_ci_secret_scanning.py": (
        "contract",
        "reads the CI secret-scanning policy: ci.yml, .gitleaks.toml, .gitguardian.yml, .pre-commit-config.yaml",
    ),
    "tests/scripts/test_ci_test_selection.py": (
        "contract",
        "reads the test-selection wiring of .github/workflows/ci.yml",
    ),
    "tests/scripts/test_refresh_test_durations_contract.py": (
        "contract",
        "the shard counts and coverage arguments track both workflow matrices of .github/workflows/ci.yml",
    ),
    "tests/scripts/test_rotate_cluster_secret.py": (
        "integration",
        "the rotation script keeps the backup passphrase pinned across the backup service and its artifact layer: spans scripts, services.backup.dump, services.backup.artifact, no one of which may import all the others",
    ),
    "tests/scripts/test_test_selector_contract.py": (
        "contract",
        "every lint-family test file on disk is pinned in the selector, and no pin is stale",
    ),
    "tests/scripts/test_toolchain_uv_pin.py": (
        "contract",
        "keeps the workflows and scripts/provision/toolchain.sh on one uv version",
    ),
    "tests/scripts/test_update_model_pricing_contract.py": (
        "contract",
        "the reconcile tests run against the reviewed catalog base/lm/pricing_catalog_archive.json, and the update-model-pricing workflow runs only trusted main code with write permissions",
    ),
    "tests/scripts/test_worktree_sh_clean.py": (
        "contract",
        "runs scripts/worktree.sh clean as a process against throwaway repositories",
    ),
    "tests/services/test_ava_root_custody_contract.py": (
        "contract",
        "compiles the permissions helper's Swift source with tests/services/helper_child_custody.swift and checks the native reaper cannot race owned signal delivery",
    ),
    "tests/services/test_ava_root_glue_manifests.py": (
        "integration",
        "the ava-root manifest generation from the roster across gateway, ops, browser and ava-root: spans base, gateway, ops, services.supervision.ava_root, services.supervision.ava_root_glue, services.desktop.browser, no one of which may import all the others",
    ),
    "tests/services/test_backup_recovery_contract.py": (
        "contract",
        "asserts the conversation-recovery sources named by services/backup/dump.py, the checkpoint module and db/schema.sql are checkpoint tables, not the events archive",
    ),
    "tests/services/test_backup_scheduler_shutdown.py": (
        "integration",
        "real signal stop of a scheduler blocked in an ops job: spans base, ops, services.backup.scheduler, no one of which may import all the others",
    ),
    "tests/services/test_gate_root.py": (
        "integration",
        "the gate as a native ava-root child with ops and the healthchecks service: spans base, ops, services.supervision.ava_root, services.supervision.healthchecks, no one of which may import all the others",
    ),
    "tests/services/test_hosted_db_wait_liveness.py": (
        "integration",
        "agent-host DB waits against the delivery watchdog's stale scan and force: spans agent, base, services.agent_runner.agent_host, services.wake.delivery_watchdog, no one of which may import all the others",
    ),
    "tests/services/test_maintenance_readiness.py": (
        "integration",
        "readiness of a stopped generation through the cli, the gateway and ops: spans base, cli, gateway, ops, no one of which may import all the others",
    ),
    "tests/skills/test_impersonation_launch.py": (
        "contract",
        "tests the launch scripts of the ava_builtins/skills/platform/ava-guide/external-agents skill",
    ),
    "tests/skills/test_inspect_a_trace_fetch.py": (
        "contract",
        "tests the scripts of the .agents/skills/inspect-a-trace skill",
    ),
    "tests/skills/test_watcher_send_retry.py": (
        "contract",
        "tests the reference watcher scripts of the ava_builtins skills",
    ),
    "tests/skills/use_other_agents/test_coding_session_resume.py": (
        "contract",
        "tests the spawn scripts of the ava_builtins/skills/platform/ava-guide/external-agents skill",
    ),
    "tests/skills/use_other_agents/test_spawn_runtime_compat.py": (
        "contract",
        "tests the spawn scripts of the ava_builtins/skills/platform/ava-guide/external-agents skill",
    ),
    "tests/test_asyncio_stall_probe.py": (
        "contract",
        "tests the CI stall probe, tests/_asyncio_stall_probe.py",
    ),
    "tests/test_ci_rerun_workflow.py": (
        "contract",
        "runs the shell of .github/workflows/ci-rerun.yml against a mock GitHub API",
    ),
    "tests/test_db_check_enum_sync.py": (
        "contract",
        "keeps Python enums in step with the CHECK constraints in db/schema.sql",
    ),
    "tests/test_e2e_residue_sweep.py": (
        "contract",
        "tests the e2e residue reaper, tests/e2e/_proc.py",
    ),
    "tests/test_e2e_truncate_retry.py": (
        "contract",
        "tests the deadlock-retried TRUNCATE helper, tests/e2e/_truncate.py",
    ),
    "tests/test_env_guard_canary.py": (
        "contract",
        "tests that the env guard of the test harness fires on a simulated e2e leak",
    ),
    "tests/test_event_fixtures.py": (
        "contract",
        "the shared wire-format fixtures, tests/fixtures/events, parse through the event adapter and cover every system role",
    ),
    "tests/test_goal_watch_filter.py": (
        "contract",
        "tests the watch_idle reference snippets of the ava-goal, long-running-agent and ava-fleet skills",
    ),
    "tests/test_helperproc.py": (
        "integration",
        "permissions-helper process sessions routed through the agent host: spans base, services.agent_runner.agent_host, services.desktop.permissions_helper, no one of which may import all the others",
    ),
    "tests/test_home_isolation.py": (
        "contract",
        "asserts the harness's env bootstrap, tests/fixtures/env_bootstrap.py, hides the operator's home",
    ),
    "tests/test_lint_contextvars_allowlist.py": (
        "contract",
        "probes the ruff contextvars ban configured in pyproject.toml",
    ),
    "tests/test_lint_event_kinds.py": (
        "contract",
        "scans every production module against the event registry",
    ),
    "tests/test_lint_fixture_scope_contract.py": (
        "contract",
        "the fixture-scope lint reads the real tests/e2e/conftest.py: flags it in its pre-fix shape and when its package init is deleted, and matches its fixture body's env keys",
    ),
    "tests/test_lint_marker_contract.py": (
        "contract",
        "keeps the backend NoteTag enum in step with ui/web/src/components/timeline/markers.tsx",
    ),
    "tests/test_lint_truncate_isolation.py": (
        "contract",
        "keeps the per-test TRUNCATE list in step with db/schema.sql and migrations/",
    ),
    "tests/test_os_job_leak_guard.py": (
        "contract",
        "tests the session OS-job leak guard, tests/_os_jobs.py",
    ),
    "tests/test_pool_keepalives.py": (
        "integration",
        "every long-lived pool of the gateway and agent ops carries the base keepalive kwargs: spans base, gateway, services.agent_runner.agent_ops, no one of which may import all the others",
    ),
    "tests/test_visual_snapshot.py": (
        "contract",
        "tests the visual snapshot helper, tests/e2e/visual/_visual_snapshot.py",
    ),
}
