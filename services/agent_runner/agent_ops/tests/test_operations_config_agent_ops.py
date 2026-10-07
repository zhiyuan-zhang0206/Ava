"""The agent-ops daemon dispatches config read and write ops."""

from __future__ import annotations

from typing import Any

import pytest
from psycopg_pool import ConnectionPool

from ops.rpc_schemas import ConfigReadResult, ConfigWriteOpResult


@pytest.mark.asyncio
async def test_dispatch_config_read_calls_config_read_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config_read kind -> ops.config_read_op, returns completed with its result."""
    from services.agent_runner.agent_ops import daemon

    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    captured: list[bool] = []

    def _fake_config_read() -> ConfigReadResult:
        captured.append(True)
        return ConfigReadResult(machine="x", host_fields={}, raw_overrides={})

    monkeypatch.setattr(daemon.host_config, "config_read_op", _fake_config_read)
    status, result = await daemon._dispatch(
        "config_read", {}, active_ops={}, workers=set(), pool=dispatch_pool
    )
    assert status == "completed"
    # _dispatch serializes the result model to a JSON dict for the wire.
    assert result["machine"] == "x"
    assert captured == [True]


@pytest.mark.asyncio
async def test_dispatch_config_write_passes_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config_write kind -> ops.config_write_op(payload['overrides'] + local/actor/trace_id),
    fail-fast on missing key."""
    from services.agent_runner.agent_ops import daemon

    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    captured: dict[str, Any] = {}

    def _fake_config_write(
        overrides: dict[str, Any],
        *,
        local: bool = False,
        actor: str | None = None,
        trace_id: str | None = None,
    ) -> ConfigWriteOpResult:
        captured["overrides"] = overrides
        captured["local"] = local
        captured["actor"] = actor
        captured["trace_id"] = trace_id
        return ConfigWriteOpResult(machine="x", results={}, applied=True, restart_required=[])

    monkeypatch.setattr(daemon.host_config, "config_write_op", _fake_config_write)
    status, _result = await daemon._dispatch(
        "config_write",
        {"overrides": {"ops_concurrency": 2}},
        active_ops={},
        workers=set(),
        pool=dispatch_pool,
    )
    assert status == "completed"
    assert captured["overrides"] == {"ops_concurrency": 2}
    assert captured["actor"] is None  # payload carried no gateway-stamped identity
    assert captured["trace_id"] is None


@pytest.mark.asyncio
async def test_dispatch_config_write_missing_overrides_key_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """payload missing 'overrides' key -> failed result (ConfigWritePayload
    validation rejects it, fail-fast, no silent fallback)."""
    from services.agent_runner.agent_ops import daemon

    dispatch_pool: ConnectionPool = ConnectionPool(open=False)
    # The config_write arm validates payload into ConfigWritePayload, whose
    # `overrides` is required; a missing key is a caught ValidationError surfaced
    # as a 'failed' op result (the /ops route returns HTTP 200 + status=failed).
    status, result = await daemon._dispatch(
        "config_write", {}, active_ops={}, workers=set(), pool=dispatch_pool
    )  # no 'overrides' key
    assert status == "failed"
    assert "overrides" in str(result["error"])
