"""Directory-level fixtures that follow the tests, not a `conftest.py`.

A conftest's fixtures reach only the tests below it, so a test moved into a
package's `tests/` directory silently loses them. This table is a migration
device that lets moved tests keep the fixture closure they had; the end state is
each package's tests declaring the environment they need, or a local conftest
providing it, so entries only come out (the entry count is the number of
environment dependencies not yet made explicit).

Each fixture module in `tests/path_scoped/` is registered for the paths listed in
`PATH_SCOPES`, the way pytest registers a conftest for its directory: the module's
fixtures bind to the collector node of the directory or test file, so the autouse
names, their order, their visibility and their override chain are exactly a
conftest's, and a session-scoped autouse fixture is instantiated only for the tests
under the path.

Moving a test: edit `paths` in its `PATH_SCOPES` entry (directories or single test
files, relative to the repo root, forward slashes); list the new path next to the old
one while both exist. A path may be as narrow as one file, so tests that came from
different directories can sit together. `test_files` does not change on a move: it
counts the test files the paths hold, and `tests/ci/test_path_scopes.py` fails when
fewer are found, which is what a test moved out without its new path listed looks
like. Nothing else changes: the fixture modules do not know where their tests live.
A path that does not exist stops the run.

Depends on `FixtureManager.parsefactories(holder=, node=)`, the semi-internal
interface pytest's own conftest handling uses. `tests/ci/test_path_scopes.py` locks
its signature and behavior; a pytest upgrade (which needs manual approval) must
re-check it.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import NamedTuple

import pytest


class Scope(NamedTuple):
    paths: tuple[str, ...]  # directories or test files whose tests the module governs
    test_files: int  # how many test files those paths hold; unchanged by a move


# Fixture module -> its scope. One module per former conftest, so the autouse names
# register in one alphabetical batch, as in a conftest.
PATH_SCOPES: dict[str, Scope] = {
    "tests.path_scoped.agent_tests": Scope(
        (
            "tests/agent",
            "agent/db/tests",
            "agent/graph/exec/tests",
            "agent/graph/llm/tests",
            "agent/graph/tests",
            "agent/hooks/tests",
            "agent/llm/tests",
            "agent/messages/tests",
            "agent/ownership/tests",
            "agent/startup/tests",
            "agent/tests/test_checkpoint_interval.py",
            "agent/tests/test_exec_child.py",
            "agent/tests/test_exec_subprocess.py",
            "agent/tests/test_history_dump.py",
            "agent/tests/test_lazy_child_imports.py",
            "agent/tests/test_messages_delta_switch.py",
            "agent/tests/test_plugin_catalog.py",
            "agent/tests/test_plugin_load_containment.py",
            "agent/tests/test_state_slot.py",
            "ava_builtins/plugins/ava_code/tests",
            "ava_builtins/plugins/ava_fleet/tests/test_ava_fleet_plugin.py",
            "ava_builtins/plugins/ava_fleet/tests/test_task_registry.py",
            "ava_builtins/plugins/ava_silent_idle/tests",
            "ava_builtins/plugins/ava_syntax_fix/tests",
            "ava_builtins/plugins/lm_google/tests/test_llm_decode_ms.py",
            "ava_builtins/plugins/tests/test_ava_sdk_reminder_plugin.py",
            "ava_builtins/plugins/tests/test_sysprompt_verbosity_regression.py",
            "base/agents/incarnation/tests/test_resources.py",
            "base/agents/messages/tests/test_caller_identity.py",
            "base/agents/messages/tests/test_envelope.py",
            "base/config/tests/test_events_channel_isolation.py",
            "base/deploy/tests/test_maintenance_cold.py",
            "base/events/live/tests/test_event_publisher_drop.py",
            "base/events/live/tests/test_events_wire_format.py",
            "base/lm/tests/test_llm_factory.py",
            "base/lm/tests/test_llm_factory_new_models.py",
            "base/lm/tests/test_provider_errors.py",
            "base/lm/tests/test_provider_stop.py",
            "services/agent_host/tests/test_boot_extension_materialize.py",
            "services/agent_host/tests/test_circuit_breaker.py",
            "services/agent_host/tests/test_hosted_checkpoint_diagnostics.py",
            "services/agent_host/tests/test_hosted_cold_history.py",
            "services/agent_host/tests/test_hosted_compact_failure.py",
            "services/agent_host/tests/test_hosted_db_recovery.py",
            "services/agent_host/tests/test_hosted_failure_settlement_recovery.py",
            "services/agent_host/tests/test_maintenance_prior_failure.py",
            "services/agent_host/tests/test_maintenance_recovery.py",
            "services/agent_host/tests/test_pooled_checkpoint.py",
            "services/agent_host/tests/test_reconcile_after_abort.py",
            "services/agent_host/tests/test_recovery_interrupt.py",
            "agent/tests/test_checkpoint_serde.py",
            "ava_builtins/plugins/ava_memory/tests/test_process_boot.py",
            "services/agent_host/tests/test_hosted_db_flush_recovery.py",
            "services/agent_host/tests/test_hosted_force_terminate_close.py",
            "services/agent_host/tests/test_hosted_trace_checkpoint.py",
        ),
        128,
    ),
    "tests.path_scoped.ava_tests": Scope(
        (
            "tests/ava",
            "agent/graph/claim/tests",
            "agent/tests/test_lazy_child_telemetry.py",
            "agent/tests/test_register_namespace.py",
            "ava/agents/tests/test_agents_scan.py",
            "ava/agents/tests/test_list_machines.py",
            "ava/gateway_client/tests",
            "ava/mcps/tests",
            "ava/sdk_surface/tests",
            "ava/shell/tests/test_shell.py",
            "ava/shell/tests/test_shell_background.py",
            "ava/shell/tests/test_shell_scan.py",
            "ava/shell/tests/test_shell_ttl.py",
            "ava/tests/test_agent_identity.py",
            "ava/tests/test_attachment_transport.py",
            "ava/tests/test_files.py",
            "ava/tests/test_lazy_boot_import.py",
            "ava/tests/test_lazy_connection.py",
            "ava/tests/test_lazy_lm_import.py",
            "ava/tests/test_machine_surface.py",
            "ava/tests/test_mcp_config.py",
            "ava/tests/test_mcp_config_enable_filter.py",
            "ava/tests/test_mcps.py",
            "ava/tests/test_sdk_disable.py",
            "ava/tests/test_sdk_redis_resilience.py",
            "ava/tests/test_security.py",
            "ava/tests/test_self_compact_publish.py",
            "ava/tests/test_self_machine_spec.py",
            "ava/tests/test_session_venv_projection.py",
            "ava/tests/test_skills.py",
            "ava/tests/test_understand.py",
            "ava/tests/test_watcher.py",
            "ava/tests/test_watcher_plugin_load.py",
            "ava/tests/test_web.py",
            "ava_builtins/plugins/ava_fleet/tests/test_lazy_plugin_import.py",
            "ava_builtins/plugins/ava_memory/tests/test_memory.py",
            "ava_builtins/skills/tests/test_self_evolution_collect.py",
            "ava_builtins/skills/tests/test_self_evolution_daily_scan.py",
            "ava_builtins/skills/tests/test_self_evolution_mirror_backfill.py",
            "ava_builtins/skills/tests/test_self_evolution_record.py",
            "base/config/tests/test_seed_guard.py",
            "base/deploy/schema/tests",
            "base/deploy/tests/test_migration_reset.py",
            "base/deploy/tests/test_migrations.py",
            "gateway/schedules/tests/test_schedule_manager_pty.py",
            "services/agent_host/tests/test_hosted_dispatcher_cancellation.py",
            "ava/tests/test_composer_commands.py",
            "agent/tests/test_external.py",
            "gateway/tests/test_presets_sdk.py",
            "gateway/tests/test_ui.py",
        ),
        65,
    ),
    "tests.path_scoped.cli_tests": Scope(
        (
            "tests/cli",
            "base/packages/tests",
            "cli/commands/agents/tests",
            "cli/commands/cluster/tests",
            "cli/commands/converge/tests",
            "cli/commands/data_plane/tests/test_cluster_instance_bind.py",
            "cli/commands/data_plane/tests/test_pg_socket_dir.py",
            "cli/commands/data_plane/tests/test_pgbouncer.py",
            "cli/commands/data_plane/tests/test_pgbouncer_reachable_bind_wait.py",
            "cli/commands/data_plane/tests/test_pgbouncer_stop_isolation.py",
            "cli/commands/data_plane/tests/test_pooler_stop.py",
            "cli/commands/data_plane/tests/test_status_data_plane.py",
            "cli/commands/extensions/tests",
            "cli/commands/lifecycle/tests/test_checkpoint_migration_phase.py",
            "cli/commands/lifecycle/tests/test_commands_restart_stop.py",
            "cli/commands/lifecycle/tests/test_commands_start.py",
            "cli/commands/lifecycle/tests/test_commands_status.py",
            "cli/commands/lifecycle/tests/test_extension_materialize_ordering.py",
            "cli/commands/lifecycle/tests/test_maintenance_stop_report.py",
            "cli/commands/lifecycle/tests/test_remote_data_plane.py",
            "cli/commands/lifecycle/tests/test_rollout_robustness.py",
            "cli/commands/lifecycle/tests/test_start_data_plane.py",
            "cli/commands/lifecycle/tests/test_start_generation.py",
            "cli/commands/lifecycle/tests/test_start_health_port_gate.py",
            "cli/commands/lifecycle/tests/test_start_partial_stop.py",
            "cli/commands/lifecycle/tests/test_start_readiness_gate.py",
            "cli/commands/lifecycle/tests/test_start_readiness_preflight.py",
            "cli/commands/lifecycle/tests/test_stop_extras.py",
            "cli/commands/lifecycle/tests/test_stop_terminals.py",
            "cli/commands/management/tests/test_config_cmd.py",
            "cli/commands/observability/tests/test_converge_otel_collector.py",
            "cli/commands/observability/tests/test_lgtm_toggle.py",
            "cli/commands/observability/tests/test_observatory_urls.py",
            "cli/commands/observability/tests/test_otel_port_deviation.py",
            "cli/commands/observability/tests/test_trace_ship.py",
            "cli/commands/tests/test_external_skills.py",
            "cli/commands/tests/test_external_skills_adversarial.py",
            "cli/commands/tests/test_external_skills_crash_safety.py",
            "cli/commands/tests/test_frontend_deps.py",
            "cli/commands/tests/test_gateway_preflight_budget.py",
            "cli/commands/tests/test_lgtm_native.py",
            "cli/commands/tests/test_repo_browser_gate.py",
            "cli/commands/tests/test_repo_heartbeat_gate.py",
            "cli/commands/tests/test_service_probe_ports.py",
            "cli/commands/tests/test_session_naming.py",
            "cli/parsers/tests",
            "cli/tests/test_boot_retry.py",
            "cli/tests/test_cli_groups.py",
            "cli/tests/test_cluster_health_window.py",
            "cli/tests/test_commands_register.py",
            "cli/tests/test_lite_verbs.py",
            "cli/tests/test_logs_rotate.py",
            "cli/tests/test_main_log_init.py",
            "cli/tests/test_mcp_install.py",
            "cli/tests/test_mcp_serve.py",
            "cli/tests/test_memory_cmd.py",
            "cli/tests/test_presets_cmd.py",
            "cli/tests/test_pty_commands.py",
            "cli/tests/test_python_install.py",
            "cli/tests/test_redis_binary_selection.py",
            "cli/tests/test_settings_failure.py",
            "cli/tests/test_skill_install.py",
            "cli/tests/test_start_identity.py",
            "cli/tests/test_start_runtime.py",
            "ops/roster/tests/test_pty_sessions_wiring.py",
            "scripts/tests/test_start_agent_bearer.py",
            "services/ava_root/tests/test_maintenance_late_child.py",
            "services/agent_host/tests/test_agent_host_identity_probe.py",
        ),
        114,
    ),
    "tests.path_scoped.db_authority_tests": Scope(
        (
            "tests/lifecycle/db_authority",
            "base/cluster/authority/tests",
            "base/tests/test_delivery.py",
            "cli/commands/tests/test_single_box.py",
        ),
        9,
    ),
    "tests.path_scoped.gateway_tests": Scope(
        (
            "tests/gateway",
            "ava_builtins/plugins/tests/test_agent_inspect_metrics.py",
            "ava_builtins/plugins/tests/test_agent_inspect_widgets.py",
            "base/agents/tests/test_log_sink.py",
            "base/packages/plugins/tests/test_enable_config.py",
            "base/paths/tests",
            "base/telemetry/metrics/tests/test_metrics_aggregate_equivalence.py",
            "gateway/agents/tests/test_completion_notice_flusher.py",
            "gateway/agents/tests/test_context_breakdown.py",
            "gateway/agents/tests/test_inbound_provenance.py",
            "gateway/agents/tests/test_max_id_gauge.py",
            "gateway/agents/tests/test_resurrect_forward.py",
            "gateway/agents/tests/test_spawn_forward.py",
            "gateway/agents/tests/test_timeline.py",
            "gateway/alerts/tests",
            "gateway/auth/tests/test_session_store.py",
            "gateway/cluster/tests/test_loki_shards.py",
            "gateway/cluster/tests/test_status_services.py",
            "gateway/extensions/tests",
            "gateway/inspect/tests",
            "gateway/lgtm/tests/test_loki_events.py",
            "gateway/lgtm/tests/test_prom_metrics.py",
            "gateway/lgtm/tests/test_telemetry_staleness.py",
            "gateway/routers/tests",
            "gateway/run_timeline/tests",
            "gateway/schedules/tests/test_schedule_manager.py",
            "gateway/tests",
            "gateway/ttl_reaper/tests",
            "ops/agents/tests/test_caller_write_fence.py",
            "ops/tests/test_cluster_rpc.py",
            "ops/tests/test_inventory_ops.py",
            "ops/tests/test_operations.py",
        ),
        116,
    ),
    "tests.path_scoped.integration_tests": Scope(
        (
            "tests/integration",
            "gateway/tests/test_agent_launch_retry_sdk.py",
            "ava/tests/test_core.py",
            "base/agents/impersonation/tests/test_impersonation_replay_content_identity.py",
            "base/cluster/dataplane/tests/test_vendored_binaries.py",
            "cli/commands/management/tests/test_config_provisioning_surface.py",
            "gateway/agents/tests/test_agent_launch_visibility.py",
            "gateway/lgtm/tests/test_manifest_serializer_contract.py",
            "ops/lifecycle/tests/test_agent_launch_runner.py",
            "ops/tests/test_cross_machine_dispatch.py",
        ),
        17,
    ),
    "tests.path_scoped.services_tests": Scope(
        (
            "tests/services",
            "base/deploy/maintenance/tests/test_maintenance_failures.py",
            "base/native_process/tests/test_ava_root_supervisor_reaping.py",
            "base/telemetry/tests/test_tracing.py",
            "ops/agent_pause/tests/test_daemon_shutdown_bound.py",
            "ops/agent_pause/tests/test_delivery_watchdog_shutdown.py",
            "ops/agent_pause/tests/test_im_bridge_shutdown.py",
            "ops/agent_pause/tests/test_labeler_shutdown.py",
            "ops/tests/test_healthcheck_page_server.py",
            "services/agent_host/tests/test_agent_host_abort_reconcile.py",
            "services/agent_host/tests/test_agent_host_boot_defer.py",
            "services/agent_host/tests/test_agent_host_log_rotate.py",
            "services/agent_host/tests/test_agent_host_pages.py",
            "services/agent_host/tests/test_agent_host_plugins.py",
            "services/agent_host/tests/test_agent_host_pool_release.py",
            "services/agent_host/tests/test_agent_host_recrash_reap.py",
            "services/agent_host/tests/test_agent_host_shutdown.py",
            "services/agent_host/tests/test_agent_host_turn_reconcile.py",
            "services/agent_host/tests/test_crash_recovery.py",
            "services/agent_host/tests/test_host_pool_capacity.py",
            "services/agent_host/tests/test_host_turn_progress_publish.py",
            "services/agent_host/tests/test_hosted_idle_recovery.py",
            "services/agent_host/tests/test_turn_admission.py",
            "services/agent_host/tests/test_turn_dispatcher.py",
            "services/agent_ops/tests",
            "services/ava_root/tests/test_ava_root_health.py",
            "services/ava_root/tests/test_ava_root_probes.py",
            "services/ava_root/tests/test_ava_root_selfcheck.py",
            "services/ava_root/tests/test_ava_root_stop_window.py",
            "services/ava_root/tests/test_ava_root_wiring.py",
            "services/ava_root_glue/tests",
            "services/backup_scheduler/tests",
            "services/browser/tests",
            "services/computer/tests/test_computer_mcp_daemon.py",
            "services/computer/tests/test_computer_mcp_wrapper.py",
            "services/computer/tests/test_computer_ocr.py",
            "services/computer/tests/test_computer_session.py",
            "services/computer/tests/test_computer_task_sessions.py",
            "services/delivery_watchdog/tests",
            "services/events_maintenance/tests",
            "services/gate/tests",
            "services/gateway_side/backup/tests",
            "services/gateway_side/walg/tests",
            "services/healthchecks/tests",
            "services/heartbeat/tests/test_heartbeat_daemon.py",
            "services/heartbeat/tests/test_heartbeat_liveness.py",
            "services/heartbeat/tests/test_station_healthcheck.py",
            "services/hierarchy_worker/tests/test_hierarchy_worker.py",
            "services/im_bridge/adapters/tests/test_im_bridge_telegram.py",
            "services/im_bridge/adapters/tests/test_services_weixin_adapter.py",
            "services/im_bridge/tests",
            "services/labeler/tests",
            "services/memory_indexer/backends/tests",
            "services/memory_indexer/embeddings/tests",
            "services/memory_indexer/tests",
            "services/memory_search/tests",
            "services/page_server/tests",
            "services/permissions_helper/tests/test_permissions_helper.py",
            "services/redis_bridge/tests",
            "services/tests",
            "services/agent_host/tests/test_hosted_backlog_recovery.py",
        ),
        128,
    ),
    "tests.path_scoped.structure_tests": Scope(
        (
            "tests/scripts/structure",
            "scripts/audit/tests/test_audit_split_reexports.py",
            "scripts/lint/tests/test_baseline_shard_validity_gate.py",
            "scripts/lint/tests/test_directory_budget_entries.py",
            "scripts/lint/tests/test_lint_code_structure.py",
            "scripts/lint/tests/test_locality_gate.py",
            "scripts/lint/tests/test_patch_targets_baseline.py",
            "scripts/lint/tests/test_rename_aware_baseline.py",
            "scripts/lint/tests/test_tests_dir_scope.py",
            "scripts/structure/tests",
            "scripts/tests/test_patch_targets.py",
        ),
        16,
    ),
}


def modules_by_path(scopes: dict[str, Scope]) -> dict[str, list[str]]:
    by_path: dict[str, list[str]] = {}
    for module, scope in scopes.items():
        for path in scope.paths:
            by_path.setdefault(path, []).append(module)
    return by_path


def scope_problems(scopes: dict[str, Scope], root: Path) -> list[str]:
    """What is wrong with the table: a missing path, or fewer test files than recorded."""
    problems: list[str] = []
    for module, scope in scopes.items():
        missing = [path for path in scope.paths if not (root / path).exists()]
        if missing:
            problems.append(f"{module}: paths do not exist: {missing}")
            continue
        found = {
            file.resolve()
            for path in scope.paths
            for file in (
                [root / path] if (root / path).is_file() else (root / path).rglob("test_*.py")
            )
        }
        if len(found) < scope.test_files:
            problems.append(
                f"{module}: its paths hold {len(found)} test files, {scope.test_files} are "
                "recorded; a test that moved without its new path listed here has lost "
                "these fixtures"
            )
    return problems


_MODULES_BY_PATH = modules_by_path(PATH_SCOPES)


def pytest_configure(config: pytest.Config) -> None:
    missing = sorted(path for path in _MODULES_BY_PATH if not (config.rootpath / path).exists())
    if missing:
        raise pytest.UsageError(
            f"tests/fixtures/path_scopes.py names paths that do not exist: {missing}. "
            "A moved test directory or file must be renamed in PATH_SCOPES, or its "
            "fixtures stop applying."
        )


def pytest_collectstart(collector: pytest.Collector) -> None:
    for module in _MODULES_BY_PATH.get(collector.nodeid, ()):
        collector.session._fixturemanager.parsefactories(
            holder=importlib.import_module(module), node=collector
        )
