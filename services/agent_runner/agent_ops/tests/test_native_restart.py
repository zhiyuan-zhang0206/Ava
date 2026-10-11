"""Only the versioned restart receipt owns replay after an Ops response is lost."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from base.agents.incarnation.native_restart_models import (
    NativeRestartOperation,
    NativeRestartRequest,
)
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.native_process.loaded_commit import LoadedCommit
from ops.lifecycle.native_restart import restart_native_work_op
from ops.rpc_schemas import OpStatus
from services.agent_runner.agent_host.tests.native_cancel.helpers import managed_work
from services.agent_runner.agent_ops import daemon
from services.agent_runner.agent_ops.tests.test_request_identity import pool as pool


async def test_merged_effort_refusal_has_no_restart_or_config_effects(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    _inc, target = await managed_work(db_conn, aops_pool, database_gate=database_gate)
    db_conn.execute(
        "UPDATE agents_meta SET birth_config=%s::jsonb WHERE id=%s",
        ('{"llm_model":"deepseek-flash","reasoning_effort":"max"}', target.agent_id),
    )
    db_conn.commit()
    operation = NativeRestartOperation(
        operation_key="invalid-effort",
        request=NativeRestartRequest(target=target, config_overlay={"reasoning_effort": "low"}),
    )
    result = await restart_native_work_op(
        database, event_bus, target.agent_id, operation, pool, catalog=model_catalog
    )
    assert result.status == "refused"
    assert result.reason == "invalid_overlay"
    assert "unsupported reasoning effort" in result.detail
    assert db_conn.execute("SELECT count(*) FROM native_restart_commands").fetchone() == (0,)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='restart'",
        (target.agent_id,),
    ).fetchone() == (0,)
    assert db_conn.execute(
        "SELECT config_overlay FROM agents_meta WHERE id=%s", (target.agent_id,)
    ).fetchone() == (None,)


async def test_domain_receipt_recovers_without_generic_claim_or_second_command(
    op_executor: ThreadPoolExecutor,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    database_gate: ProcessDbGate,
) -> None:
    _inc, target = await managed_work(db_conn, aops_pool, database_gate=database_gate)
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
            "lifecycle",
            packet,
            "domain-ops",
            pool,
            active_ops={},
            workers=set(),
            executor=op_executor,
            catalog=model_catalog,
            authority=config_authority,
            database=ops_database,
            image=ops_image,
        )
    original = result
    assert original is not None
    assert db_conn.execute(
        "SELECT count(*) FROM api_idempotency WHERE key='domain-ops'"
    ).fetchone() == (0,)
    status, recovered = await daemon._dispatch_idempotent_pass(
        "lifecycle",
        packet,
        "domain-ops",
        pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
    )
    assert status is OpStatus.COMPLETED and recovered == original
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='restart'",
        (target.agent_id,),
    ).fetchone() == (1,)
    bad = packet | {"body": operation.model_dump(mode="json") | {"operation_key": "another"}}
    status, result = await daemon._dispatch_idempotent_pass(
        "lifecycle",
        bad,
        "domain-ops",
        pool,
        active_ops={},
        workers=set(),
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
    )
    assert status is OpStatus.FAILED
    assert result == {"error": "guarded restart envelope identity differs"}
