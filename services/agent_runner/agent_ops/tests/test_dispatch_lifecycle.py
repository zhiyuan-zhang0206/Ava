"""Lifecycle dispatch keeps the domain result and failure wire contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from psycopg_pool import ConnectionPool

from base.agents import TerminateResult
from base.agents.messages.inbound import WakeTriggerKind
from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from services.agent_runner.agent_ops import daemon
from services.agent_runner.agent_ops.tests.test_daemon import _stub_pool


@pytest.mark.asyncio
async def test_dispatch_lifecycle_calls_lifecycle_op(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """lifecycle kind -> ops.lifecycle_op with parsed path."""
    dispatch_pool: ConnectionPool = _stub_pool()
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
        catalog: ModelCatalog,
    ):
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
            "path": "/api/agents/42/resurrect-if-pending-work-v2",
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
    )
    assert status == "completed"
    # _dispatch serializes the lifecycle response model to a JSON dict for the wire.
    assert result == {"status": "enqueued", "open_tasks": None, "shell_sessions": None}
    assert captured["path"] == "/api/agents/42/resurrect-if-pending-work-v2"
    assert captured["body"] == {"resurrected_by": "system"}
    assert captured["trigger_inbound_id"] == 123
    assert captured["trigger_inbound_kind"] == "chat"


@pytest.mark.asyncio
async def test_dispatch_lifecycle_missing_path_fails(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """lifecycle payload without 'path' returns failed without invoking ops."""
    dispatch_pool: ConnectionPool = _stub_pool()

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
    )
    assert status == "failed"
    # LifecyclePayload validation rejects a missing 'path' before lifecycle_op runs.
    assert "path" in str(result["error"])


@pytest.mark.asyncio
async def test_dispatch_unparseable_lifecycle_path(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """ops.lifecycle_op raising ValueError lands as failed result, not a crash."""
    dispatch_pool: ConnectionPool = _stub_pool()

    async def _raises(
        _db: object,
        _bus: object,
        path: str,
        body: dict[str, Any],
        pool: ConnectionPool | None,
        *,
        trigger_inbound_id: int | None = None,
        trigger_inbound_kind: WakeTriggerKind | None = None,
        catalog: ModelCatalog,
    ):
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
    )
    assert status == "failed"
    assert "not recognized" in str(result["error"])


@pytest.mark.asyncio
async def test_dispatch_resurrect_refusal_fails_with_its_reason(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """A resurrection refusal is a durable verdict, returned in the wire form
    the caller classifies (`ResurrectRefused: <reason>`), not a dispatch crash."""
    from base.agents import ResurrectRefused

    dispatch_pool: ConnectionPool = _stub_pool()

    async def _raises(
        _db: object,
        _bus: object,
        path: str,
        body: dict[str, Any],
        pool: ConnectionPool | None,
        *,
        trigger_inbound_id: int | None = None,
        trigger_inbound_kind: WakeTriggerKind | None = None,
        catalog: ModelCatalog,
    ):
        raise ResurrectRefused("runtime_cutover_required")

    monkeypatch.setattr(daemon.lifecycle, "lifecycle_op", _raises)
    status, result = await daemon._dispatch(
        "lifecycle",
        {"path": "/api/agents/7/resurrect-explicit-v2", "body": {}},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
        executor=op_executor,
        catalog=model_catalog,
        authority=config_authority,
    )
    assert (status, result) == ("failed", {"error": "ResurrectRefused: runtime_cutover_required"})


@pytest.mark.asyncio
async def test_dispatch_wire_error_carries_reason(
    op_executor: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """AvaAgentError raised by an op is converted to a failed result with reason field
    so the gateway's _raise_proxied_wire_error_from_payload can re-emit."""
    dispatch_pool: ConnectionPool = _stub_pool()

    from base.agents import AgentNotFound

    async def _raises(
        _db: object,
        _bus: object,
        path: str,
        body: dict[str, Any],
        pool: ConnectionPool | None,
        *,
        trigger_inbound_id: int | None = None,
        trigger_inbound_kind: WakeTriggerKind | None = None,
        catalog: ModelCatalog,
    ):
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
    )
    assert status == "failed"
    assert "AgentNotFound" in str(result["error"])
    assert result.get("reason") == "agent_not_found"
