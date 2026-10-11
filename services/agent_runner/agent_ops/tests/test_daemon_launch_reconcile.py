"""A versioned repeatable wake never falls through to old launch effects."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from psycopg_pool import ConnectionPool

from base.config.service_read import ConfigAuthority
from base.db import Database
from base.lm.catalog import ModelCatalog
from base.native_process.loaded_commit import LoadedCommit
from ops.cluster import rpc
from ops.rpc_schemas import OpStatus
from ops.rpc_schemas.launch_retry import LaunchReconciled, LaunchReconcileRequest
from services.agent_runner.agent_ops import daemon


@pytest.mark.asyncio
async def test_reconcile_dispatch_uses_its_own_repeatable_handler(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
) -> None:
    from ops.lifecycle import launch_reconcile

    pool: ConnectionPool = ConnectionPool(open=False)
    calls: list[tuple[UUID, object]] = []
    attempt = uuid4()

    async def reconcile(
        _db: object, _bus: object, body: LaunchReconcileRequest, received_pool: object
    ) -> LaunchReconciled:
        calls.append((body.launch_attempt_id, received_pool))
        return LaunchReconciled(wake_published=True)

    async def forbidden(*_args: object, catalog: ModelCatalog) -> None:
        raise AssertionError("reconciliation cannot invoke the creation launch handler")

    monkeypatch.setattr(launch_reconcile, "reconcile_launch_op", reconcile)
    monkeypatch.setattr(daemon.lifecycle, "launch_agent_op", forbidden)
    status, result = await daemon._dispatch(
        "launch-reconcile-v1",
        {"launch_attempt_id": str(attempt)},
        active_ops={},
        workers=set(),
        pool=pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
    )
    assert status == OpStatus.COMPLETED
    assert result == {"wake_published": True}
    assert calls == [(attempt, pool)]
    assert "launch-reconcile-v1" not in rpc._NON_IDEMPOTENT_KINDS
    assert {"spawn-launch-v2", "lifecycle"} <= rpc._NON_IDEMPOTENT_KINDS
