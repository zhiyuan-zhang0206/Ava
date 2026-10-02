"""`base.db` / `base.events.live.redis_client` connection helpers read settings once
and hand back a working connection / pool / sync client, so call sites stop
hand-writing `psycopg.connect(settings.data_plane.db_url)` and
`redis.Redis.from_url(settings.data_plane.redis_url)`. These pin that the helpers read the
live settings URL (the conftest testcontainer) and pass options through.

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
import types
from collections.abc import Iterator
from typing import Any, LiteralString, NoReturn, cast

import loguru
import psycopg
import pytest
from loguru import logger as loguru_logger

from base import db
from base.agents.exit_codes import CODE_BEHIND_MINIMUM_EXIT_CODE
from base.config import settings
from base.db import code_version_gate as gate
from base.db import connections
from base.events.live.bus import EventBus
from base.log.sinks import add_sink
from base.native_process import code_version
from base.telemetry import process_name


def _session_direct_url(_config: object = None) -> str:
    return settings.data_plane.db_url


def test_connect_runs_a_query() -> None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)


def test_connect_autocommit_passthrough() -> None:
    with db.connect(autocommit=True) as conn:
        assert conn.autocommit is True


def test_connect_defaults_to_manual_commit() -> None:
    with db.connect() as conn:
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


def test_pool_hands_out_working_connections() -> None:
    pool = db.pool(min_size=1, max_size=2)
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)
    finally:
        pool.close()


def test_sync_redis_ping() -> None:
    client = EventBus.from_settings().sync_redis()
    try:
        assert client.ping() is True  # pyright: ignore[reportUnknownMemberType]
    finally:
        client.close()


def test_sync_redis_decode_responses_passthrough() -> None:
    client = EventBus.from_settings().sync_redis(decode_responses=True)
    try:
        client.set("ava:test:connect-helper", "v")
        assert client.get("ava:test:connect-helper") == "v"  # str, not bytes
    finally:
        client.delete("ava:test:connect-helper")
        client.close()


# ── the code-version gate ────────────────────────────────────────────────────

_VERSION = 500


class _Exited(BaseException):
    """What the patched `os._exit` raises: the process would have ended here."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


