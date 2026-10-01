"""The daemon /healthz port fields of `ServiceSettings`.

Moved out of `base/config/services.py` when the task #3696 field additions
pushed that module past its 800-line hard ceiling (the same split pattern as
`delivery_watchdog_fields.py`, #2624). Mixed into `ServiceSettings` — NOT a
config domain: `settings.services.*`, every alias/scope/capability face, and
the `.env` contract stay exactly as they were.
"""

from __future__ import annotations

from pydantic import Field

# ── daemon /healthz ports — host scope ───────────────────────────────────
#
# `host`, not `cluster-pinned`: a health port is a fact of the machine's
# localhost namespace, and the default is the fixed port table
# (`base.host.env.port_table`). The runner computes its own ops URL from its own
# `health_port('ops')` and registers it (`base/cluster/machines.py`), and the
# gateway reads that URL back off the machines row. So the gateway does not serve
# these to runners over /api/bootstrap, and a runner's .env never caches a
# gateway-served value. `ava start` refuses to launch onto a port another home's
# daemon already answers on. The sibling `*_health_url` / `*_pidfile` fields were
# already `host`.


class ServiceHealthPortFields:
    """The per-unit daemon /healthz port overrides, in their former order."""

    labeler_health_port: int | None = Field(
        default=None,
        alias="AVA_LABELER_HEALTH_PORT",
        description="Labeler daemon /healthz port override (per unit). Unset = default 8103.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    im_bridge_health_port: int | None = Field(
        default=None,
        alias="AVA_IM_BRIDGE_HEALTH_PORT",
        description="IM Bridge daemon /healthz port override (per unit). Unset = default 8111.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    heartbeat_health_port: int | None = Field(
        default=None,
        alias="AVA_HEARTBEAT_HEALTH_PORT",
        description="Heartbeat daemon /healthz port override (per unit). Unset = default 8107.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    delivery_watchdog_health_port: int | None = Field(
        default=None,
        alias="AVA_DELIVERY_WATCHDOG_HEALTH_PORT",
        description="Delivery watchdog /healthz port override (per unit). Unset = default 8110.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    delivery_watchdog_health_url: str = Field(
        default="",
        alias="AVA_DELIVERY_WATCHDOG_HEALTH_URL",
        description="Delivery watchdog healthcheck URL. Empty = derive via base.daemon.health.health_port('delivery_watchdog').",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    task_maintenance_health_port: int | None = Field(
        default=None,
        alias="AVA_TASK_MAINTENANCE_HEALTH_PORT",
        description="Task-maintenance daemon /healthz port override (per unit). Unset = default 8108.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    events_maintenance_health_port: int | None = Field(
        default=None,
        alias="AVA_EVENTS_MAINTENANCE_HEALTH_PORT",
        description="Events-maintenance daemon /healthz port override (per unit). Unset = default 8109.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    pg_backup_health_port: int | None = Field(
        default=None,
        alias="AVA_PG_BACKUP_HEALTH_PORT",
        description="Postgres backup scheduler /healthz port override (per unit). Unset = default 8116.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    memory_indexer_health_port: int | None = Field(
        default=None,
        alias="AVA_MEMORY_INDEXER_HEALTH_PORT",
        description="Memory indexer daemon /healthz port override (per unit). Unset = default 8105.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    ops_health_port: int | None = Field(
        default=None,
        alias="AVA_OPS_HEALTH_PORT",
        description="ava-ops daemon /healthz + /ops port override (per unit) — the agent-runner's inbound port the gateway dials to run cluster ops; the runner registers the resulting URL itself. Unset = default 8113.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )
