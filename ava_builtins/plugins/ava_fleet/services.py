"""ava_fleet — ops service declarations (the plugin's `build_services()` hook).

A plugin that runs its own long-lived gateway/agent-runner daemon declares it
here instead of hardcoding a ServiceSpec into the core `ops/roster.py`: a
plugin ships a `services.py` exposing ``services() -> tuple[ServiceSpec, ...]``,
and `ops.spec._plugin_services()` discovers + folds it into the single
`build_services()` roster (so watchdog keepalive / `ava start` / `ava status`
all still derive from one place). Discovery keys on this plugin's code being
PRESENT on the machine, not the agent-facing enable-state — the cluster-level
on/off is the explicit `AVA_TASK_MAINTENANCE_ENABLED` settings gate below. See
`decisions/2026-07-19-plugin-registered-services.md`.

This module is deliberately light: it imports only the ops service contract and
roster probe helper plus `shared` — never `plugin.py` or the fleet
domain code — so the ops/CLI/watchdog process that discovers it does not pull the
agent kernel in. `services()` is a function (not a module constant) so probe
ports derived from settings / monkeypatched `health_port()` are read at use-time,
matching `build_services()`'s use-time contract.
"""

from __future__ import annotations

from ops.roster import daemon_identity
from ops.service_spec import ServiceSpec
from shared.config import settings
from shared.daemon_health import health_port
from shared.machine import MachineRole

# task-maintenance runs on the gateway capability, like the other cluster-wide
# daemons. Declared here (not reaching into ops's private `_GATEWAY`) so the
# plugin owns its own capability decision.
_GATEWAY: frozenset[MachineRole] = frozenset({"gateway"})


def _task_maintenance_gate() -> str | None:
    """Gate reason for task-maintenance, or None when it will start.

    This is the daemon's cluster-level on/off — `AVA_TASK_MAINTENANCE_ENABLED`, an
    explicit machine/cluster settings field (scope=host), read deterministically at
    daemon-start and unaffected by any per-agent config overlay. Owned by the
    plugin (not `ops.spec._gate_reason`) so the fleet-domain toggle travels with
    the fleet service, and it — not the plugin's agent-facing enable-state — is
    what decides whether task-maintenance runs."""
    if not settings.daemon.task_maintenance_enabled:
        return "disabled (AVA_TASK_MAINTENANCE_ENABLED off)"
    return None


def services() -> tuple[ServiceSpec, ...]:
    """The ops services the fleet plugin contributes to the roster.

    Currently the gateway-side task-maintenance daemon (cluster-wide task
    reminders + escalation). Runs on the gateway capability, like the other
    cluster-wide daemons; its `AVA_TASK_MAINTENANCE_ENABLED` toggle rides its own
    `gate`. The cmd is a venv-direct launch (`.venv/bin/python`, relative to the
    source checkout the service starts in) — no `uv run` wrapper.
    """
    return (
        ServiceSpec(
            session="task-maintenance",
            cmd=".venv/bin/python -m ava_builtins.plugins.ava_fleet.task_maintenance.daemon",
            capabilities=_GATEWAY,
            # assert_schema_current at boot, then it scans the tasks tables — a
            # revive under a dead or drifted DB would just crash-loop it.
            requires_db=True,
            curl_url=f"http://localhost:{health_port('task_maintenance')}/healthz",
            # Same identity contract as every core /healthz daemon: a 2xx is only
            # believed once name/home/pid say the answering process is this unit's.
            identity_probe=daemon_identity(
                "task_maintenance", settings.services.task_maintenance_pidfile
            ),
            healthcheck_module="ava_builtins.plugins.ava_fleet.task_maintenance.healthcheck",
            gate=_task_maintenance_gate,
        ),
    )
