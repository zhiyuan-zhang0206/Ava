"""Guarded HTTP admission for an explicitly observed launch attempt."""

import asyncio

from fastapi import APIRouter, HTTPException, Request

from base.log import logger
from gateway.http.auth.request_principal import (
    PRINCIPAL_SCOPE,
    SCOPE_HEADER,
    AuthPrincipal,
    PrincipalScopeError,
    request_key,
)
from ops.agents.launch_retry import RetryLaunchConflictError, accept_retry_launch
from ops.cluster import rpc
from ops.rpc_schemas.launch_retry import (
    LaunchReconcileRequest,
    RetryLaunchAccepted,
    RetryLaunchRequest,
)

router = APIRouter()


@router.post("/api/keyed/v1/agents/{agent_id}/retry-launch")
async def retry_launch(
    agent_id: int, body: RetryLaunchRequest, request: Request
) -> RetryLaunchAccepted:
    """Commit one retry intent, then best-effort reconcile its frozen attempt.

    Never downgrade this intent to the original endpoint or v2 launch RPC.
    Every request authenticates again, including a historical receipt replay.
    """
    key = request.headers.get("Idempotency-Key")
    if (
        key is None
        or request.headers.get(SCOPE_HEADER) != PRINCIPAL_SCOPE
        or not isinstance(getattr(request.state, "auth_principal", None), AuthPrincipal)
    ):
        raise HTTPException(
            status_code=400, detail="guarded retry requires a key and verified principal-v1 scope"
        )
    try:
        scoped = request_key(request, key, method="POST", path=request.url.path)
    except PrincipalScopeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        target, accepted = await asyncio.to_thread(
            accept_retry_launch, request.app.state.db_pool, scoped, agent_id, body
        )
    except RetryLaunchConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        await rpc.dispatch_to_machine(
            request.app.state.db,
            target_machine=target,
            kind="launch-reconcile-v1",
            payload=LaunchReconcileRequest(launch_attempt_id=accepted.launch_attempt_id).model_dump(
                mode="json"
            ),
        )
    except (rpc.ClusterOpUnreachable, rpc.ClusterOpFailed) as exc:
        logger.warning(
            "accepted launch retry {} could not reconcile: {}", accepted.launch_attempt_id, exc
        )
    return accepted
