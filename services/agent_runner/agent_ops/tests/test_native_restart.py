"""Only the versioned restart receipt owns replay after an Ops response is lost."""

from typing import Any

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from base.agents.incarnation.native_restart_models import (
    NativeRestartOperation,
    NativeRestartRequest,
)
from ops.rpc_schemas import OpStatus
from services.agent_runner.agent_host.tests.native_cancel.helpers import managed_work
from services.agent_runner.agent_ops import daemon
from services.agent_runner.agent_ops.tests.test_request_identity import pool as pool


async def test_domain_receipt_recovers_without_generic_claim_or_second_command(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _inc, target = await managed_work(db_conn, aops_pool)
    request = NativeRestartRequest(target=target)
    operation = NativeRestartOperation(operation_key="domain-ops", request=request)
    packet = {
        "path": f"/api/agents/{target.agent_id}/restart-work-v1",
        "body": operation.model_dump(mode="json"),
    }
    actual = daemon._dispatch
    result: dict[str, object] | None = None
    injected = False

    async def lose_response(*args: Any, **kwargs: Any) -> Any:
        nonlocal result, injected
        status, result = await actual(*args, **kwargs)
        assert status is OpStatus.COMPLETED
        if not injected:
            injected = True
            raise psycopg.OperationalError("test Ops accepted response lost")
        return status, result

    monkeypatch.setattr(daemon, "_dispatch", lose_response)
    with pytest.raises(psycopg.OperationalError):
        await daemon._dispatch_idempotent_pass(
            "lifecycle", packet, "domain-ops", pool, active_ops={}, workers=set()
        )
    original = result
    assert original is not None
    assert db_conn.execute(
        "SELECT count(*) FROM api_idempotency WHERE key='domain-ops'"
    ).fetchone() == (0,)
    status, recovered = await daemon._dispatch_idempotent_pass(
        "lifecycle", packet, "domain-ops", pool, active_ops={}, workers=set()
    )
    assert status is OpStatus.COMPLETED and recovered == original
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='restart'",
        (target.agent_id,),
    ).fetchone() == (1,)
    bad = packet | {"body": operation.model_dump(mode="json") | {"operation_key": "another"}}
    status, result = await daemon._dispatch_idempotent_pass(
        "lifecycle", bad, "domain-ops", pool, active_ops={}, workers=set()
    )
    assert status is OpStatus.FAILED
    assert result == {"error": "guarded restart envelope identity differs"}
