"""Cross-machine forward helpers for the /api/agents/* router surface.

Lifecycle operations (resurrect / force-terminate / restart) and spawn must
run on the agent's home machine via that machine's ops server — the gateway is
a pure router and never shortcuts a co-located runner. These helpers are
endpoint-private routing logic, shared by routers/agents.py (spawn),
routers/agents_lifecycle.py (lifecycle ops) and the op-forwarding routers
(guide / packages / schedules), so they live in their own module — the ops
server never sees them (forwarding never recurses inside an op, see
ops/ops_lifecycle.py), and tests get one stable patch point
(monkeypatch `_enqueue_lifecycle` / `_forward_spawn_to_remote` here). Same
intent, just relocated from the old app.py to here.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger
from pydantic import ValidationError

from ops import cluster_rpc as _cluster_rpc
from ops.rpc_schemas import LaunchAgentRequest, OpFailure, SpawnedAgent
from shared.agents import (
    EXCEPTION_BY_REASON,
    AgentNotFound,
    CrossMachineGatewayUnavailable,
    ErrorReason,
)

# Browser clients and the agent UI give lifecycle requests a short response
# window. The generic cluster-RPC policy is intentionally more patient for
# rollout work, but an offline runner must become a clear 502 before this
# caller-facing request expires. The one retry retains the server-side
# idempotency envelope for a transient connection drop without risking a long
# silent wait.
_LIFECYCLE_DISPATCH_DEADLINE_S = 12.0
_LIFECYCLE_DISPATCH_TIMEOUT_S = 5.0
_LIFECYCLE_DISPATCH_RETRIES = 1


def _raise_proxied_wire_error_from_payload(payload: dict[str, object]) -> None:
    """Reconstruct + raise the target-side AvaAgentError from a failed
    `/ops` result payload.

    The ops server's `_dispatch` returns an OpFailure-shaped result on failure
    (`{error, detail, reason}`). If a recognized wire `reason` is present,
    reconstruct the matching AvaAgentError subclass so the original semantics
    survive the cross-machine boundary unchanged. Otherwise fall back to a
    generic `CrossMachineGatewayUnavailable` carrying the op's error string.
    """
    try:
        failure = OpFailure.model_validate(payload)
    except ValidationError:
        # A payload that is not even OpFailure-shaped (no `error`) -> generic.
        raise CrossMachineGatewayUnavailable(
            f"target machine reported failure via queue: {payload!r}"
        ) from None
    if failure.reason is not None and failure.detail is not None:
        try:
            reason = ErrorReason(failure.reason)
        except ValueError:
            logger.warning("Unknown error reason '{}' in cross-machine payload", failure.reason)
        else:
            raise EXCEPTION_BY_REASON[reason](failure.detail)
    raise CrossMachineGatewayUnavailable(f"target machine reported failure via queue: {payload!r}")


async def _forward_to_home_machine(agent_id: int, path: str, json_body: dict) -> dict:
    """Lifecycle operations (resurrect / force-terminate / etc.) must run on
    the agent's home machine (`agents_meta.machine`) — they mutate physical
    host state (session / OS process); not doing them on the home
    machine is a noop / starts on the wrong host.

    Always POSTs a 'lifecycle' op to the target machine's ops server and
    blocks on the response, then returns the result dict (caller reshapes to
    the business model). No local shortcut — even when target IS the local
    machine, the ops server on localhost executes the op in-process. Single
    box is just the special case where the ops server is reachable at
    127.0.0.1.

    The target machine's ava-ops server executes the op in-process via
    `ops.ops_lifecycle.lifecycle_op` — no cross-machine forwarding inside the
    op, no risk of a forwarding loop.

    Raises:
        AgentNotFound: agent_id does not exist (no agents_meta row).
        CrossMachineGatewayUnavailable: the target machine's ops server was
            unreachable (offline / not yet registered / timed out).
        Other AvaAgentError subclasses: business errors raised by the
            target machine's op are passed through.
    """
    # Lazy import: routers/agents -> gateway.app -> routers cycle. Also lets
    # tests monkeypatch the helper with the same signature as before the
    # router split.
    from gateway.app import app

    target = await asyncio.to_thread(_home_machine_blocking, app, agent_id)
    return await _enqueue_lifecycle(target, path, json_body)


async def _enqueue_lifecycle(target: str, path: str, json_body: dict) -> dict:
    """POST a 'lifecycle' op to the target machine's ops server, return its result.

    Translates a 'failed' outcome (target machine's op raised an AvaAgentError)
    into the wire-protocol exception so the gateway's handler re-emits the
    original semantics.
    """
    try:
        async with asyncio.timeout(_LIFECYCLE_DISPATCH_DEADLINE_S):
            return await _cluster_rpc.dispatch_to_machine(
                target_machine=target,
                kind="lifecycle",
                payload={"path": path, "body": json_body},
                timeout_s=_LIFECYCLE_DISPATCH_TIMEOUT_S,
                retries=_LIFECYCLE_DISPATCH_RETRIES,
            )
    except TimeoutError as exc:
        raise CrossMachineGatewayUnavailable(
            f"target machine={target!r} lifecycle op did not answer within "
            f"{_LIFECYCLE_DISPATCH_DEADLINE_S:.0f}s"
        ) from exc
    except _cluster_rpc.ClusterOpUnreachable as exc:
        raise CrossMachineGatewayUnavailable(
            f"target machine={target!r} ops server unreachable for lifecycle op: {exc!s}"
        ) from exc
    except _cluster_rpc.ClusterOpFailed as exc:
        _raise_proxied_wire_error_from_payload(exc.result)
        raise  # unreachable — _raise_proxied_wire_error_from_payload must raise


async def _forward_spawn_to_remote(target: str, body: LaunchAgentRequest) -> SpawnedAgent:
    """POST a 'spawn-launch' op to the target machine's ops server, return the result.

    The target machine's ava-ops server dispatches in-process to
    `ops.ops_lifecycle.launch_agent_op` (Task #1236 follow-up: the gateway
    already created the agent row as the main identity — the runner's op only
    launches the process) and returns the SpawnedAgent dict.

    Raises:
        CrossMachineGatewayUnavailable: the target machine's ops server was
            unreachable (offline / not yet registered / timed out).
        Other AvaAgentError subclasses: business errors raised by the
            target machine's op are passed through.
    """
    forward_body = body.model_dump(exclude_none=True)
    try:
        result = await _cluster_rpc.dispatch_to_machine(
            target_machine=target,
            kind="spawn-launch",
            payload=forward_body,
        )
    except _cluster_rpc.ClusterOpUnreachable as exc:
        raise CrossMachineGatewayUnavailable(
            f"target machine={target!r} ops server unreachable for spawn op: {exc!s}"
        ) from exc
    except _cluster_rpc.ClusterOpFailed as exc:
        _raise_proxied_wire_error_from_payload(exc.result)
        raise  # unreachable
    return SpawnedAgent.model_validate(result)


def _home_machine_blocking(app: Any, agent_id: int) -> str:
    """Sync home-machine lookup for lifecycle ops — via to_thread."""
    with app.state.db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT machine FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    if row is None:
        raise AgentNotFound(f"agent {agent_id} does not exist")
    return row[0]
