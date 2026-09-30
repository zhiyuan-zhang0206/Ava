"""The blocking op arms: every synchronous `/ops` arm in one module.

Split out of `services/agent_ops/daemon.py` at its file-size ceiling (task
#4129 I4). `daemon._dispatch_sync` is
the thin binding that supplies this daemon's shared DB pool, and
`daemon._run_arm` runs the arms here on the daemon's own worker pool; the
commentary on `dispatch_sync` below -- which moved with the code -- explains
why that separation exists.
"""

from __future__ import annotations

import threading
from typing import Any

from psycopg_pool import ConnectionPool

from ops import cluster, host_config, inventory, uploads
from ops.rpc_schemas import (
    AgentSkillViewPayload,
    ConfigAuditReadPayload,
    ConfigWritePayload,
    InventoryWritePayload,
    ShellCapturePayload,
    ShellKillPayload,
    ShellProbePayload,
    UploadReceivePayload,
)

# The read-modify-write arms are serialized against each other: `config_write` and `inventory_write` both
# READ their on-disk state, modify it and write it back, so an interleave lands the
# later writer's snapshot over the earlier one's fields with no error anywhere — in
# the only on-disk copy of a cluster's secrets.
#
# Blocking, not refused: millisecond writes with no session to collide over, so
# waiting one out is cheap where refusing would make a caller re-send a config edit
# for nothing. `threading.Lock`, not `asyncio.Lock` — the contention is between
# worker THREADS, which an asyncio lock cannot see, and taking one on the loop would
# put the serialization back where this change removed it.
#
# Same-process only; the cross-process race is closed by `runtime_config`'s own lock.
_state_write_lock = threading.Lock()


def dispatch_sync(
    kind: str, payload: dict[str, Any], *, pool: ConnectionPool | None
) -> tuple[str, dict[str, object]]:
    """Run synchronous ops on the daemon's worker pool, never the event loop.

    Configuration and inventory writes share a lock because both read and
    replace local state. A blocked filesystem operation must leave health,
    maintenance resume and unrelated ops reachable.
    """
    match kind:
        case "status_probe":
            return "completed", cluster.cluster_status_op(pool).model_dump(mode="json")
        case "config_read":
            return "completed", host_config.config_read_op().model_dump(mode="json")
        case "config_audit_read":
            ca = ConfigAuditReadPayload.model_validate(payload)
            return "completed", host_config.config_audit_read_op(ca.last).model_dump(mode="json")
        case "config_write":
            cw = ConfigWritePayload.model_validate(payload)
            with _state_write_lock:
                return "completed", host_config.config_write_op(
                    cw.overrides, local=cw.local, actor=cw.actor, trace_id=cw.trace_id
                ).model_dump(mode="json")
        case "inventory_read":
            return "completed", inventory.inventory_read_op().model_dump(mode="json")
        case "inventory_write":
            iw = InventoryWritePayload.model_validate(payload)
            with _state_write_lock:
                return "completed", inventory.inventory_write_op(
                    iw.plugins, iw.mcp_servers
                ).model_dump(mode="json")
        case "shell_probe" | "shell_kill" | "shell_capture" | "agent_skill_view":
            return "completed", _agent_arm(kind, payload, pool)
        case "upload_receive":
            ur = UploadReceivePayload.model_validate(payload)
            return "completed", uploads.upload_receive_op(ur).model_dump(mode="json")
        case _:
            return "failed", {"error": f"unknown kind: {kind!r}"}


def _agent_arm(
    kind: str, payload: dict[str, Any], pool: ConnectionPool | None
) -> dict[str, object]:
    """The per-agent read and shell arms (one agent's shells and command view)."""
    match kind:
        case "shell_probe":
            sp = ShellProbePayload.model_validate(payload)
            return cluster.shell_probe_op(sp.agent_id).model_dump(mode="json")
        case "shell_kill":
            sk = ShellKillPayload.model_validate(payload)
            return cluster.shell_kill_op(sk.agent_id, sk.session_id).model_dump(mode="json")
        case "agent_skill_view":
            asv = AgentSkillViewPayload.model_validate(payload)
            return cluster.agent_skill_view_op(asv.agent_id, pool).model_dump(mode="json")
        case "shell_capture":
            sc = ShellCapturePayload.model_validate(payload)
            return cluster.shell_capture_op(sc.agent_id, sc.session_id, sc.lines).model_dump(
                mode="json"
            )
        case _:
            raise ValueError(f"not an agent arm: {kind!r}")
