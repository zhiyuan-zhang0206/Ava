"""`ava` CLI command door — the `cmd_*` implementations `cli.parsers` handlers
(and a few scripts) lazy-import.

Top-level `cli/` package, decoupled from `ava/` SDK — this package does not
import `ava.*`. Audience is ops / developers, not the agent. Entry point
`[project.scripts] ava = "cli.main:main"` is installed to `.venv/bin/ava` by
`uv sync`.

Everything below `cmd_*` is a private implementation detail of its own module —
module-local helpers are not re-exported here. A test seam is patched at the
module that defines the name (e.g. `cli.commands._probe._curl_ok`), never at
this package namespace.
"""

from __future__ import annotations

from cli.commands._cluster_boot_unit import (
    cmd_boot_unit_install,
    cmd_boot_unit_status,
    cmd_boot_unit_uninstall,
)
from cli.commands._cluster_cancel import cmd_cluster_cancel
from cli.commands._cluster_cron import (
    cmd_cron_register,
    cmd_cron_unregister,
)
from cli.commands._cluster_health import cmd_health_probe
from cli.commands._cluster_hold_watchdog import (
    cmd_hold_watchdog,
    cmd_hold_watchdog_register,
    cmd_hold_watchdog_unregister,
)
from cli.commands._cluster_recover import cmd_cluster_recover
from cli.commands._cluster_recover_pending import cmd_cluster_recover_pending
from cli.commands._cluster_rollback import cmd_rollback
from cli.commands._cluster_watchdog_probe import (
    cmd_watchdog_probe,
    cmd_watchdog_probe_register,
    cmd_watchdog_probe_unregister,
)
from cli.commands._converge import cmd_converge
from cli.commands._firewall import (
    cmd_firewall_status,
    cmd_firewall_sync,
)
from cli.commands._grafana_render import cmd_grafana_render
from cli.commands._lgtm import (
    cmd_lgtm_off,
    cmd_lgtm_on,
    cmd_lgtm_status,
)
from cli.commands._pitr_activation import (
    cmd_pitr_activate,
    cmd_pitr_rollback,
    cmd_pitr_status,
)
from cli.commands._update_dispatch import cmd_update
from cli.commands.agents.pty import (
    cmd_pty_freeze,
    cmd_pty_resume,
    cmd_pty_status,
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
from cli.commands.ensure_db_role import cmd_ensure_db_role
from cli.commands.extensions.mcp import (
    cmd_mcp_add,
    cmd_mcp_disable,
    cmd_mcp_enable,
    cmd_mcp_install,
    cmd_mcp_list,
    cmd_mcp_remove,
    cmd_mcp_uninstall,
    cmd_mcp_upgrade,
)
from cli.commands.extensions.packages import (
    cmd_packages_policy,
    cmd_packages_refresh,
    cmd_packages_rollback,
    cmd_packages_status,
)
from cli.commands.extensions.plugins import (
    cmd_plugins_disable,
    cmd_plugins_enable,
    cmd_plugins_install,
    cmd_plugins_installed,
    cmd_plugins_uninstall,
    cmd_plugins_update,
    cmd_plugins_upgrade,
)
from cli.commands.extensions.skill import (
    cmd_skill_disable,
    cmd_skill_enable,
    cmd_skill_install,
    cmd_skill_register,
    cmd_skill_scan,
    cmd_skill_trust,
    cmd_skill_update,
    cmd_skill_upgrade,
)
from cli.commands.logs import (
    cmd_logs_retention,
    cmd_logs_rotate,
)
from cli.commands.pitr import (
    cmd_pitr_drill,
    cmd_pitr_multipart_abort,
    cmd_pitr_multipart_list,
    cmd_pitr_retention_arm,
    cmd_pitr_retention_disable,
    cmd_pitr_retention_inspect,
    cmd_pitr_retention_run_once,
    cmd_pitr_retention_status,
    cmd_pitr_snapshot_archive,
    cmd_pitr_snapshot_retire,
    cmd_pitr_snapshot_verify,
)
from cli.commands.start import cmd_start
from cli.commands.status import cmd_status
from cli.commands.stop import (
    cmd_restart,
    cmd_stop,
)
from cli.commands.trace import cmd_trace_ship

__all__ = [
    "cmd_boot_unit_install",
    "cmd_boot_unit_status",
    "cmd_boot_unit_uninstall",
    "cmd_cluster_cancel",
    "cmd_cluster_destroy",
    "cmd_cluster_down",
    "cmd_cluster_ls",
    "cmd_cluster_mark_staging",
    "cmd_cluster_pause",
    "cmd_cluster_recover",
    "cmd_cluster_recover_pending",
    "cmd_cluster_restart",
    "cmd_cluster_resume",
    "cmd_cluster_status",
    "cmd_converge",
    "cmd_cron_register",
    "cmd_cron_unregister",
    "cmd_ensure_db_role",
    "cmd_firewall_status",
    "cmd_firewall_sync",
    "cmd_grafana_render",
    "cmd_health_probe",
    "cmd_hold_watchdog",
    "cmd_hold_watchdog_register",
    "cmd_hold_watchdog_unregister",
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
    "cmd_packages_policy",
    "cmd_packages_refresh",
    "cmd_packages_rollback",
    "cmd_packages_status",
    "cmd_pitr_activate",
    "cmd_pitr_drill",
    "cmd_pitr_multipart_abort",
    "cmd_pitr_multipart_list",
    "cmd_pitr_retention_arm",
    "cmd_pitr_retention_disable",
    "cmd_pitr_retention_inspect",
    "cmd_pitr_retention_run_once",
    "cmd_pitr_retention_status",
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
]
