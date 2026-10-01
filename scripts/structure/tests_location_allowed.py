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
    "tests/agent/test_hosted_resurrection_fences.py": (
        "integration",
        "agent and ops are peers: both sides write the same lifecycle row and fence each other",
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
    "tests/base/test_crash_row_writers.py": (
        "contract",
        "scans every production module for writers of the crash-row predicate fields",
    ),
    "tests/base/test_retired_schema_markers.py": (
        "contract",
        "reads migrations/ for the retired-marker drop and its rollback",
    ),
    "tests/base/test_runner_role.py": (
        "contract",
        "asserts the ava_runner capability matrix declared in db/schema.sql",
    ),
    "tests/base/test_schedule_timezone.py": (
        "contract",
        "scans the schedules/ templates for the cluster wall clock",
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
    "tests/cli/test_agent_profile_launch_env.py": (
        "integration",
        "one pipeline test: cli root driver, ops spec and the services ava_root_glue manifest",
    ),
    "tests/cli/test_maintenance_readiness.py": (
        "integration",
        "cli start against a real gateway app: cli and gateway are peers",
    ),
    "tests/gateway/test_schemas_wire_format.py": (
        "contract",
        "freezes the wire schemas against ui/web/openapi.json and ui/web/src/lib/types.ts",
    ),
    "tests/ops/test_resurrection_admission.py": (
        "integration",
        "agent and ops are peers: admission and resurrect race on the same rows",
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
    "tests/scripts/test_alert_rules.py": (
        "contract",
        "validates deploy/lgtm/config/grafana/provisioning/alerting/rules.yml",
    ),
    "tests/scripts/test_alert_rules_backup_custody.py": (
        "contract",
        "validates the backup-custody rules of deploy/lgtm/config/grafana/provisioning/alerting/rules.yml",
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
    "tests/scripts/test_toolchain_uv_pin.py": (
        "contract",
        "keeps the workflows and scripts/provision/toolchain.sh on one uv version",
    ),
    "tests/skills/test_ci_watcher.py": (
        "contract",
        "tests the ci_watcher script of the .agents/skills/ship-a-change skill",
    ),
    "tests/skills/test_impersonation_launch.py": (
        "contract",
        "tests the launch scripts of the ava_builtins/skills/ava-use-other-agents skill",
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
        "tests the spawn scripts of the ava_builtins/skills/ava-use-other-agents skill",
    ),
    "tests/skills/use_other_agents/test_spawn_runtime_compat.py": (
        "contract",
        "tests the spawn scripts of the ava_builtins/skills/ava-use-other-agents skill",
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
    "tests/test_qa_approved_gate_workflow.py": (
        "contract",
        "runs the shell of .github/workflows/qa-approved-gate.yml against a mock GitHub API",
    ),
    "tests/test_visual_snapshot.py": (
        "contract",
        "tests the visual snapshot helper, tests/e2e/_visual_snapshot.py",
    ),
}
