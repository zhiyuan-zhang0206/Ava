"""`base.db` connection helpers read settings once
and hand back a working connection / pool, so call sites stop
hand-writing `psycopg.connect(settings.data_plane.db_url)`. These pin that the
helpers read the live settings URL (the conftest testcontainer) and pass options through.

The second half is the client-side code-version gate every pooled session of these
helpers carries: a session under a lower code version than
`deployment_state.min_code_version` terminates its process. Termination is
`os._exit`, replaced there by a function that raises `_Exited`, so a refusal that
would end a process ends only the test. The gate rides the baseline-session
restore, so most of those tests drive `_restore_pooled_session` (and its async
twin) directly, against a fake connection for the read schedule and a real
Postgres for what the SQL actually does.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import types
from collections.abc import Iterator
from typing import Any, NoReturn

import loguru
import psycopg
import pytest
from loguru import logger as loguru_logger

from base import db
from base.agents.exit_codes import CODE_BEHIND_MINIMUM_EXIT_CODE
from base.config import settings
from base.db import Database, connections
from base.db import code_version_gate as gate
from base.telemetry import process_name


def _session_direct_url(_config: object = None, **_kwargs: object) -> str:
    return settings.data_plane.db_url


def test_connect_runs_a_query(
    process_gate: gate.ProcessDbGate,
) -> None:
    with db.connect(gate=process_gate) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)


def test_connect_autocommit_passthrough(process_gate: gate.ProcessDbGate) -> None:
    with db.connect(autocommit=True, gate=process_gate) as conn:
        assert conn.autocommit is True


def test_connect_defaults_to_manual_commit(
    process_gate: gate.ProcessDbGate,
) -> None:
    with db.connect(gate=process_gate) as conn:
        assert conn.autocommit is False


def test_connect_url_bounds_statements_unless_unbounded() -> None:
    """Against a real Postgres named explicitly: the door delivers the 60s
    statement ceiling by default and none when the caller dials unbounded."""
    url = settings.data_plane.db_url
    with db.connect_url(url) as conn:
        assert conn.execute("SHOW statement_timeout").fetchone() == ("1min",)
    with db.connect_url(url, autocommit=True, unbounded=True) as conn:
        assert conn.autocommit is True
        assert conn.execute("SHOW statement_timeout").fetchone() == ("0",)


def test_pool_hands_out_working_connections(
    process_gate: gate.ProcessDbGate,
) -> None:
    pool = db.pool(gate=process_gate, min_size=1, max_size=2)
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)
    finally:
        pool.close()


# ── the code-version gate ────────────────────────────────────────────────────

_VERSION = 500


class _Exited(BaseException):
    """What the patched `os._exit` raises: the process would have ended here."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