@pytest.fixture(autouse=True)
def _gated_process(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """A gated process at a fixed version whose `os._exit` is observable.

    Process posture, the version cache and the read timestamp are replaced for
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
    monkeypatch.setattr(code_version, "_version", _VERSION)
    monkeypatch.setattr(code_version, "_db_gate_exempt", False)
    monkeypatch.setattr(gate, "_last_read_at", None)
    return exits


def _clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the gate's clock with a list-backed one the test advances."""
    now = [1000.0]
    monkeypatch.setattr(gate, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
    return now


# ── the read schedule ────────────────────────────────────────────────────────


def test_the_first_borrow_reads_and_the_next_thirty_seconds_do_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _clock(monkeypatch)
    assert gate.min_read_due() is True
    gate.observe_minimum(0)
    assert gate.min_read_due() is False
    now[0] += gate.MIN_REFRESH_INTERVAL_S - 0.1
    assert gate.min_read_due() is False
    now[0] += 0.1
    assert gate.min_read_due() is True


def test_an_exempt_process_never_reads_the_minimum() -> None:
    code_version.exempt_from_db_gate()
    assert gate.min_read_due() is False


# ── the verdict ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("minimum", [0, _VERSION - 1, _VERSION])
def test_a_process_at_or_above_the_minimum_carries_on(
    minimum: int, _gated_process: list[int]
) -> None:
    gate.observe_minimum(minimum)
    assert _gated_process == []


def test_a_process_below_the_minimum_logs_critical_and_exits(
    _gated_process: list[int],
) -> None:
    rendered: list[str] = []

    def capture(message: loguru.Message) -> None:
        rendered.append(message.record["message"])

    sink_id = add_sink(capture, level="CRITICAL")
    try:
        with pytest.raises(_Exited) as exited:
            gate.observe_minimum(_VERSION + 1)
    finally:
        type(loguru_logger).remove(loguru_logger, sink_id)  # the fixture stubbed the instance's

    assert exited.value.code == CODE_BEHIND_MINIMUM_EXIT_CODE
    assert _gated_process == [CODE_BEHIND_MINIMUM_EXIT_CODE]
    (line,) = rendered
    assert f"process {process_name()} runs code version {_VERSION}" in line
    assert f"below the cluster minimum {_VERSION + 1}" in line
    assert f"exiting with code {CODE_BEHIND_MINIMUM_EXIT_CODE}" in line


def test_a_process_that_dials_before_opening_its_sinks_still_says_why(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """loguru discards a record no handler receives; the refusal then goes to stderr."""
    monkeypatch.setattr(gate, "_has_log_sink", lambda: False)
    with pytest.raises(_Exited):
        gate.observe_minimum(_VERSION + 1)
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two statements per borrow, always; the second is the plain statement ceiling
    except on the borrow that also reads the minimum."""
    now = _clock(monkeypatch)
    conn = _FakeConn(minimum=0)
    restore: Any = connections._restore_pooled_session

    restore(conn)
    restore(conn)
    now[0] += gate.MIN_REFRESH_INTERVAL_S
    restore(conn)

    name = (gate.application_name(),)
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
    _gated_process: list[int],
) -> None:
    conn = _FakeConn(minimum=_VERSION + 10)
    restore: Any = connections._restore_pooled_session
    with pytest.raises(_Exited):
        restore(conn)
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
    monkeypatch: pytest.MonkeyPatch, _gated_process: list[int]
) -> None:
    now = _clock(monkeypatch)
    conn = _FakeAsyncConn(minimum=0)
    restore: Any = connections._restore_pooled_session_async

    await restore(conn)
    await restore(conn)
    now[0] += gate.MIN_REFRESH_INTERVAL_S
    await restore(conn)

    name = (gate.application_name(),)
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
        await restore(conn)
    assert _gated_process == [CODE_BEHIND_MINIMUM_EXIT_CODE]


def test_an_exempt_process_keeps_the_plain_restore() -> None:
    code_version.exempt_from_db_gate()
    conn = _FakeConn(minimum=10**9)
    restore: Any = connections._restore_pooled_session

    restore(conn)

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


def test_gateway_start_raises_the_minimum_to_its_version(stored_minimum: int) -> None:
    _set_minimum(0)
    assert gate.raise_min_code_version() == _VERSION
    assert _read_minimum() == _VERSION


def test_a_repeat_start_at_the_same_version_changes_nothing(stored_minimum: int) -> None:
    _set_minimum(_VERSION)
    assert gate.raise_min_code_version() == _VERSION
    assert _read_minimum() == _VERSION


def test_the_raise_is_greatest_and_never_lowers_the_minimum(
    monkeypatch: pytest.MonkeyPatch, stored_minimum: int
) -> None:
    """The gate stops an older process before its update runs, so the race GREATEST
    closes (two gateways starting at once) is reproduced by an exempt dial, which
    skips the read, carrying an older version."""
    _set_minimum(_VERSION + 25)
    code_version.exempt_from_db_gate()
    monkeypatch.setattr(code_version, "_version", _VERSION)

    assert gate.raise_min_code_version() == _VERSION + 25
    assert _read_minimum() == _VERSION + 25


def test_the_raise_refuses_when_the_singleton_row_is_missing(stored_minimum: int) -> None:
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
        conn.execute("DELETE FROM deployment_state")
        try:
            with pytest.raises(RuntimeError, match="singleton row is missing"):
                gate.raise_min_code_version()
        finally:
            conn.execute(
                "INSERT INTO deployment_state (id, min_code_version) VALUES (1, %s)",
                (stored_minimum,),
            )


def test_a_pooled_dial_below_the_stored_minimum_ends_the_process(
    stored_minimum: int, _gated_process: list[int]
) -> None:
    _set_minimum(_VERSION + 1)
    with pytest.raises(_Exited):
        db.connect()
    assert _gated_process == [CODE_BEHIND_MINIMUM_EXIT_CODE]


def test_a_pooled_dial_at_the_stored_minimum_works_and_keeps_the_ceiling(
    stored_minimum: int, _gated_process: list[int]
) -> None:
    """The combined statement is `SET statement_timeout` as an expression: after the
    read, the session carries the same 60s ceiling the plain restore gives."""
    _set_minimum(_VERSION)
    with db.connect() as conn:
        row = conn.execute("SHOW statement_timeout").fetchone()
        assert row is not None and row[0] == "1min"
        row = conn.execute("SHOW application_name").fetchone()
        assert row is not None and row[0] == f"ava:{process_name()}:v{_VERSION}"
    assert _gated_process == []


def test_a_dict_row_pool_borrows_through_the_gate(
    stored_minimum: int, _gated_process: list[int]
) -> None:
    """The read builds its own tuple cursor, so a pool whose connections return
    dicts is not broken by it."""
    from psycopg.rows import dict_row

    _set_minimum(_VERSION)
    pool = db.pool(min_size=1, max_size=1, row_factory=dict_row, autocommit=True)
    try:
        with pool.connection() as conn:
            row = conn.execute("SELECT 1 AS one").fetchone()
        assert row == {"one": 1}
    finally:
        pool.close()
    assert _gated_process == []


async def test_the_async_restore_reads_the_stored_minimum(
    monkeypatch: pytest.MonkeyPatch, stored_minimum: int, _gated_process: list[int]
) -> None:
    _set_minimum(_VERSION)
    restore: Any = connections._restore_pooled_session_async
    async with await psycopg.AsyncConnection.connect(settings.data_plane.db_url) as aconn:
        await restore(aconn)
        cur = await aconn.execute("SHOW statement_timeout")
        row = await cur.fetchone()
        assert row is not None and row[0] == "1min"
    assert _gated_process == []

    _set_minimum(_VERSION + 1)
    assert gate.min_read_due() is False  # the read above is still fresh
    monkeypatch.setattr(gate, "_last_read_at", None)  # thirty seconds on: the read is due again
    async with await psycopg.AsyncConnection.connect(settings.data_plane.db_url) as aconn:
        with pytest.raises(_Exited):
            await restore(aconn)


def test_direct_dials_are_not_gated_and_carry_no_process_name(
    monkeypatch: pytest.MonkeyPatch, stored_minimum: int, _gated_process: list[int]
) -> None:
    _set_minimum(_VERSION + 1)
    monkeypatch.setattr(connections, "direct_db_url", _session_direct_url)
    with db.connect(direct=True) as conn:
        params = conn.info.get_parameters()
        assert not params.get("application_name", "").startswith("ava:")
    assert _gated_process == []


def test_an_exempt_process_dials_as_the_cli_without_a_version(
    monkeypatch: pytest.MonkeyPatch, stored_minimum: int, _gated_process: list[int]
) -> None:
    _set_minimum(_VERSION + 1)
    code_version.exempt_from_db_gate()
    monkeypatch.setattr(code_version, "_version", None)
    monkeypatch.setattr(code_version, "get", lambda: pytest.fail("the CLI needs no version"))

    with db.connect() as conn:
        row = conn.execute("SHOW application_name").fetchone()
        assert row is not None and row[0] == "ava:cli"
    assert _gated_process == []


def test_a_runner_login_reads_the_minimum_and_cannot_write_it(
    monkeypatch: pytest.MonkeyPatch, stored_minimum: int, _gated_process: list[int]
) -> None:
    """The gate needs no new grant: the runner group's blanket `SELECT` covers the
    column, and its inability to `UPDATE` the row (only the gateway raises the
    minimum) is unchanged."""
    from tests._containers import runner_projection

    _set_minimum(_VERSION)
    runner_url = runner_projection()
    monkeypatch.setattr(settings.data_plane, "db_url", runner_url)

    with db.connect() as conn:
        assert gate.min_read_due() is False  # this dial read the minimum
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE deployment_state SET min_code_version = 0")
    assert _gated_process == []


# ── the migration ────────────────────────────────────────────────────────────


def test_the_migration_restores_the_column_where_the_baseline_has_it() -> None:
    """With the column dropped, the migration restores it (twice over: it is
    idempotent) with the shape and comment `db/schema.sql` gives a fresh database.
    Run inside one transaction, so the suite database is untouched."""
    from base.paths import repo_root

    (up_file,) = (repo_root() / "migrations").glob("*_min-code-version.sql")
    column = (
        "SELECT data_type, is_nullable, column_default, "
        "col_description('deployment_state'::regclass, ordinal_position::int) "
        "FROM information_schema.columns "
        "WHERE table_name = 'deployment_state' AND column_name = 'min_code_version'"
    )
    with psycopg.connect(settings.data_plane.db_url) as conn:
        try:
            baseline = conn.execute(column).fetchone()
            assert baseline is not None and baseline[:3] == ("bigint", "NO", "0")

            conn.execute("ALTER TABLE deployment_state DROP COLUMN min_code_version")
            assert conn.execute(column).fetchone() is None

            up = cast(LiteralString, up_file.read_text())
            conn.execute(up)
            conn.execute(up)
            assert conn.execute(column).fetchone() == baseline
        finally:
            conn.rollback()
