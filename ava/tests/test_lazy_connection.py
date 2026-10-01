"""_LazyConnection rebuilds a dead connection instead of reusing it.

Regression for agent 2147 (2026-08-03): ava.DB held one long-lived psycopg
connection and never checked `closed`/`broken`, so once a network outage killed
the socket (psycopg marks the conn broken after the failed read), every
subsequent ava.DB op in that process failed with "the connection is closed"
until the process restarted — while the agent main-loop pool (which checks
every borrow) self-healed as soon as the network returned.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import pytest

from ava._settings import _LazyConnection


class _FakeConn:
    """Minimal stand-in for psycopg.Connection exposing closed/broken."""

    def __init__(self) -> None:
        self.closed = False
        self.broken = False

    def close(self) -> None:
        self.closed = True


def test_reuses_live_connection() -> None:
    factory_calls: list[int] = []
    proxy = _LazyConnection(_counting_factory(factory_calls), "test")
    first = cast(_FakeConn, proxy._get())
    second = cast(_FakeConn, proxy._get())
    assert first is second
    assert factory_calls == [1]


def test_rebuilds_after_close() -> None:
    factory_calls: list[int] = []
    proxy = _LazyConnection(_counting_factory(factory_calls), "test")
    conn = cast(_FakeConn, proxy._get())
    conn.close()
    rebuilt = proxy._get()
    assert rebuilt is not conn
    assert len(factory_calls) == 2
    assert not cast(_FakeConn, rebuilt).closed


def test_rebuilds_after_broken() -> None:
    factory_calls: list[int] = []
    proxy = _LazyConnection(_counting_factory(factory_calls), "test")
    conn = cast(_FakeConn, proxy._get())
    conn.broken = True
    rebuilt = proxy._get()
    assert rebuilt is not conn
    assert len(factory_calls) == 2


def test_concurrent_get_returns_single_connection() -> None:
    factory_calls: list[int] = []
    proxy = _LazyConnection(_counting_factory(factory_calls), "test")
    results = [proxy._get() for _ in range(50)]
    assert len({id(r) for r in results}) == 1
    assert factory_calls == [1]


def test_dead_conn_closed_before_rebuild() -> None:
    factory_calls: list[int] = []

    def factory() -> object:
        factory_calls.append(1)
        return _FakeConn()

    proxy = _LazyConnection(factory, "test")
    conn = cast(_FakeConn, proxy._get())
    conn.broken = True
    proxy._get()
    assert conn.closed  # the dead conn was closed before being discarded
    assert len(factory_calls) == 2


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: setattr(c, "closed", True),  # type: ignore[arg-type],
        lambda c: setattr(c, "broken", True),  # type: ignore[arg-type],
    ],
)
def test_dead_detection_paths(mutate: object) -> None:
    factory_calls: list[int] = []
    proxy = _LazyConnection(_counting_factory(factory_calls), "test")
    conn = cast(_FakeConn, proxy._get())
    mutate(conn)  # type: ignore[arg-type]
    proxy._get()
    assert len(factory_calls) == 2


def _counting_factory(calls: list[int]) -> Callable[[], object]:
    def factory() -> object:
        calls.append(1)
        return _FakeConn()

    return factory