@pytest.fixture
def _gated_process(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """A gated process at a fixed version whose `os._exit` is observable.

    The public process posture/version answers and the owned read timestamp are replaced for
    the test and restored after; `hard_exit` also closes loguru's sinks and the
    stdlib handlers, so both are neutralized to keep the session's. Returns the
    exit codes attempted.
    """
    exits: list[int] = []

    def fake_exit(code: int) -> NoReturn:
        exits.append(code)
        raise _Exited(code)

    def keep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(os, "_exit", fake_exit)
    monkeypatch.setattr(loguru_logger, "remove", keep)
    monkeypatch.setattr(logging, "shutdown", keep)
    return exits


@pytest.fixture
def process_gate(_gated_process: list[int]) -> gate.ProcessDbGate:
    """The test process's explicit budget, with the original fixed version and identity."""
    return gate.ProcessDbGate(version=lambda: _VERSION, process=process_name())


# Every test retains the original observable hard-exit instrumentation.
pytestmark = pytest.mark.usefixtures("_gated_process")


def _clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the gate's clock with a list-backed one the test advances."""
    now = [1000.0]
    monkeypatch.setattr(gate, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
    return now


# ── the read schedule ────────────────────────────────────────────────────────


def test_the_first_borrow_reads_and_the_next_thirty_seconds_do_not(
    process_gate: gate.ProcessDbGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _clock(monkeypatch)
    assert process_gate.min_read_due() is True
    process_gate.observe_minimum(0)
    assert process_gate.min_read_due() is False
    now[0] += gate.MIN_REFRESH_INTERVAL_S - 0.1
    assert process_gate.min_read_due() is False
    now[0] += 0.1
    assert process_gate.min_read_due() is True


def test_an_exempt_process_never_reads_the_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    process_gate = gate.ProcessDbGate(
        process="cli", version=lambda: pytest.fail("the CLI needs no version"), exempt=True
    )
    assert process_gate.min_read_due() is False


# ── the verdict ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("minimum", [0, _VERSION - 1, _VERSION])
def test_a_process_at_or_above_the_minimum_carries_on(
    process_gate: gate.ProcessDbGate, minimum: int, _gated_process: list[int]
) -> None:
    process_gate.observe_minimum(minimum)
    assert _gated_process == []


def test_a_process_below_the_minimum_logs_critical_and_exits(
    process_gate: gate.ProcessDbGate,
    _gated_process: list[int],
) -> None:
    rendered: list[str] = []

    def capture(message: loguru.Message) -> None:
        rendered.append(message.record["message"])

    sink_id = loguru_logger.add(capture, level="CRITICAL", diagnose=False)
    try:
        with pytest.raises(_Exited) as exited:
            process_gate.observe_minimum(_VERSION + 1)
    finally:
        type(loguru_logger).remove(loguru_logger, sink_id)  # the fixture stubbed the instance's

    assert exited.value.code == CODE_BEHIND_MINIMUM_EXIT_CODE
    assert _gated_process == [CODE_BEHIND_MINIMUM_EXIT_CODE]
    (line,) = rendered
    assert f"process {process_name()} runs code version {_VERSION}" in line
    assert f"below the cluster minimum {_VERSION + 1}" in line
    assert f"exiting with code {CODE_BEHIND_MINIMUM_EXIT_CODE}" in line


def test_a_process_that_dials_before_opening_its_sinks_still_says_why(
    process_gate: gate.ProcessDbGate,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """loguru discards a record no handler receives; the refusal then goes to stderr."""
    monkeypatch.setattr(gate, "_has_log_sink", lambda: False)
    with pytest.raises(_Exited):
        process_gate.observe_minimum(_VERSION + 1)
    assert f"below the cluster minimum {_VERSION + 1}" in capsys.readouterr().err


# ── the restore statement schedule (fake connection) ─────────────────────────


class _Cursor:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[str, ...] = ()) -> _Cursor:
        self.conn.executed.append((sql, params))
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        return ("60000", self.conn.executed[-1][1][0], self.conn.minimum)


class _FakeConn:
    """Records every statement with its parameters, and answers the combined read
    with `minimum`."""

    def __init__(self, minimum: int) -> None:
        self.minimum = minimum
        self.executed: list[tuple[str, tuple[str, ...]]] = []
        self.commits = 0

    def execute(self, sql: str, params: tuple[str, ...] = ()) -> None:
        self.executed.append((sql, params))

    def cursor(self, **_kwargs: object) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        self.commits += 1


def test_the_restore_reads_the_minimum_at_most_every_interval(
    process_gate: gate.ProcessDbGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two statements per borrow, always; the second is the plain statement ceiling
    except on the borrow that also reads the minimum."""
    now = _clock(monkeypatch)
    conn = _FakeConn(minimum=0)
    restore: Any = connections._restore_pooled_session

    restore(conn, gate=process_gate)
    restore(conn, gate=process_gate)
    now[0] += gate.MIN_REFRESH_INTERVAL_S
    restore(conn, gate=process_gate)

    name = (process_gate.application_name(),)
    reset, plain = connections.PG_POOLED_BASELINE_RESTORE_SQL
    combined = connections.PG_POOLED_RESTORE_WITH_MIN_SQL
    assert conn.executed == [
        (reset, ()),
        (combined, name),
        (reset, ()),
        (plain, name),
        (reset, ()),
        (combined, name),
    ]
    assert conn.commits == 3


def test_the_restore_refuses_when_the_read_minimum_is_above_this_process(
    process_gate: gate.ProcessDbGate,
    _gated_process: list[int],
) -> None:
    conn = _FakeConn(minimum=_VERSION + 10)
    restore: Any = connections._restore_pooled_session
    with pytest.raises(_Exited):
        restore(conn, gate=process_gate)
    assert _gated_process == [CODE_BEHIND_MINIMUM_EXIT_CODE]
    assert conn.commits == 0


class _AsyncCursor(_Cursor):
    async def __aenter__(self) -> _AsyncCursor:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, sql: str, params: tuple[str, ...] = ()) -> _AsyncCursor:  # pyright: ignore[reportIncompatibleMethodOverride]
        self.conn.executed.append((sql, params))
        return self

    async def fetchone(self) -> tuple[Any, ...] | None:  # pyright: ignore[reportIncompatibleMethodOverride]
        return ("60000", self.conn.executed[-1][1][0], self.conn.minimum)


class _FakeAsyncConn(_FakeConn):
    async def execute(self, sql: str, params: tuple[str, ...] = ()) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
        self.executed.append((sql, params))

    def cursor(self, **_kwargs: object) -> _AsyncCursor:  # pyright: ignore[reportIncompatibleMethodOverride]
        return _AsyncCursor(self)

    async def commit(self) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
        self.commits += 1


async def test_the_async_restore_follows_the_same_schedule_and_verdict(
    process_gate: gate.ProcessDbGate, monkeypatch: pytest.MonkeyPatch, _gated_process: list[int]
) -> None:
    now = _clock(monkeypatch)
    conn = _FakeAsyncConn(minimum=0)
    restore: Any = connections._restore_pooled_session_async

    await restore(conn, gate=process_gate)
    await restore(conn, gate=process_gate)
    now[0] += gate.MIN_REFRESH_INTERVAL_S
    await restore(conn, gate=process_gate)

    name = (process_gate.application_name(),)
    reset, plain = connections.PG_POOLED_BASELINE_RESTORE_SQL
    combined = connections.PG_POOLED_RESTORE_WITH_MIN_SQL
    assert conn.executed == [
        (reset, ()),
        (combined, name),
        (reset, ()),
        (plain, name),
        (reset, ()),
        (combined, name),
    ]

    conn.minimum = _VERSION + 1
    now[0] += gate.MIN_REFRESH_INTERVAL_S
    with pytest.raises(_Exited):
        await restore(conn, gate=process_gate)
    assert _gated_process == [CODE_BEHIND_MINIMUM_EXIT_CODE]


def test_an_exempt_process_keeps_the_plain_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    process_gate = gate.ProcessDbGate(
        process="cli", version=lambda: pytest.fail("the CLI needs no version"), exempt=True
    )
    conn = _FakeConn(minimum=10**9)
    restore: Any = connections._restore_pooled_session

    restore(conn, gate=process_gate)

    reset, plain = connections.PG_POOLED_BASELINE_RESTORE_SQL
    assert conn.executed == [(reset, ()), (plain, ("ava:cli",))]


# ── real Postgres ────────────────────────────────────────────────────────────


@pytest.fixture
def stored_minimum() -> Iterator[int]:
    """The suite database's current `min_code_version`, restored when the test ends.

    The database outlives the test (one per worker), so a test that moves the
    minimum must put it back or every later gated dial in the worker would be
    judged against it.
    """
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
        row = conn.execute("SELECT min_code_version FROM deployment_state WHERE id = 1").fetchone()
        assert row is not None
        original = int(row[0])
        try:
            yield original
        finally:
            conn.execute("UPDATE deployment_state SET min_code_version = %s", (original,))


def _set_minimum(value: int) -> None:
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
        conn.execute("UPDATE deployment_state SET min_code_version = %s", (value,))


def _read_minimum() -> int:
    with psycopg.connect(settings.data_plane.db_url) as conn:
        row = conn.execute("SELECT min_code_version FROM deployment_state").fetchone()
    assert row is not None
    return int(row[0])


def test_gateway_start_raises_the_minimum_to_its_version(
    process_gate: gate.ProcessDbGate, stored_minimum: int, database: Database
) -> None:
    _set_minimum(0)
    assert process_gate.raise_min_code_version(database) == _VERSION
    assert _read_minimum() == _VERSION


def test_a_repeat_start_at_the_same_version_changes_nothing(
    process_gate: gate.ProcessDbGate, stored_minimum: int, database: Database
) -> None:
    _set_minimum(_VERSION)
    assert process_gate.raise_min_code_version(database) == _VERSION
    assert _read_minimum() == _VERSION


def test_the_raise_is_greatest_and_never_lowers_the_minimum(
    monkeypatch: pytest.MonkeyPatch, stored_minimum: int, database: Database
) -> None:
    """The gate stops an older process before its update runs, so the race GREATEST
    closes (two gateways starting at once) is reproduced by an exempt dial, which
    skips the read, carrying an older version."""
    _set_minimum(_VERSION + 25)
    process_gate = gate.ProcessDbGate(
        process="cli", version=lambda: pytest.fail("the CLI needs no version"), exempt=True
    )

    process_gate = gate.ProcessDbGate(process="cli", version=lambda: _VERSION, exempt=True)
    assert (
        process_gate.raise_min_code_version(Database.from_settings(gate=process_gate))
        == _VERSION + 25
    )
    assert _read_minimum() == _VERSION + 25


def test_the_raise_refuses_when_the_singleton_row_is_missing(
    process_gate: gate.ProcessDbGate, stored_minimum: int, database: Database
) -> None:
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
        conn.execute("DELETE FROM deployment_state")
        try:
            with pytest.raises(RuntimeError, match="singleton row is missing"):
                process_gate.raise_min_code_version(database)
        finally:
            conn.execute(
                "INSERT INTO deployment_state (id, min_code_version) VALUES (1, %s)",
                (stored_minimum,),
            )


def test_a_pooled_dial_below_the_stored_minimum_ends_the_process(
    process_gate: gate.ProcessDbGate, stored_minimum: int, _gated_process: list[int]
) -> None:
    _set_minimum(_VERSION + 1)
    with pytest.raises(_Exited):
        db.connect(gate=process_gate)
    assert _gated_process == [CODE_BEHIND_MINIMUM_EXIT_CODE]


def test_a_pooled_dial_at_the_stored_minimum_works_and_keeps_the_ceiling(
    process_gate: gate.ProcessDbGate, stored_minimum: int, _gated_process: list[int]
) -> None:
    """The combined statement is `SET statement_timeout` as an expression: after the
    read, the session carries the same 60s ceiling the plain restore gives."""
    _set_minimum(_VERSION)
    with db.connect(gate=process_gate) as conn:
        row = conn.execute("SHOW statement_timeout").fetchone()
        assert row is not None and row[0] == "1min"
        row = conn.execute("SHOW application_name").fetchone()
        assert row is not None and row[0] == f"ava:{process_name()}:v{_VERSION}"
    assert _gated_process == []


def test_a_dict_row_pool_borrows_through_the_gate(
    process_gate: gate.ProcessDbGate, stored_minimum: int, _gated_process: list[int]
) -> None:
    """The read builds its own tuple cursor, so a pool whose connections return
    dicts is not broken by it."""
    from psycopg.rows import dict_row

    _set_minimum(_VERSION)
    pool = db.pool(gate=process_gate, min_size=1, max_size=1, row_factory=dict_row, autocommit=True)
    try:
        with pool.connection() as conn:
            row = conn.execute("SELECT 1 AS one").fetchone()
        assert row == {"one": 1}
    finally:
        pool.close()
    assert _gated_process == []


async def test_the_async_restore_reads_the_stored_minimum(
    process_gate: gate.ProcessDbGate,
    monkeypatch: pytest.MonkeyPatch,
    stored_minimum: int,
    _gated_process: list[int],
) -> None:
    now = _clock(monkeypatch)
    _set_minimum(_VERSION)
    restore: Any = connections._restore_pooled_session_async
    async with await psycopg.AsyncConnection.connect(settings.data_plane.db_url) as aconn:
        await restore(aconn, gate=process_gate)
        cur = await aconn.execute("SHOW statement_timeout")
        row = await cur.fetchone()
        assert row is not None and row[0] == "1min"
    assert _gated_process == []

    _set_minimum(_VERSION + 1)
    assert process_gate.min_read_due() is False  # the read above is still fresh
    now[0] += gate.MIN_REFRESH_INTERVAL_S
    assert process_gate.min_read_due() is True
    async with await psycopg.AsyncConnection.connect(settings.data_plane.db_url) as aconn:
        with pytest.raises(_Exited):
            await restore(aconn, gate=process_gate)


def test_direct_dials_are_not_gated_and_carry_no_process_name(
    process_gate: gate.ProcessDbGate,
    monkeypatch: pytest.MonkeyPatch,
    stored_minimum: int,
    _gated_process: list[int],
) -> None:
    _set_minimum(_VERSION + 1)
    monkeypatch.setattr(connections, "direct_db_url", _session_direct_url)
    with db.connect(direct=True, gate=process_gate) as conn:
        params = conn.info.get_parameters()
        assert not params.get("application_name", "").startswith("ava:")
    assert _gated_process == []


def test_an_exempt_process_dials_as_the_cli_without_a_version(
    monkeypatch: pytest.MonkeyPatch, stored_minimum: int, _gated_process: list[int]
) -> None:
    _set_minimum(_VERSION + 1)
    process_gate = gate.ProcessDbGate(
        process="cli", version=lambda: pytest.fail("the CLI needs no version"), exempt=True
    )

    with db.connect(gate=process_gate) as conn:
        row = conn.execute("SHOW application_name").fetchone()
        assert row is not None and row[0] == "ava:cli"
    assert _gated_process == []


def test_a_runner_login_reads_the_minimum_and_cannot_write_it(
    process_gate: gate.ProcessDbGate,
    monkeypatch: pytest.MonkeyPatch,
    stored_minimum: int,
    _gated_process: list[int],
) -> None:
    """The gate needs no new grant: the runner group's blanket `SELECT` covers the
    column, and its inability to `UPDATE` the row (only the gateway raises the
    minimum) is unchanged."""
    from tests._containers import runner_projection

    _set_minimum(_VERSION)
    runner_url = runner_projection()
    monkeypatch.setattr(settings.data_plane, "db_url", runner_url)

    with db.connect(gate=process_gate) as conn:
        assert process_gate.min_read_due() is False  # this dial read the minimum
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE deployment_state SET min_code_version = 0")
    assert _gated_process == []


def test_independent_handles_and_async_restore_share_one_process_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _clock(monkeypatch)
    owner = gate.ProcessDbGate(version=lambda: _VERSION, process="test-process")
    original_connect = connections.psycopg.connect
    test_thread = threading.current_thread()
    restored: list[_FakeConn] = []

    def dial(url: str, **kwargs: Any) -> Any:
        if threading.current_thread() is not test_thread:
            return original_connect(url, **kwargs)
        conn = _FakeConn(minimum=0)
        restored.append(conn)
        return conn

    monkeypatch.setattr(connections.psycopg, "connect", dial)
    first = Database.from_settings(gate=owner)
    second = Database.from_settings(gate=owner)
    first.connect()
    now[0] += 29.9
    second.connect()
    # Refreshing a handle's config does not reset this process's clock.
    Database.from_settings(gate=owner).connect()
    assert [conn.executed[1][0] for conn in restored] == [
        connections.PG_POOLED_RESTORE_WITH_MIN_SQL,
        connections.PG_POOLED_BASELINE_RESTORE_SQL[1],
        connections.PG_POOLED_BASELINE_RESTORE_SQL[1],
    ]
    assert all(len(conn.executed) == 2 and conn.commits == 1 for conn in restored)
    assert all(conn.executed[1][1] == (f"ava:test-process:v{_VERSION}",) for conn in restored)
    now[0] += 0.1
    first.connect()
    assert restored[-1].executed[1][0] == connections.PG_POOLED_RESTORE_WITH_MIN_SQL


async def test_sync_and_async_pool_callbacks_retain_the_same_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _clock(monkeypatch)
    owner = gate.ProcessDbGate(version=lambda: _VERSION, process="test-process")

    def pool_factory(_url: str, **kwargs: Any) -> Any:
        return types.SimpleNamespace(**kwargs)

    monkeypatch.setattr(connections, "ConnectionPool", pool_factory)
    first: Any = Database.from_settings(gate=owner).pool()
    pool_type: Any = pool_factory
    second: Any = Database.from_settings(gate=owner).async_pool(
        pool_type, min_size=1, max_size=2, timeout=1
    )
    sync_conn = _FakeConn(0)
    first.configure(sync_conn)
    first.check(sync_conn)
    async_conn = _FakeAsyncConn(0)
    await second.check(async_conn)
    assert sync_conn.executed[1][0] == connections.PG_POOLED_RESTORE_WITH_MIN_SQL
    assert sync_conn.executed[3][0] == connections.PG_POOLED_BASELINE_RESTORE_SQL[1]
    assert async_conn.executed[1][0] == connections.PG_POOLED_BASELINE_RESTORE_SQL[1]
    now[0] += 30
    await second.check(async_conn)
    assert async_conn.executed[3][0] == connections.PG_POOLED_RESTORE_WITH_MIN_SQL
    first.check(sync_conn)
    assert sync_conn.executed[5][0] == connections.PG_POOLED_BASELINE_RESTORE_SQL[1]
    # A different process starts with an independent budget.
    other = gate.ProcessDbGate(version=lambda: _VERSION, process="other-process")
    assert other.min_read_due() is True


def test_exempt_process_owner_never_needs_a_loaded_code_version() -> None:
    def no_version() -> int:
        raise AssertionError("an exempt CLI must not resolve Git")

    owner = gate.ProcessDbGate(version=no_version, process="cli", exempt=True)
    assert owner.application_name() == "ava:cli"
    assert owner.min_read_due() is False


def test_explicit_process_gate_hard_exits_from_a_real_worker() -> None:
    source = """
import threading
from base.db.code_version_gate import ProcessDbGate
owner = ProcessDbGate(version=lambda: 1, process="worker-proof")
worker = threading.Thread(target=lambda: owner.observe_minimum(2))
worker.start()
worker.join(timeout=2)
raise RuntimeError("the stale worker did not terminate its process")
"""
    result = subprocess.run(  # noqa: S603 — own interpreter and fixed source in pytest isolation
        [sys.executable, "-c", source], capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == CODE_BEHIND_MINIMUM_EXIT_CODE
    assert "process worker-proof runs code version 1" in result.stderr
