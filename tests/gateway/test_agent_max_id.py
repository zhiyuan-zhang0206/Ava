"""Gateway agent-registry max-id gauge (task #2010).

Locks the two pieces the flusher composes:

- ``read_max_agent_id_blocking`` — the exact SQL and the None guards (empty
  table / NULL row).
- ``emit_max_agent_id`` — one ``agent_registry`` telemetry event carrying the
  ``max_id`` payload (the OTLP side records it as a gauge via
  ``_METRIC_DISPOSITION``, locked in test_event_contract.py).

The flusher loop itself is the same structure as
``_auth401_log.auth401_flusher`` / ``_latency.latency_flusher`` — a dropped
sample logged and retried on the next tick — so it is covered by the shared
behavior of those loops plus the two unit tests here.
"""

from __future__ import annotations

from typing import cast

import pytest
from psycopg_pool import ConnectionPool

import gateway._agent_max_id as agent_max_id


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
    assert agent_max_id.read_max_agent_id_blocking(pool) == 9_999
    assert pool.executed == ["SELECT max(id) FROM agents"]  # type: ignore[reportUnknownMemberType]
    # one borrow per sample — nothing held between ticks
    assert pool.connections == 1  # type: ignore[reportUnknownMemberType]


def test_read_max_agent_id_returns_none_for_empty_registry() -> None:
    assert agent_max_id.read_max_agent_id_blocking(cast(ConnectionPool, _FakePool(None))) is None
    # NULL row (max over an empty table) is also None, never 0
    assert agent_max_id.read_max_agent_id_blocking(cast(ConnectionPool, _FakePool((None,)))) is None


def test_emit_max_agent_id_sends_one_telemetry_event(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted: list[tuple[str, str, dict[str, object]]] = []

    def _fake_emit(
        category: str, event_name: str, *, attributes: dict[str, object] | None = None
    ) -> None:
        emitted.append((category, event_name, attributes or {}))

    monkeypatch.setattr(agent_max_id.telemetry, "emit", _fake_emit)

    agent_max_id.emit_max_agent_id(9_999)
    assert emitted == [("telemetry", "agent_registry", {"max_id": 9_999})]
