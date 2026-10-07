"""A versioned repeatable wake never falls through to old launch effects."""

from uuid import UUID, uuid4

import pytest
from psycopg_pool import ConnectionPool

from ops.cluster import rpc
from ops.rpc_schemas import OpStatus
from ops.rpc_schemas.launch_retry import LaunchReconciled, LaunchReconcileRequest
from services.agent_runner.agent_ops import daemon


@pytest.mark.asyncio
async def test_reconcile_dispatch_and_old_consumer_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    from ops.lifecycle import launch_reconcile

    pool: ConnectionPool = ConnectionPool(open=False)
    calls: list[tuple[UUID, object]] = []
    attempt = uuid4()

    async def reconcile(
        _db: object, _bus: object, body: LaunchReconcileRequest, received_pool: object
    ) -> LaunchReconciled:
        calls.append((body.launch_attempt_id, received_pool))
        return LaunchReconciled(wake_published=True)

    async def forbidden(*_args: object) -> None:
        raise AssertionError("versioned reconciliation cannot invoke legacy launch")

    monkeypatch.setattr(launch_reconcile, "reconcile_launch_op", reconcile)
    monkeypatch.setattr(daemon.lifecycle, "launch_agent_op", forbidden)
    status, result = await daemon._dispatch(
        "launch-reconcile-v1",
        {"launch_attempt_id": str(attempt)},
        active_ops={},
        workers=set(),
        pool=pool,
    )
    assert status == OpStatus.COMPLETED
    assert result == {"wake_published": True}
    assert calls == [(attempt, pool)]
    # Older runners run this exact dispatcher guard with their old vocabulary.
    # Model the removed new member, rather than handing a fake HTTP response to
    # the caller: actual dispatch refuses before touching the pool or any launch arm.
    original = daemon.is_op_kind

    def old_kind(value: str) -> bool:
        return value != "launch-reconcile-v1" and original(value)

    monkeypatch.setattr(daemon, "is_op_kind", old_kind)
    status, result = await daemon._dispatch(
        "launch-reconcile-v1",
        {"launch_attempt_id": str(attempt)},
        active_ops={},
        workers=set(),
        pool=pool,
    )
    assert status == OpStatus.FAILED
    assert isinstance(result["error"], str)
    assert "unknown kind" in result["error"]
    assert len(calls) == 1
    assert "launch-reconcile-v1" not in rpc._NON_IDEMPOTENT_KINDS
    assert {"spawn-launch", "spawn-launch-v2", "lifecycle"} <= rpc._NON_IDEMPOTENT_KINDS
