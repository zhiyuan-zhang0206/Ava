"""The daemon /healthz port fields of `ServiceSettings`.

Moved out of `base/config/domains/services/settings.py` when the task #3696 field additions
pushed that module past its 800-line hard ceiling (the same split pattern as
`delivery_watchdog_fields.py`, #2624). Mixed into `ServiceSettings` — NOT a
config domain: `settings.services.*`, every alias/scope/capability face, and
the `.env` contract stay exactly as they were.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from base.host.env.port_table import FIXED_PORTS

# ── daemon /healthz ports — host scope ───────────────────────────────────
#
# `host`, not `cluster-pinned`: a health port is a fact of the machine's
# localhost namespace, and the default is the fixed port table
# (`base.host.env.port_table`). The runner computes its own ops URL from its own
# the `ops` row of `ServiceEndpoints` and registers it (`base/cluster/machines.py`), and the
# gateway reads that URL back off the machines row. So the gateway does not serve
# these to runners over /api/bootstrap, and a runner's .env never caches a
# gateway-served value. `ava start` refuses to launch onto a port another home's
# daemon already answers on. A daemon's healthz URL and pidfile are derived from
# its name (`base.daemon.health.healthz_url`, `base.paths.pid_path`), not configured.


def health_port_field(name: str, *, capability: str | None = None, writable: bool = False) -> Any:
    """The `<name>_health_port` field of a standard `/healthz` daemon.

    `name` is the daemon's health name (a key of the fixed port table); the
    alias is `AVA_<NAME>_HEALTH_PORT` and the described default is the table's.
    """
    extra: dict[str, Any] = {} if capability is None else {"capability": capability}
    extra |= {
        "restart_required": "",
        "writable": writable,
        "sensitive": False,
        "scope": "host",
        "remote_writable": False,
    }
    return Field(
        default=None,
        alias=f"AVA_{name.upper()}_HEALTH_PORT",
        description=(
            f"{name.replace('_', ' ').capitalize()} daemon /healthz port override (per unit). "
            f"Unset = default {FIXED_PORTS[name]}."
        ),
        json_schema_extra=extra,
    )


class ServiceHealthPortFields:
    """The per-unit daemon /healthz port overrides."""

    labeler_health_port: int | None = health_port_field("labeler")
    im_bridge_health_port: int | None = health_port_field("im_bridge")
    heartbeat_health_port: int | None = health_port_field("heartbeat")
    delivery_watchdog_health_port: int | None = health_port_field("delivery_watchdog")
    task_maintenance_health_port: int | None = health_port_field("task_maintenance")
    events_maintenance_health_port: int | None = health_port_field("events_maintenance")
    pg_backup_health_port: int | None = health_port_field("pg_backup")
    ttl_reaper_health_port: int | None = health_port_field("ttl_reaper")
    schedule_manager_health_port: int | None = health_port_field("schedule_manager")
    memory_indexer_health_port: int | None = health_port_field("memory_indexer")
    # The ops daemon is the agent-runner's inbound port the gateway dials to run
    # cluster ops; the runner registers the resulting URL itself.
    ops_health_port: int | None = health_port_field("ops", capability="agent-runner")
    # `writable`: the one official repair surface for a hosted-runner port that
    # collides on a mirrored localhost namespace (`.env` hand-edits were the only
    # fix during the 2026-09-02 win/wsl 8114 incident). Host scope stays
    # host-writable; remote_writable=False keeps a remote `--machine` set out.
    agent_host_health_port: int | None = health_port_field(
        "agent_host", capability="agent-runner", writable=True
    )
    page_server_health_port: int | None = health_port_field(
        "page_server", capability="agent-runner"
    )
