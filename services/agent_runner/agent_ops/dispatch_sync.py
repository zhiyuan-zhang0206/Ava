# meiva:note
# Config and inventory read-modify-write operations share a thread lock so simultaneous workers cannot overwrite one another's fields or secrets. An event-loop lock cannot serialize these worker threads. This lock is process-local; the runtime config owner retains cross-process file locking. Blocking operations use the daemon-owned executor.
from __future__ import annotations

import threading
from typing import Any

from psycopg_pool import ConnectionPool

from base.agents.to_user import ToUserReadRequest
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.host.system.hostport import Host
from base.native_process.loaded_commit import LoadedCommit
from ops import host_config, inventory
from ops.cluster import operations as cluster
from ops.rpc_schemas import (
    AgentSkillViewPayload,
    ConfigAuditReadPayload,
    ConfigWritePayload,
    InventoryWritePayload,
    OpStatus,
    ShellCapturePayload,
    ShellKillPayload,
    ShellProbePayload,
)
from ops.to_user import read_to_user

_state_write_lock = threading.Lock()


def dispatch_sync(
    kind: str,
    payload: dict[str, Any],
    *,
    pool: ConnectionPool | None,
    db: Database,
    authority: ConfigAuthority,
    image: LoadedCommit,
    local_machine: MachineIdentity,
    host: Host,
) -> tuple[OpStatus, dict[str, object]]:

    match kind:
        case "to_user_read":
            request = ToUserReadRequest.model_validate(payload)
            if pool is None:
                raise RuntimeError("To user requires the native DB pool")
            return OpStatus.COMPLETED, read_to_user(
                pool,
                request.agent_id,
                local_machine.name(),
                references=request.references,
            ).model_dump(mode="json")
        case "upload-receive-v1":
            return _upload_arm(payload, pool, local_machine=local_machine.name())
        case "status_probe":
            return OpStatus.COMPLETED, cluster.cluster_status_op(
                db, pool, image=image, local_machine=local_machine
            ).model_dump(mode="json")
        case "config_read":
            return OpStatus.COMPLETED, host_config.config_read_op(
                authority=authority, local_machine=local_machine.name(), host=host
            ).model_dump(mode="json")
        case "config_audit_read":
            ca = ConfigAuditReadPayload.model_validate(payload)
            return OpStatus.COMPLETED, host_config.config_audit_read_op(
                ca.last, local_machine=local_machine.name()
            ).model_dump(mode="json")
        case "config_write":
            cw = ConfigWritePayload.model_validate(payload)
            with _state_write_lock:
                return OpStatus.COMPLETED, host_config.config_write_op(
                    cw.overrides,
                    authority=authority,
                    local=cw.local,
                    actor=cw.actor,
                    trace_id=cw.trace_id,
                    local_machine=local_machine.name(),
                    host=host,
                ).model_dump(mode="json")
        case "inventory_read":
            return OpStatus.COMPLETED, inventory.inventory_read_op(
                local_machine=local_machine, host=host
            ).model_dump(mode="json")
        case "inventory_write":
            iw = InventoryWritePayload.model_validate(payload)
            with _state_write_lock:
                return OpStatus.COMPLETED, inventory.inventory_write_op(
                    iw.plugins, iw.mcp_servers, local_machine=local_machine, host=host
                ).model_dump(mode="json")
        case "shell_probe" | "shell_kill" | "shell_capture" | "agent_skill_view":
            return OpStatus.COMPLETED, _agent_arm(kind, payload, pool)
        case _:
            return OpStatus.FAILED, {"error": f"unknown kind: {kind!r}"}


def _agent_arm(
    kind: str, payload: dict[str, Any], pool: ConnectionPool | None
) -> dict[str, object]:

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


def _upload_arm(
    payload: dict[str, Any], pool: ConnectionPool | None, *, local_machine: str
) -> tuple[OpStatus, dict[str, object]]:

    from httpx2 import HTTPStatusError

    from base.agents.upload_delivery.models import (
        ReceiveRequest,
        UploadDeliveryConflictError,
        UploadQuotaExceededError,
    )
    from ops.upload_delivery import receive

    if pool is None:
        raise RuntimeError("immutable upload receiver requires the native DB pool")
    request = ReceiveRequest.model_validate(payload)
    try:
        return OpStatus.COMPLETED, receive(pool, request, local_machine=local_machine).model_dump(
            mode="json"
        )
    except HTTPStatusError as exc:
        if exc.response.status_code in {401, 403, 404, 409}:
            return OpStatus.FAILED, {
                "error": "source object unavailable",
                "reason": "upload-source-unavailable-v1",
            }
        raise
    except UploadDeliveryConflictError:
        return OpStatus.FAILED, {
            "error": "immutable copy conflict",
            "reason": "upload-copy-conflict-v1",
        }
    except UploadQuotaExceededError:
        return OpStatus.FAILED, {
            "error": "native quota exceeded",
            "reason": "upload-copy-quota-v1",
        }
