"""Agent-registry max-id gauge (task #2010), a loop of the events-maintenance service.

Locks the pieces the loop composes:

- ``read_max_agent_id_blocking`` — the exact SQL and the None guards (empty
  table / NULL row).
- ``emit_max_agent_id`` — one ``agent_registry`` telemetry event carrying the
  ``max_id`` payload (the OTLP side records it as a gauge via
  ``_METRIC_DISPOSITION``, locked in test_event_contract.py).

- ``registry_gauge_round`` / ``registry_gauge_loop`` — one sample per round, a
  sample at once, a crash ends the loop.
"""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from psycopg_pool import ConnectionPool

from base.daemon.loop_health import LoopProgress
from services.events_maintenance import registry_gauge


class _FakeCursor:
    def __init__(self, row: object) -> None:
        self._row = row
        self.executed: list[str] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str) -> None:
        self.executed.append(sql)

    def fetchone(self) -> object:
        return self._row


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return self._cursor


class _FakePool:
    def __init__(self, row: object) -> None:
        self._row = row
        self.connections = 0
        self.executed: list[str] = []

    def connection(self) -> _FakeConnection:
        self.connections += 1
        cursor = _FakeCursor(self._row)
        self.executed = cursor.executed
        return _FakeConnection(cursor)


def test_read_max_agent_id_queries_the_agents_table() -> None:
    pool = cast(ConnectionPool, _FakePool((9_999,)))
    assert registry_gauge.read_max_agent_id_blocking(pool) == 9_999
    assert pool.executed == ["SELECT max(id) FROM agents"]  # type: ignore[reportUnknownMemberType]
    # one borrow per sample — nothing held between ticks
    assert pool.connections == 1  # type: ignore[reportUnknownMemberType]


def test_read_max_agent_id_returns_none_for_empty_registry() -> None:
    assert registry_gauge.read_max_agent_id_blocking(cast(ConnectionPool, _FakePool(None))) is None
    # NULL row (max over an empty table) is also None, never 0
    assert (
        registry_gauge.read_max_agent_id_blocking(cast(ConnectionPool, _FakePool((None,)))) is None
    )


def test_emit_max_agent_id_sends_one_telemetry_event(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted: list[tuple[str, str, dict[str, object]]] = []

    def _fake_emit(
        category: str, event_name: str, *, attributes: dict[str, object] | None = None
    ) -> None:
        emitted.append((category, event_name, attributes or {}))

    monkeypatch.setattr(registry_gauge.telemetry, "emit", _fake_emit)

    registry_gauge.emit_max_agent_id(9_999)
    assert emitted == [("telemetry", "agent_registry", {"max_id": 9_999})]


async def test_a_round_samples_once_and_emits(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted: list[int] = []

    def read(_pool: object) -> int:
        return 42

    monkeypatch.setattr(registry_gauge, "read_max_agent_id_blocking", read)
    monkeypatch.setattr(registry_gauge, "emit_max_agent_id", emitted.append)

    await registry_gauge.registry_gauge_round(cast(ConnectionPool, object()))

    assert emitted == [42]


async def test_a_round_over_an_empty_registry_emits_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[int] = []

    def read(_pool: object) -> None:
        return None

    monkeypatch.setattr(registry_gauge, "read_max_agent_id_blocking", read)
    monkeypatch.setattr(registry_gauge, "emit_max_agent_id", emitted.append)

    await registry_gauge.registry_gauge_round(cast(ConnectionPool, object()))

    assert emitted == []


async def test_the_loop_samples_at_once_then_paces(monkeypatch: pytest.MonkeyPatch) -> None:
    samples: list[int] = []

    def read(_pool: object) -> int:
        return 7

    monkeypatch.setattr(registry_gauge, "read_max_agent_id_blocking", read)
    monkeypatch.setattr(registry_gauge, "emit_max_agent_id", samples.append)
    task = asyncio.create_task(
        registry_gauge.registry_gauge_loop(cast(ConnectionPool, object()), LoopProgress("g", 180.0))
    )
    try:
        for _ in range(200):
            if samples:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert samples == [7]  # the sample at once; the 60 s wait never elapses


async def test_a_failing_sample_ends_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_pool: object) -> int:
        raise RuntimeError("bad registry")

    monkeypatch.setattr(registry_gauge, "read_max_agent_id_blocking", boom)

    with pytest.raises(RuntimeError, match="bad registry"):
        await registry_gauge.registry_gauge_loop(
            cast(ConnectionPool, object()), LoopProgress("g", 180.0)
        )


def test_the_gateway_no_longer_samples_the_registry() -> None:
    """The gauge is a loop of the events-maintenance service: the gateway module is
    gone and its lifespan starts no flusher for it."""
    import importlib.util

    import gateway.app

    assert importlib.util.find_spec("gateway.agents.max_id_gauge") is None
    assert "max_id" not in vars(gateway.app)
