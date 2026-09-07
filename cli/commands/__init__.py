"""`ava` CLI implementation — composed from per-command sub-modules.

Top-level `cli/` package, decoupled from `ava/` SDK — this package does not
import `ava.*`. Audience is ops / developers, not the agent. Entry point
`[project.scripts] ava = "cli.main:main"` is installed to `.venv/bin/ava` by
`uv sync`.

Sub-module layout:
- private helpers: `_repo`, `_compose`, `_session_lifecycle`, `_probe`, `_setup`
- one module per command: `start`, `stop`, `status`, `update`, `cluster`,
  `plugins`, `skill`, `auth`, `machine`
- `migrations`: not a command — `cmd_migrations_apply` is called as a step of
  `start` (pending migrations are applied during `ava start`)

`stdlib` re-exports (`subprocess`, `os`) sit here so existing
`monkeypatch.setattr("cli.commands.subprocess.run", ...)` calls keep
working at the old namespace.
"""

from __future__ import annotations

# stdlib re-exports — pytest monkeypatches `cli.commands.subprocess.run` /
# `cli.commands.os.kill` / `cli.commands.time.sleep` to stub out subprocess /
# signal / sleep calls in unit tests.
import os
import subprocess
import time

from cli.commands._cluster_cancel import cmd_cluster_cancel
from cli.commands._cluster_cron import cmd_cron_register, cmd_cron_unregister
from cli.commands._cluster_health import cmd_health_probe
from cli.commands._cluster_recover import cmd_cluster_recover
from cli.commands._cluster_rollback import cmd_rollback
from cli.commands._cluster_watchdog_probe import (
    cmd_watchdog_probe,
    cmd_watchdog_probe_register,
    cmd_watchdog_probe_unregister,
)
from cli.commands._converge import cmd_converge, converge_host
from cli.commands._firewall import cmd_firewall_status, cmd_firewall_sync
from cli.commands._gateway_ready import (
    GatewayReadiness,
    gateway_readiness_detail,
)
from cli.commands._gateway_ready import (
    await_gateway_serving as _await_gateway_serving,
)
from cli.commands._lgtm import cmd_lgtm_off, cmd_lgtm_on, cmd_lgtm_status
from cli.commands._pitr_activation import cmd_pitr_activate, cmd_pitr_rollback, cmd_pitr_status
from cli.commands._probe import (
    OccupiedPort,
    ReadinessWait,
    ServiceProbe,
    _cluster_pin_status,
    _curl_ok,
    _detect_prod_source_drift,
    _husk_session_reason,
    _notify_non_critical_unready_services,
    _occupied_health_ports,
    _pid_alive,
    _pidfile_path,
    _print_non_critical_unready_services,
    _print_service_row,
    _print_unready_services,
    _probe_service,
    _recovered_non_critical_specs,
    _resolve_recovered_non_critical_alerts,
    _tcp_alive,
    _wait_for_services_ready,
)
from cli.commands._repo import (
    GATEWAY_PROBE_PATH,
    GatewayProbe,
    ServiceSpec,
    _assert_schema_current_or_die,
    _ensure_frontend_deps,
    _preflight_probes,
    _probe_gateway_or_die,
    _register_machine_or_die,
    _repo_root,
    _roles_or_none,
    _services_for_roles,
    _services_for_roles_annotated,
    build_services,
    probe_gateway_once,
    session_name,
)
from cli.commands._session_lifecycle import (
    _graceful_kill_session,
    _has_session,
    _kill_session,
    _launch_roster,
    _launch_sessions,
    _new_session,
)
from cli.commands._setup import (
    _SETUP_FIELDS,
    _collect_setup_values,
    _print_missing_setup_error,
    _resolve_setup_field,
    _SetupField,
)
from cli.commands.cluster import (
    cmd_cluster_mark_staging,
    cmd_cluster_pause,
    cmd_cluster_restart,
    cmd_cluster_resume,
    cmd_cluster_status,
)
from cli.commands.cluster_lifecycle import (
    cmd_cluster_destroy,
    cmd_cluster_down,
    cmd_cluster_ls,
)
from cli.commands.config import cmd_config_get, cmd_config_set, cmd_config_unset
from cli.commands.ensure_db_role import cmd_ensure_db_role
from cli.commands.logs import cmd_logs_retention, cmd_logs_rotate
from cli.commands.mcp import (
    cmd_mcp_add,
    cmd_mcp_disable,
    cmd_mcp_enable,
    cmd_mcp_install,
    cmd_mcp_list,
    cmd_mcp_remove,
    cmd_mcp_uninstall,
    cmd_mcp_upgrade,
)
from cli.commands.migrations import cmd_migrations_apply
from cli.commands.pitr import (
    cmd_pitr_retention_inspect,
    cmd_pitr_snapshot_archive,
    cmd_pitr_snapshot_retire,
    cmd_pitr_snapshot_verify,
)
from cli.commands.plugins import (
    cmd_plugins_disable,
    cmd_plugins_enable,
    cmd_plugins_install,
    cmd_plugins_installed,
    cmd_plugins_uninstall,
    cmd_plugins_update,
    cmd_plugins_upgrade,
)
from cli.commands.pty import cmd_pty_freeze, cmd_pty_resume, cmd_pty_status
from cli.commands.skill import (
    cmd_skill_disable,
    cmd_skill_enable,
    cmd_skill_install,
    cmd_skill_register,
    cmd_skill_scan,
    cmd_skill_trust,
    cmd_skill_update,
    cmd_skill_upgrade,
)
from cli.commands.start import _cmd_start_body, cmd_start
from cli.commands.status import cmd_status
from cli.commands.stop import (
    _do_stop,
    _reap_cluster_chrome,
    _release_self_heal_pause,
    cmd_pause,
    cmd_restart,
    cmd_stop,
)
from cli.commands.trace import cmd_trace_ship
from cli.commands.update import (
    _CONVERGING_TIMEOUT_S,
    _PHASE_A_TIMEOUT_S,
    _PHASE_B_TIMEOUT_S,
    _POLL_INTERVAL_S,
    _POLL_TIMEOUT_S,
    _STAGE_NO_PROGRESS_S,
    POLL_CONVERGING,
    POLL_NO_PROGRESS,
    POLL_OK,
    POLL_STALLED,
    GitPullFailed,
    GitPullResult,
    PollVerdict,
    _changed_paths_vs_origin,
    _classify_change,
    _dispatch_one_and_wait,
    _fan_out,
    _list_agent_runners,
    _poll_until_unpaused,
    _probe_one_until_unpaused,
    _quiesce_all_agents,
    _quiesce_local_agents,
    _resolve_fanout_targets,
    _restart_frontend_session,
    _run_agent_runner_self_update,
    _run_frontend_only_update,
    _run_gateway_local_update,
    _run_gateway_orchestration,
    apply_pending_migrations,
    cmd_update,
    dry_run_checks,
    estimate_maintenance_window,
    git_pull_main,
)
from cli.commands.update import (
    _poll_verdict_detail as _poll_verdict_detail,
)

