"""The daemon /healthz port block of `ServiceSettings`.

Moved out of `shared/config/services.py` when the task #3696 field additions
pushed that module past its 800-line hard ceiling (the same split pattern as
`delivery_watchdog_fields.py`, #2624). Mixed into `ServiceSettings` — NOT a
config domain: `settings.services.*`, every alias/scope/capability face, and
the `.env` contract stay exactly as they were.
"""

from __future__ import annotations

from pydantic import Field

# ── daemon /healthz ports — host scope, one unit at a time ───────────────
#
# `host`, not `cluster-pinned`, because a port block is a property of the
# CLUSTER while the collision domain is one MACHINE's localhost namespace.
# Those coincide until a machine carries two localhost namespaces (WSL2,
# containers, netns) — on 2026-07-26 a WSL2 runner and a native Windows
# runner of the same cluster held the same ports by construction and the
# WSL2 relay republished the Linux daemons on the Windows loopback, so the
# Windows watchdog probed its own port and was answered by the other unit
# (issue #977). Nothing about a health port is cluster-constrained: the
# runner computes its own ops URL from its own `health_port('ops')` and
# registers it (`shared/machines.py`), and the gateway reads that URL back
# off the machines row. So the gateway no longer serves these to runners
# over /api/bootstrap — and a runner's .env never caches a gateway-served
# value at all since the 2026-08-01 config refactor (every runner process
# fetches at startup), so a per-unit port is durable by construction. A
# co-located second unit states its base once with `ava start --health-port-base`;
# `ava start` refuses to launch onto a port another unit already answers on.
# The sibling `*_health_url` / `*_pidfile` fields were already `host`.


class ServiceHealthPortFields:
    """The per-unit daemon /healthz port overrides, in their former order."""

    gateway_watchdog_health_port: int | None = Field(
        default=None,
        alias="AVA_GATEWAY_WATCHDOG_HEALTH_PORT",
        description="Gateway watchdog /healthz port override (per unit). Unset = shared default 8119.",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    agent_runner_watchdog_health_port: int | None = Field(
        default=None,
        alias="AVA_AGENT_RUNNER_WATCHDOG_HEALTH_PORT",
        description="Agent-runner watchdog /healthz port override (per unit). Unset = shared default 8120.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    labeler_health_port: int | None = Field(
        default=None,
        alias="AVA_LABELER_HEALTH_PORT",
        description="Labeler daemon /healthz port override (per unit). Unset = shared default 8103.",
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
        description="IM Bridge daemon /healthz port override (per unit). Unset = shared default 8111.",
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
        description="Heartbeat daemon /healthz port override (per unit). Unset = shared default 8107.",
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
        description="Delivery watchdog /healthz port override (per unit). Unset = shared default 8110.",
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
        description="Delivery watchdog healthcheck URL. Empty = derive via shared.daemon_health.health_port('delivery_watchdog').",
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
        description="Task-maintenance daemon /healthz port override (per unit). Unset = shared default 8108.",
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
        description="Events-maintenance daemon /healthz port override (per unit). Unset = shared default 8109.",
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
        description="Postgres backup scheduler /healthz port override (per unit). Unset = shared default 8116.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )
    pitr_uploader_health_port: int | None = Field(
        default=None,
        alias="AVA_PITR_UPLOADER_HEALTH_PORT",
        description="PITR uploader /healthz port override (per unit). Unset = shared default 8117.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )
    pitr_base_backup_health_port: int | None = Field(
        default=None,
        alias="AVA_PITR_BASE_BACKUP_HEALTH_PORT",
        description="PITR base candidate scheduler /healthz port override.",
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
        description="Memory indexer daemon /healthz port override (per unit). Unset = shared default 8105.",
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
        description="ava-ops daemon /healthz + /ops port override (per unit) — the agent-runner's inbound port the gateway dials to run cluster ops; the runner registers the resulting URL itself. Unset = shared default 8106.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )
