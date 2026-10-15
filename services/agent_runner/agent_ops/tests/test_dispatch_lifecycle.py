from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from psycopg_pool import ConnectionPool

from base.agents.messages.inbound import WakeTriggerKind
from base.api_contracts.agent_contract import TerminateResult
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.host.system.tests.fake_commands import fake_host
from base.lm.catalog import ModelCatalog
from base.native_process.loaded_commit import LoadedCommit
from services.agent_runner.agent_ops import daemon
from services.agent_runner.agent_ops.tests.test_daemon import stub_pool


@pytest.mark.asyncio
async def test_dispatch_lifecycle_calls_lifecycle_op(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    dispatch_pool: ConnectionPool = stub_pool()
    captured: dict[str, object] = {}

    async def _fake_lifecycle(
        _db: object,
        _bus: object,
        path: str,
        body: dict[str, Any],
        pool: ConnectionPool | None,
        *,
        trigger_inbound_id: int | None = None,
        trigger_inbound_kind: WakeTriggerKind | None = None,
        local_machine: str,
        catalog: ModelCatalog,
    ):
        assert local_machine == machine_identity.name()
        from ops.rpc_schemas import TerminateAgentResponse

        captured["path"] = path
        captured["body"] = body
        captured["trigger_inbound_id"] = trigger_inbound_id
        captured["trigger_inbound_kind"] = trigger_inbound_kind
        return TerminateAgentResponse(status=TerminateResult.ENQUEUED)

    monkeypatch.setattr(daemon.lifecycle, "lifecycle_op", _fake_lifecycle)
    status, result = await daemon._dispatch(
        "lifecycle",
        {
            "path": "/api/agents/42/resurrect-if-pending-work",
            "body": {"resurrected_by": "system"},
            "trigger_inbound_id": 123,
            "trigger_inbound_kind": "chat",
        },
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
        host=fake_host(),
    )
    assert status == "completed"
    assert result == {"status": "enqueued", "open_tasks": None, "shell_sessions": None}
    assert captured["path"] == "/api/agents/42/resurrect-if-pending-work"
    assert captured["body"] == {"resurrected_by": "system"}
    assert captured["trigger_inbound_id"] == 123
    assert captured["trigger_inbound_kind"] == "chat"


@pytest.mark.asyncio
async def test_dispatch_lifecycle_missing_path_fails(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    dispatch_pool: ConnectionPool = stub_pool()

    async def _should_not_be_called(_db: object, _bus: object, *_a: object, **_kw: object):
        raise AssertionError("lifecycle_op should not be invoked on missing path")

    monkeypatch.setattr(daemon.lifecycle, "lifecycle_op", _should_not_be_called)
    status, result = await daemon._dispatch(
        "lifecycle",
        {},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
        host=fake_host(),
    )
    assert status == "failed"
    assert "path" in str(result["error"])


@pytest.mark.asyncio
async def test_dispatch_unparseable_lifecycle_path(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    dispatch_pool: ConnectionPool = stub_pool()

    async def _raises(
        _db: object,
        _bus: object,
        path: str,
        body: dict[str, Any],
        pool: ConnectionPool | None,
        *,
        trigger_inbound_id: int | None = None,
        trigger_inbound_kind: WakeTriggerKind | None = None,
        local_machine: str,
        catalog: ModelCatalog,
    ):
        assert local_machine == machine_identity.name()
        raise ValueError(f"lifecycle path not recognized: {path!r}")

    monkeypatch.setattr(daemon.lifecycle, "lifecycle_op", _raises)
    status, result = await daemon._dispatch(
        "lifecycle",
        {"path": "/garbage", "body": {}},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
        host=fake_host(),
    )
    assert status == "failed"
    assert "not recognized" in str(result["error"])


@pytest.mark.asyncio
async def test_dispatch_resurrect_refusal_fails_with_its_reason(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    from base.api_contracts.agent_contract import ResurrectRefused

    dispatch_pool: ConnectionPool = stub_pool()

    async def _raises(
        _db: object,
        _bus: object,
        path: str,
        body: dict[str, Any],
        pool: ConnectionPool | None,
        *,
        trigger_inbound_id: int | None = None,
        trigger_inbound_kind: WakeTriggerKind | None = None,
        local_machine: str,
        catalog: ModelCatalog,
    ):
        assert local_machine == machine_identity.name()
        raise ResurrectRefused("runtime_cutover_required")

    monkeypatch.setattr(daemon.lifecycle, "lifecycle_op", _raises)
    status, result = await daemon._dispatch(
        "lifecycle",
        {"path": "/api/agents/7/resurrect-explicit", "body": {}},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
        host=fake_host(),
    )
    assert (status, result) == ("failed", {"error": "ResurrectRefused: runtime_cutover_required"})


@pytest.mark.asyncio
async def test_dispatch_wire_error_carries_reason(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    ops_database: Callable[[], Database],
    ops_image: LoadedCommit,
    *,
    machine_identity: MachineIdentity,
) -> None:
    dispatch_pool: ConnectionPool = stub_pool()

    from base.api_contracts.agent_contract import AgentNotFound

    async def _raises(
        _db: object,
        _bus: object,
        path: str,
        body: dict[str, Any],
        pool: ConnectionPool | None,
        *,
        trigger_inbound_id: int | None = None,
        trigger_inbound_kind: WakeTriggerKind | None = None,
        local_machine: str,
        catalog: ModelCatalog,
    ):
        assert local_machine == machine_identity.name()
        raise AgentNotFound("agent 999 does not exist")

    monkeypatch.setattr(daemon.lifecycle, "lifecycle_op", _raises)
    status, result = await daemon._dispatch(
        "lifecycle",
        {"path": "/api/agents/999/terminate", "body": {}},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
        database=ops_database,
        image=ops_image,
        local_machine=machine_identity,
        host=fake_host(),
    )
    assert status == "failed"
    assert "AgentNotFound" in str(result["error"])
    assert result.get("reason") == "agent_not_found"
