# pyright: reportUnknownArgumentType=warning, reportUnknownLambdaType=warning
"""The `ops` op clusters — ops-server-callable RPC implementations.

Free functions backing both the gateway FastAPI handlers and the in-process dispatch in
services/agent_ops/daemon.py. These tests pin the contract independently of either entry point:
dispatch routing in the ops server has its own coverage in services/agent_ops/tests/test_daemon.py,
endpoint smoke tests live in tests/gateway/test_cluster_endpoints.py.
"""

from __future__ import annotations

import pytest

from base.db import Database
from base.deploy.maintenance.tests.test_admission import isolate as isolate
from base.events.live.bus import EventBus
from ops import lifecycle
from ops.lifecycle import launch
from ops.rpc_schemas import (
    LaunchAgentRequest,
    TerminateAgentRequest,
)


def _db() -> Database:
    return Database.from_settings()


@pytest.fixture
def stub_pool() -> object:
    """Sentinel pool — every op call below mocks the gateway/agents helpers so
    the pool is never touched, but the signature still requires an object."""
    return object()


@pytest.mark.asyncio
async def test_launch_agent_op_hosted_fork_still_wakes(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object, database: Database, event_bus: EventBus
) -> None:
    """A fork's inbounds were pre-inserted by create_agent_row as raw SQL (no
    wake inside) — the hosted launch must publish the wake explicitly, and must
    not insert a second prompt."""
    inserted: list[int] = []

    def _fake_insert(_db, _bus, _pool: object, _agent_id: int, _prompt: str, _source: str) -> int:
        inserted.append(1)
        return 0

    monkeypatch.setattr(launch, "_insert_prompt_blocking", _fake_insert)

    async def _fake_publish(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(lifecycle, "publish_inbound_arrived", _fake_publish)
    wakes: list[tuple[int, str]] = []
    monkeypatch.setattr(
        launch, "publish_inbound_wake", lambda _db, _bus, aid, payload: wakes.append((aid, payload))
    )

    body = LaunchAgentRequest(agent_id=8)
    result = await lifecycle.launch_agent_op(database, event_bus, body, stub_pool)  # type: ignore[arg-type]
    assert result.id == 8
    assert inserted == []  # fork prompt is delivered pre-launch, never here
    assert wakes == [(8, "0")]


async def test_force_terminate_hosted_skips_process_kill_and_cancels_turn(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object, database: Database, event_bus: EventBus
) -> None:
    """Hosted force-terminate: no process to SIGKILL — the DB fence runs with
    kill_process=False and the turn-cancel acceleration fires after the
    transaction. The durable terminate inbound inserted by the fence is the
    captured: dict[str, object] = {}
    correctness mechanism; the cancel only accelerates a wedged turn."""
    from base.agents import AgentStatus

    captured: dict[str, object] = {}

    def _fake_force_blocking(
        _db: object,
        _bus: object,
        aid: int,
        _body: object,
        _pool: object,
        _recovery_wake: str | None,
    ) -> tuple[AgentStatus, int | None, list[str], int]:
        captured["agent_id"] = aid
        return AgentStatus.RUNNING, None, [], 91

    monkeypatch.setattr(lifecycle, "_terminate_force_blocking", _fake_force_blocking)
    cancelled: list[tuple[int, int]] = []

    async def _fake_cancel(aid: int, command_id: int) -> None:
        cancelled.append((aid, command_id))

    monkeypatch.setattr(lifecycle, "_cancel_hosted_turn_best_effort", _fake_cancel)

    async def _fake_page_closed(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(lifecycle, "publish_page_closed", _fake_page_closed)

    resp = await lifecycle.terminate_agent_op(
        database,
        event_bus,
        9,
        TerminateAgentRequest(force=True),
        stub_pool,  # type: ignore[arg-type]
    )
    assert resp.status == "enqueued"
    assert resp.shell_sessions is None
    assert captured == {"agent_id": 9}
    assert cancelled == [(9, 91)]


@pytest.mark.asyncio
async def test_launch_agent_op_hosted_failure_preserves_its_row(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object, database: Database, event_bus: EventBus
) -> None:
    """A failed legacy prompt insert leaves the row for explicit repair."""

    def _boom(
        _db: object, _bus: object, _pool: object, _agent_id: int, _prompt: str, _source: str
    ) -> int:
        raise RuntimeError("prompt insert failed")

    monkeypatch.setattr(launch, "_insert_prompt_blocking", _boom)
    reclaimed: list[tuple[int, str]] = []

    def _fake_reclaim(
        _db: object, _bus: object, agent_id: int, _pool: object, *, source: str
    ) -> list[str]:
        reclaimed.append((agent_id, source))
        return []

    monkeypatch.setattr(lifecycle, "force_mark_terminated", _fake_reclaim)

    body = LaunchAgentRequest(agent_id=7, prompt="go", prompt_source="user")
    with pytest.raises(RuntimeError, match="prompt insert failed"):
        await lifecycle.launch_agent_op(database, event_bus, body, stub_pool)  # type: ignore[arg-type]
    assert reclaimed == []


@pytest.mark.asyncio
async def test_launch_agent_op_hosted_validation_failure_preserves_its_row(
    monkeypatch: pytest.MonkeyPatch, stub_pool: object, database: Database, event_bus: EventBus
) -> None:
    """A runner rejection is reported by the gateway; it never terminates creation."""

    def _boom_validate(*_a: object, **_k: object) -> None:
        raise RuntimeError("bad model config")

    monkeypatch.setattr("base.lm.factory.validate_model_config", _boom_validate)
    reclaimed: list[tuple[int, str]] = []

    def _fake_reclaim(
        _db: object, _bus: object, agent_id: int, _pool: object, *, source: str
    ) -> list[str]:
        reclaimed.append((agent_id, source))
        return []

    monkeypatch.setattr(lifecycle, "force_mark_terminated", _fake_reclaim)

    body = LaunchAgentRequest(agent_id=7, prompt="go", prompt_source="user")
    with pytest.raises(RuntimeError, match="bad model config"):
        await lifecycle.launch_agent_op(database, event_bus, body, stub_pool)  # type: ignore[arg-type]
    assert reclaimed == []