# Tests do `monkeypatch.setattr(_cli.settings, "field", value)` to override
# Settings for a single test; re-export so the namespace works.
from shared.config import settings

__all__ = [
    "GATEWAY_PROBE_PATH",
    "POLL_CONVERGING",
    "POLL_NO_PROGRESS",
    "POLL_OK",
    "POLL_STALLED",
    "_CONVERGING_TIMEOUT_S",
    "_PHASE_A_TIMEOUT_S",
    "_PHASE_B_TIMEOUT_S",
    "_POLL_INTERVAL_S",
    "_POLL_TIMEOUT_S",
    "_SETUP_FIELDS",
    "_STAGE_NO_PROGRESS_S",
    "GatewayProbe",
    "GatewayReadiness",
    "GitPullFailed",
    "GitPullResult",
    "OccupiedPort",
    "PollVerdict",
    "ReadinessWait",
    "ServiceProbe",
    "ServiceSpec",
    "_SetupField",
    "_assert_schema_current_or_die",
    "_await_gateway_serving",
    "_changed_paths_vs_origin",
    "_classify_change",
    "_cluster_pin_status",
    "_cmd_start_body",
    "_collect_setup_values",
    "_curl_ok",
    "_detect_prod_source_drift",
    "_dispatch_one_and_wait",
    "_do_stop",
    "_ensure_frontend_deps",
    "_fan_out",
    "_graceful_kill_session",
    "_has_session",
    "_husk_session_reason",
    "_kill_session",
    "_launch_roster",
    "_launch_sessions",
    "_list_agent_runners",
    "_new_session",
    "_notify_non_critical_unready_services",
    "_occupied_health_ports",
    "_pid_alive",
    "_pidfile_path",
    "_poll_until_unpaused",
    "_preflight_probes",
    "_print_missing_setup_error",
    "_print_non_critical_unready_services",
    "_print_service_row",
    "_print_unready_services",
    "_probe_gateway_or_die",
    "_probe_one_until_unpaused",
    "_probe_service",
    "_quiesce_all_agents",
    "_quiesce_local_agents",
    "_reap_cluster_chrome",
    "_recovered_non_critical_specs",
    "_register_machine_or_die",
    "_release_self_heal_pause",
    "_repo_root",
    "_resolve_fanout_targets",
    "_resolve_recovered_non_critical_alerts",
    "_resolve_setup_field",
    "_restart_frontend_session",
    "_roles_or_none",
    "_run_agent_runner_self_update",
    "_run_frontend_only_update",
    "_run_gateway_local_update",
    "_run_gateway_orchestration",
    "_services_for_roles",
    "_services_for_roles_annotated",
    "_tcp_alive",
    "_wait_for_services_ready",
    "apply_pending_migrations",
    "build_services",
    "cmd_cluster_cancel",
    "cmd_cluster_destroy",
    "cmd_cluster_down",
    "cmd_cluster_ls",
    "cmd_cluster_mark_staging",
    "cmd_cluster_pause",
    "cmd_cluster_recover",
    "cmd_cluster_restart",
    "cmd_cluster_resume",
    "cmd_cluster_status",
    "cmd_config_get",
    "cmd_config_set",
    "cmd_config_unset",
    "cmd_converge",
    "cmd_cron_register",
    "cmd_cron_unregister",
    "cmd_ensure_db_role",
    "cmd_firewall_status",
    "cmd_firewall_sync",
    "cmd_health_probe",
    "cmd_lgtm_off",
    "cmd_lgtm_on",
    "cmd_lgtm_status",
    "cmd_logs_retention",
    "cmd_logs_rotate",
    "cmd_mcp_add",
    "cmd_mcp_disable",
    "cmd_mcp_enable",
    "cmd_mcp_install",
    "cmd_mcp_list",
    "cmd_mcp_remove",
    "cmd_mcp_uninstall",
    "cmd_mcp_upgrade",
    "cmd_migrations_apply",
    "cmd_pause",
    "cmd_pitr_activate",
    "cmd_pitr_retention_inspect",
    "cmd_pitr_rollback",
    "cmd_pitr_snapshot_archive",
    "cmd_pitr_snapshot_retire",
    "cmd_pitr_snapshot_verify",
    "cmd_pitr_status",
    "cmd_plugins_disable",
    "cmd_plugins_enable",
    "cmd_plugins_install",
    "cmd_plugins_installed",
    "cmd_plugins_uninstall",
    "cmd_plugins_update",
    "cmd_plugins_upgrade",
    "cmd_pty_freeze",
    "cmd_pty_resume",
    "cmd_pty_status",
    "cmd_restart",
    "cmd_rollback",
    "cmd_skill_disable",
    "cmd_skill_enable",
    "cmd_skill_install",
    "cmd_skill_register",
    "cmd_skill_scan",
    "cmd_skill_trust",
    "cmd_skill_update",
    "cmd_skill_upgrade",
    "cmd_start",
    "cmd_status",
    "cmd_stop",
    "cmd_trace_ship",
    "cmd_update",
    "cmd_watchdog_probe",
    "cmd_watchdog_probe_register",
    "cmd_watchdog_probe_unregister",
    "converge_host",
    "dry_run_checks",
    "estimate_maintenance_window",
    "gateway_readiness_detail",
    "git_pull_main",
    "os",
    "probe_gateway_once",
    "session_name",
    "settings",
    "subprocess",
    "time",
]
