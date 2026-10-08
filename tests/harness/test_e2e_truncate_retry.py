"""Unit tests for the deadlock-retried TRUNCATE helper (`tests/e2e/_truncate.py`).

Cover the retry semantics — a first-attempt `DeadlockDetected` is rolled back
and the retry wins; three consecutive deadlocks re-raise; any other error
passes straight through. The real two-session deadlock (and its CI DETAIL) is
reproduced against Postgres outside the suite, not here.
"""

from __future__ import annotations

from typing import Any, LiteralString, cast

import psycopg
import pytest

from tests.e2e._truncate import truncate_with_deadlock_retry

_STATEMENT: LiteralString = "TRUNCATE inbound_messages CASCADE"


class _FakeCursor:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def execute(self, statement: str) -> None:
        self._conn.executed.append(statement)
        outcome = self._conn.next_outcome()
        if outcome is not None:
            raise outcome


class _FakeConnection:
    """Records TRUNCATE traffic; pops one scripted outcome per `execute`."""

    def __init__(self, outcomes: list[psycopg.Error | None]) -> None:
        self._outcomes = list(outcomes)
        self.executed: list[str] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def next_outcome(self) -> psycopg.Error | None:
        if not self._outcomes:
            raise AssertionError("execute called beyond the scripted outcomes")
        return self._outcomes.pop(0)


def _run(conn: _FakeConnection) -> None:
    truncate_with_deadlock_retry(cast(psycopg.Connection[Any], conn), _STATEMENT)


def test_deadlock_on_first_attempt_retries_and_wins() -> None:
    conn = _FakeConnection([psycopg.errors.DeadlockDetected("deadlock detected"), None])
    _run(conn)
    assert conn.executed == [_STATEMENT, _STATEMENT]
    assert conn.rollbacks == 1
    assert conn.commits == 1


def test_three_consecutive_deadlocks_re_raise() -> None:
    conn = _FakeConnection([psycopg.errors.DeadlockDetected("deadlock detected")] * 3)
    with pytest.raises(psycopg.errors.DeadlockDetected):
        _run(conn)
    assert len(conn.executed) == 3
    assert conn.rollbacks == 2
    assert conn.commits == 0


def test_non_deadlock_error_passes_straight_through() -> None:
    conn = _FakeConnection([psycopg.errors.QueryCanceled("cancelled")])
    with pytest.raises(psycopg.errors.QueryCanceled):
        _run(conn)
    assert len(conn.executed) == 1
    assert conn.rollbacks == 0
    assert conn.commits == 0
