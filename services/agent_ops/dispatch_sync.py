"""The blocking op arms: every synchronous `/ops` arm in one module.

Split out of `services/agent_ops/daemon.py` at its file-size ceiling (task
#4129 I4). `daemon._dispatch_sync` is
the thin binding that supplies this daemon's shared DB pool, and
`daemon._run_arm` runs the arms here on the daemon's own worker pool; the
commentary on `dispatch_sync` below -- which moved with the code -- explains
why that separation exists.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from psycopg_pool import ConnectionPool

from ops import (
    ops_bootstrap_hop,
    ops_cluster,
    ops_config,
    ops_inventory,
    ops_prepare_dispatch,
    ops_prepare_facts,
    ops_uploads,
)
from ops.rpc_bootstrap_hop import BootstrapHopPayload, BootstrapRecoveryReadPayload
from ops.rpc_prepare_dispatch import PrepareDispatchPayload
from ops.rpc_prepare_facts import PrepareFactsPayload
from ops.rpc_schemas import (
    AgentSkillViewPayload,
    ClusterSpawnSession,
    ClusterTransitionPayload,
    ClusterUpdatePayload,
    ConfigAuditReadPayload,
    ConfigWritePayload,
    InventoryWritePayload,
    ShellCapturePayload,
    ShellKillPayload,
    ShellProbePayload,
    UploadReceivePayload,
)

# The read-modify-write arms, serialized against each other. Same lost serialization
# as `cluster_update`, different failure: `config_write` and `inventory_write` both
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
    """The blocking half of `daemon._dispatch`, run in a worker thread.

    Moved verbatim out of `services/agent_ops/daemon.py` when that file reached
    its size ceiling (task #4129 I4); the only change is `pool` -- this
    daemon's shared connection pool -- replacing the module-global the arms
    used to read.

    **Every arm here blocks, and none of them may block the event loop.** On
    2026-08-12 a `cluster_update` on the Windows runner stopped returning mid-spawn;
    called inline from the async dispatch, it took the whole daemon with it — 2 h
    03 m with not one line logged, every controller stopped, and the stranded-pause
    self-heal unable to run. Which syscall hung was never identified and does not
    matter: a synchronous op holding the loop is a defect on its own.

    **Nothing stayed behind**: no arm here is purely in-memory, and a stalled
    filesystem is the same defect as a stalled fetch. The two genuinely async ops
    (`spawn`, `lifecycle`) stay in `_dispatch`, awaited as before.

    Exceptions propagate to `daemon._dispatch`'s handlers unchanged — the
    awaiting coroutine re-raises them — so the wire-error mapping is untouched.

    **The half-dead shape this leaves, and how to recognise it.** A worker thread
    that wedges the way 08-12 did no longer takes the daemon down, but it does not
    come back either: it holds the daemon's `_cluster_update_lock` for as long
    as it is stuck, so
    every later `cluster_update` on this host is REFUSED while `cluster_resume`, the
    controllers, the health endpoint and every other op keep working normally. The
    host therefore looks healthy and simply will not update. The tell is a run of
    `refusing a concurrent cluster_update` warnings in this daemon's log with a
    hold time that only grows. The response is to look for the stuck thread and
    bounce the daemon — restarting it releases the lock, and nothing else will.
    """
    match kind:
        case "cluster_stop":
            transition = ClusterTransitionPayload.model_validate(payload)
            return "completed", ops_cluster.cluster_stop_op(
                transition.deploy_holder,
                transition.deploy_acquired_at,
            )
        case "cluster_update":
            # restart_only=True is the agent-runner leg of a cluster *restart*
            # (bounce services on current code, no checkout / uv sync);
            # target_sha is the rollout's pinned commit this host force-checks-out
            # (absent -> catch up to origin/main on a self-heal); mode is the
            # agent-drain policy ('none' on the rollout's Phase B — the
            # gateway-side quiesce already drained the fleet); force_reap is
            # the quiesce-timeout backstop that kills still-live agents.
            cu = ClusterUpdatePayload.model_validate(payload)
            session = ClusterSpawnSession.model_validate(
                ops_cluster.cluster_update_op(
                    restart_only=cu.restart_only,
                    target_sha=cu.target_sha,
                    mode=cu.mode,
                    force_reap=cu.force_reap,
                )
            )
            return "completed", session.model_dump(mode="json")
        case "cluster_fetch":
            return "completed", ops_cluster.cluster_fetch_op()
        case "cluster_prepare_facts":
            # The payload crossed the wire as JSON; JSON mode validates the
            # strict nested evidence model (RolloutIdentity's RFC3339 datetimes).
            pf = PrepareFactsPayload.model_validate_json(json.dumps(payload))
            return "completed", ops_prepare_facts.cluster_prepare_facts_op(pf).model_dump(
                mode="json"
            )
        case "cluster_prepare_dispatch":
            # JSON mode again: the sealed plan crosses as JSON text; the op
            # answers with the unit's acknowledgement (a refusal included).
            pd = PrepareDispatchPayload.model_validate_json(json.dumps(payload))
            return "completed", ops_prepare_dispatch.cluster_prepare_dispatch_op(pd).model_dump(
                mode="json"
            )
        case "cluster_bootstrap_hop":
            # Channel C: the request path crosses as JSON text; the handler
            # re-checks it as canonical private unit state and starts the
            # detached hop session -- the payload is a request, and the child
            # re-verifies every binding from the request bytes itself.
            bh = BootstrapHopPayload.model_validate_json(json.dumps(payload))
            return "completed", ops_bootstrap_hop.cluster_bootstrap_hop_op(bh).model_dump(
                mode="json"
            )
        case "cluster_bootstrap_recovery_read":
            # Channel C's read-only face: the unit reports its own
            # bootstrap-recovery journal slot -- the exact pre-stop abort's
            # per-unit no-effect proof reads exactly this answer. Writes
            # nothing, stops nothing.
            rr = BootstrapRecoveryReadPayload.model_validate(payload)
            return "completed", ops_bootstrap_hop.cluster_bootstrap_recovery_read_op(rr).model_dump(
                mode="json"
            )
        case "cluster_resume":
            transition = ClusterTransitionPayload.model_validate(payload)
            return "completed", ops_cluster.cluster_resume_op(
                transition.deploy_holder,
                transition.deploy_acquired_at,
            )
        case "status_probe":
            return "completed", ops_cluster.cluster_status_op(pool).model_dump(mode="json")
        case "config_read":
            return "completed", ops_config.config_read_op().model_dump(mode="json")
        case "config_audit_read":
            ca = ConfigAuditReadPayload.model_validate(payload)
            return "completed", ops_config.config_audit_read_op(ca.last).model_dump(mode="json")
        case "config_write":
            cw = ConfigWritePayload.model_validate(payload)
            with _state_write_lock:
                return "completed", ops_config.config_write_op(
                    cw.overrides, local=cw.local, actor=cw.actor, trace_id=cw.trace_id
                ).model_dump(mode="json")
        case "inventory_read":
            return "completed", ops_inventory.inventory_read_op().model_dump(mode="json")
        case "inventory_write":
            iw = InventoryWritePayload.model_validate(payload)
            with _state_write_lock:
                return "completed", ops_inventory.inventory_write_op(
                    iw.plugins, iw.mcp_servers
                ).model_dump(mode="json")
        case "shell_probe":
            sp = ShellProbePayload.model_validate(payload)
            return "completed", ops_cluster.shell_probe_op(sp.agent_id).model_dump(mode="json")
        case "shell_kill":
            sk = ShellKillPayload.model_validate(payload)
            return "completed", ops_cluster.shell_kill_op(sk.agent_id, sk.session_id).model_dump(
                mode="json"
            )
        case "agent_skill_view":
            asv = AgentSkillViewPayload.model_validate(payload)
            return "completed", ops_cluster.agent_skill_view_op(asv.agent_id, pool).model_dump(
                mode="json"
            )
        case "shell_capture":
            sc = ShellCapturePayload.model_validate(payload)
            return "completed", ops_cluster.shell_capture_op(
                sc.agent_id, sc.session_id, sc.lines
            ).model_dump(mode="json")
        case "upload_receive":
            ur = UploadReceivePayload.model_validate(payload)
            return "completed", ops_uploads.upload_receive_op(ur).model_dump(mode="json")
        case _:
            return "failed", {"error": f"unknown kind: {kind!r}"}
