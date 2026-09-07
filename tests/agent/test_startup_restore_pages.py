"""`reconcile_open_pages` — probe every open page's server and restore it.

Runs at agent boot, on each heartbeat, and on the periodic page-reconcile
host scans, as the catch-all for page-server
death after daemon supervision inside persistent page sessions. Per open row:
server alive -> keep; dead + serve_dir -> re-serve; dead + no serve_dir ->
close the row so the dead link stops showing as open (PageClosed event).

These tests stub the DB pool and the probe to pin the orchestration, plus
the probe itself against a real local HTTP server.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from agent.startup import _page_server_alive, reconcile_open_pages
from agent.state import AgentState


class _FakeCursor:
    def __init__(self, rows: list[tuple], fetchone_row: tuple[object, ...] | None = None) -> None:
        self._rows = rows  # pyright: ignore[reportUnknownMemberType]
        self._fetchone_row = fetchone_row
        self.executed: list[tuple[str, tuple[object, ...] | None]] = []

    async def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        self.executed.append((sql, params))  # pyright: ignore[reportUnknownMemberType]

    async def fetchall(self) -> list[tuple]:
        return self._rows  # pyright: ignore[reportUnknownMemberType]

    async def fetchone(self) -> tuple[object, ...] | None:
        # The page-recovery notice dedupe check ("already told within 6h?").
        return self._fetchone_row

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeConn:
    def __init__(self, rows: list[tuple], fetchone_row: tuple[object, ...] | None = None) -> None:
        self._cursor = _FakeCursor(rows, fetchone_row)

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def transaction(self) -> _FakeConn:
        return self

    async def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        await self._cursor.execute(sql, params)

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakePool:
    def __init__(self, rows: list[tuple], fetchone_row: tuple[object, ...] | None = None) -> None:
        self._conn = _FakeConn(rows, fetchone_row)

    def connection(self, *, timeout: float | None = None) -> _FakeConn:
        # Mirrors AsyncConnectionPool.connection(): returns a (sync) context
        # object whose __aenter__ is awaited by `async with`.
        return self._conn

    @property
    def executed(self) -> list[tuple[str, tuple[object, ...] | None]]:
        return self._conn._cursor.executed  # pyright: ignore[reportUnknownMemberType]


def _notice_inserts(pool: _FakePool) -> list[tuple[object, ...]]:
    """(agent_id, content) params of the re-serve-notice INSERTs recorded by
    the fake pool — typed so the assertions below stay pyright-clean."""
    rows = [p for sql, p in pool.executed if "INSERT INTO inbound_messages" in sql]  # pyright: ignore[reportUnknownMemberType]
    return [r for r in rows if r is not None]


def _row(
    name: str,
    port: int,
    host: str = "127.0.0.1",
    title: str | None = None,
    serve_dir: str | None = "/data/x",
) -> tuple:
    return (name, port, host, title, serve_dir)


def _fake_serve(served: list[tuple]) -> object:
    def _f(*args, **kwargs) -> object:
        served.append((args, kwargs))  # pyright: ignore[reportUnknownMemberType]
        return object()

    return _f


async def _run_loop_briefly(task: Any, seconds: float) -> None:
    """Run the reconcile-loop task for `seconds`, then cancel it (the loop
    never exits on its own) and swallow its CancelledError."""
    import asyncio
    import contextlib

    await asyncio.sleep(seconds)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_reserves_dead_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Server probe fails + serve_dir set → re-serve the recorded dir/name/port/title."""
    served: list[tuple] = []
    monkeypatch.setattr("agent.startup._page_server_alive", lambda _h, _p: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("ava.ui.serve", _fake_serve(served))

    pool = _FakePool([_row("report", 18001, title="Report", serve_dir="/data/report")])
    await reconcile_open_pages(pool, 7)  # type: ignore[arg-type]

    assert served == [((("/data/report", "report", 18001, "Report"), {}))]
    # no UPDATE closed (serve_dir exists -> re-serve instead of closing)
    assert not any("UPDATE agent_pages" in sql for sql, _ in pool.executed)  # pyright: ignore[reportUnknownMemberType]


async def test_keeps_alive_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Server probe succeeds → no re-serve (the row stays as-is)."""
    served: list[tuple] = []
    monkeypatch.setattr("agent.startup._page_server_alive", lambda _h, _p: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("ava.ui.serve", _fake_serve(served))

    pool = _FakePool([_row("report", 18001, serve_dir="/data/report")])
    await reconcile_open_pages(pool, 7)  # type: ignore[arg-type]

    assert served == []


async def test_closes_dead_page_without_serve_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dead server + no serve_dir (show() pages / pre-serve_dir rows) → the row
    is closed (CAS UPDATE) so the dead link stops showing as open, and a
    PageClosed event is emitted."""
    served: list[tuple] = []
    events: list[str] = []

    class _Pub:
        def emit(self, payload: str) -> None:
            events.append(payload)

    monkeypatch.setattr("agent.startup._page_server_alive", lambda _h, _p: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("ava.ui.serve", _fake_serve(served))

    pool = _FakePool(
        [
            _row("report", 18001, serve_dir="/data/report"),
            _row("plain", 18002, serve_dir=None),
        ]
    )
    await reconcile_open_pages(pool, 7, event_publisher=_Pub())  # type: ignore[arg-type]

    # report re-served; plain closed + PageClosed + one re-serve notice
    assert served == [((("/data/report", "report", 18001, None), {}))]
    updates = [p for sql, p in pool.executed if "UPDATE agent_pages" in sql]  # pyright: ignore[reportUnknownMemberType]
    assert updates == [(7, "plain")]
    inserts = _notice_inserts(pool)
    assert len(inserts) == 1
    agent_id_param, content = inserts[0]
    assert agent_id_param == 7
    assert isinstance(content, str)
    assert content.startswith("Page recovery:")
    assert "'plain'" in content
    assert "show()" in content
    assert len(events) == 1
    assert "page_closed" in events[0]
    assert '"name":"plain"' in events[0]


async def test_keeps_alive_page_without_serve_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Alive server + no serve_dir → kept as-is, no close, no event."""
    events: list[str] = []

    class _Pub:
        def emit(self, payload: str) -> None:
            events.append(payload)

    monkeypatch.setattr("agent.startup._page_server_alive", lambda _h, _p: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("ava.ui.serve", lambda *a, **k: pytest.fail("must not serve"))  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]

    pool = _FakePool([_row("plain", 18002, serve_dir=None)])
    await reconcile_open_pages(pool, 7, event_publisher=_Pub())  # type: ignore[arg-type]

    assert not any("UPDATE agent_pages" in sql for sql, _ in pool.executed)  # pyright: ignore[reportUnknownMemberType]
    assert events == []


async def test_closes_multiple_dead_show_pages_with_one_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Several dead show() rows of one agent merge into ONE notice listing
    them all — the heartbeat must not nag once per page."""
    served: list[tuple] = []
    monkeypatch.setattr("agent.startup._page_server_alive", lambda _h, _p: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("ava.ui.serve", _fake_serve(served))

    pool = _FakePool(
        [
            _row("a", 18001, serve_dir=None),
            _row("b", 18002, serve_dir=None),
        ]
    )
    await reconcile_open_pages(pool, 7)  # type: ignore[arg-type]

    updates = [p for sql, p in pool.executed if "UPDATE agent_pages" in sql]  # pyright: ignore[reportUnknownMemberType]
    assert updates == [(7, "a"), (7, "b")]
    inserts = _notice_inserts(pool)
    assert len(inserts) == 1
    _agent_id_param, content = inserts[0]
    assert isinstance(content, str)
    assert "'a'" in content and "'b'" in content


async def test_dead_show_page_recent_notice_skips_notify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent already told within the 6h window is not told again (the
    heartbeat runs every 5 min — without the window a persistent failure
    would nag on every pass). The row still closes."""
    served: list[tuple] = []
    monkeypatch.setattr("agent.startup._page_server_alive", lambda _h, _p: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("ava.ui.serve", _fake_serve(served))

    pool = _FakePool([_row("plain", 18002, serve_dir=None)], fetchone_row=((1,),))
    await reconcile_open_pages(pool, 7)  # type: ignore[arg-type]

    updates = [p for sql, p in pool.executed if "UPDATE agent_pages" in sql]  # pyright: ignore[reportUnknownMemberType]
    assert updates == [(7, "plain")]
    assert not any("INSERT INTO inbound_messages" in sql for sql, _p in pool.executed)  # pyright: ignore[reportUnknownMemberType]


async def test_swallows_query_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB down must not block the agent — query failure is logged and skipped."""

    class _BoomPool:
        async def connection(self) -> None:
            raise RuntimeError("pg down")

    monkeypatch.setattr("ava.ui.serve", lambda *a, **k: pytest.fail("must not serve"))  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
    await reconcile_open_pages(_BoomPool(), 7)  # type: ignore[arg-type]  # must not raise


def test_page_server_alive_ok() -> None:
    """A live server answering /health 200 reads as alive."""
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format: str, *args: object) -> None:
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        assert _page_server_alive("127.0.0.1", srv.server_port) is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_page_server_alive_refused() -> None:
    """A dead server (connection refused) reads as dead — no exception escapes."""
    assert _page_server_alive("127.0.0.1", 1) is False


async def test_heartbeat_runs_page_reconcile(monkeypatch: pytest.MonkeyPatch) -> None:
    """HEARTBEAT handler probes the agent's pages (Task #973: live agents must
    self-heal pages killed by a cluster rollout — boot recovery never runs)."""
    from agent.db import ClaimedInbound
    from agent.graph._claim import _BatchState, _handle_heartbeat

    calls: list[tuple[object, int, object | None]] = []

    async def _fake_reconcile(pool, agent_id, *, event_publisher=None):
        calls.append((pool, agent_id, event_publisher))  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr("agent.startup.reconcile_open_pages", _fake_reconcile)  # pyright: ignore[reportUnknownArgumentType]

    class _Ctx:
        ops_pool = object()
        event_publisher = object()

    item = ClaimedInbound(id=1, agent_id=7, content="check-in", kind="heartbeat", source="system")
    st = _BatchState()
    state = AgentState(messages=[HumanMessage(content="hi")])
    await _handle_heartbeat(_Ctx(), 7, item, st, state)  # type: ignore[arg-type]

    assert len(calls) == 1
    _pool, agent_id, publisher = calls[0]
    assert agent_id == 7
    assert publisher is _Ctx.event_publisher
    # heartbeat system note still appended as usual (breaker closed)
    assert len(st.new_msgs) == 1


async def test_page_reconcile_loop_runs_periodically(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task #2257: the heartbeat-independent periodic scan — a busy agent
    (no heartbeats) still gets its pages reconciled on a fixed cadence."""
    import asyncio

    from agent.startup import page_reconcile_loop

    calls: list[tuple[object, int, object | None]] = []

    async def _fake_reconcile(pool, agent_id, *, event_publisher=None):
        calls.append((pool, agent_id, event_publisher))  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr("agent.startup.reconcile_open_pages", _fake_reconcile)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("agent.startup._last_reconcile_at", {7: 0.0})  # pyright: ignore[reportUnknownArgumentType]

    task = asyncio.create_task(
        page_reconcile_loop(object(), 7, interval_s=0.01)  # type: ignore[arg-type]
    )
    await _run_loop_briefly(task, 0.06)

    assert len(calls) >= 2
    _pool, agent_id, publisher = calls[0]
    assert agent_id == 7
    assert publisher is None


async def test_page_reconcile_loop_survives_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising pass must not kill the loop (N1, #1284 QA review) — like the
    lease renewer, any failure is logged and the loop retries next interval
    instead of silently losing the busy-agent backstop."""
    import asyncio

    from agent.startup import page_reconcile_loop

    calls = 0

    async def _boom(pool, agent_id, *, event_publisher=None):
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    monkeypatch.setattr("agent.startup.reconcile_open_pages", _boom)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("agent.startup._last_reconcile_at", {7: 0.0})  # pyright: ignore[reportUnknownArgumentType]

    task = asyncio.create_task(
        page_reconcile_loop(object(), 7, interval_s=0.01)  # type: ignore[arg-type]
    )
    # Cancels cleanly -> the task survived; had a pass killed it, the await
    # would re-raise the exception instead.
    await _run_loop_briefly(task, 0.05)
    assert calls >= 2


async def test_page_reconcile_loop_skips_recent_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pass another path (boot / heartbeat) already ran within the interval
    suppresses the periodic pass — the combined cadence stays ~one scan per
    interval, so an idle agent is not probed twice."""
    import asyncio
    import time

    from agent.startup import page_reconcile_loop

    monkeypatch.setattr(
        "agent.startup.reconcile_open_pages",
        lambda *a, **k: pytest.fail("periodic pass must be skipped"),  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
    )
    # A heartbeat pass just ran (monotonic now, plus margin so the sleep
    # overshoot can never push the diff past the interval) — the loop must
    # not scan.
    monkeypatch.setattr("agent.startup._last_reconcile_at", {7: time.monotonic() + 60})  # pyright: ignore[reportUnknownArgumentType]

    task = asyncio.create_task(
        page_reconcile_loop(object(), 7, interval_s=0.01)  # type: ignore[arg-type]
    )
    await _run_loop_briefly(task, 0.05)


async def test_open_pages_query_filters_closed_and_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reconcile pass queries ONLY open rows — a closed/expired page can
    never appear in a scan. Regression pin for the 2026-09-01 investigation:
    the two page_restore_alive events for merge-orchestration-research were
    logged while that row was still OPEN (closed_at set hours later) — the
    fresh-rowset filter is what keeps closed rows out of every pass."""
    served: list[tuple] = []
    monkeypatch.setattr("agent.startup._page_server_alive", lambda _h, _p: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("ava.ui.serve", _fake_serve(served))

    pool = _FakePool([_row("report", 18001, serve_dir="/data/report")])
    await reconcile_open_pages(pool, 7)  # type: ignore[arg-type]

    selects = [sql for sql, _p in pool.executed if sql.startswith("SELECT")]  # pyright: ignore[reportUnknownMemberType]
    assert len(selects) == 1
    assert "closed_at IS NULL" in selects[0]
    assert "expired_at IS NULL" in selects[0]
    assert served == []


async def test_reconcile_all_open_pages_scans_each_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task #2260: the hosted daemon's periodic scan reconciles every agent
    that has open pages, one pass per agent."""
    from agent.startup import reconcile_all_open_pages

    reconciled: list[int] = []

    async def _fake_reconcile(pool, agent_id, *, event_publisher=None):
        reconciled.append(agent_id)  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr("agent.startup.reconcile_open_pages", _fake_reconcile)  # pyright: ignore[reportUnknownArgumentType]
    # No recent pass for any agent -> every listed agent is scanned.
    monkeypatch.setattr("agent.startup._last_reconcile_at", {})

    pool = _FakePool([(3,), (7,), (11,)])
    await reconcile_all_open_pages(pool, interval_s=0.01)  # type: ignore[arg-type]

    assert reconciled == [3, 7, 11]
    # The listing query filters to open rows only.
    selects = [sql for sql, _p in pool.executed if sql.startswith("SELECT")]  # pyright: ignore[reportUnknownMemberType]
    assert len(selects) == 1
    assert "DISTINCT agent_id" in selects[0]
    assert "closed_at IS NULL" in selects[0]


async def test_reconcile_all_open_pages_skips_recently_scanned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent another path (its heartbeat scan) already reconciled within
    the interval is skipped — per-agent throttle keys, so one agent's recent
    scan never suppresses another's."""
    import time

    from agent.startup import reconcile_all_open_pages

    reconciled: list[int] = []

    async def _fake_reconcile(pool, agent_id, *, event_publisher=None):
        reconciled.append(agent_id)  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr("agent.startup.reconcile_open_pages", _fake_reconcile)  # pyright: ignore[reportUnknownArgumentType]
    # Agent 7 was scanned moments ago; 3 and 11 were not.
    monkeypatch.setattr("agent.startup._last_reconcile_at", {7: time.monotonic() + 60})

    pool = _FakePool([(3,), (7,), (11,)])
    await reconcile_all_open_pages(pool, interval_s=0.01)  # type: ignore[arg-type]

    assert reconciled == [3, 11]


async def test_reconcile_all_open_pages_swallows_query_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB down must not kill the hosted daemon's scan — the listing query
    failure is logged and the pass ends."""
    from agent.startup import reconcile_all_open_pages

    class _BoomPool:
        async def connection(self) -> None:
            raise RuntimeError("pg down")

    monkeypatch.setattr(
        "agent.startup.reconcile_open_pages",
        lambda *a, **kw: pytest.fail("must not scan when the listing query fails"),  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
    )
    await reconcile_all_open_pages(_BoomPool(), interval_s=0.01)  # type: ignore[arg-type]  # must not raise


class _SequencedPool:
    """Serves one result set per connection, in order: connection 1 answers
    the listing query, each later connection is one agent's open-page query."""

    def __init__(self, results: list[list[tuple]]) -> None:
        self._results: list[list[tuple]] = results
        self._i = 0

    def connection(self) -> _SequencedConn:
        return _SequencedConn(self)


class _SequencedConn:
    def __init__(self, pool: _SequencedPool) -> None:
        self._pool = pool

    def cursor(self) -> _SequencedCur:
        return _SequencedCur(self._pool)

    async def __aenter__(self) -> _SequencedConn:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _SequencedCur:
    def __init__(self, pool: _SequencedPool) -> None:
        self._pool = pool
        self._rows: list[tuple[object, ...]] = []

    async def execute(self, sql: str, params: tuple | None = None) -> None:
        self._rows = self._pool._results[self._pool._i]  # pyright: ignore[reportUnknownMemberType]
        self._pool._i += 1  # pyright: ignore[reportUnknownMemberType]

    async def fetchall(self) -> list[tuple]:
        return self._rows

    async def fetchone(self) -> tuple[object, ...] | None:
        return None

    async def __aenter__(self) -> _SequencedCur:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


async def test_reconcile_all_open_pages_reserves_with_turn_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1 (#1312 adversarial review): the hosted daemon process has no agent
    identity, and the re-serve arm reads ava._boot.agent_id() — the pass must
    bind the agent's turn identity so serve() registers for the right agent.
    Runs the REAL reconcile path (not a fake) to pin the mechanism."""
    import ava._boot
    from agent.startup import reconcile_all_open_pages

    identities: list[int | None] = []

    def _fake_serve(*args, **kwargs) -> object:
        identities.append(ava._boot.agent_id())  # pyright: ignore[reportUnknownArgumentType]
        return object()

    monkeypatch.setattr("ava.ui.serve", _fake_serve)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("agent.startup._page_server_alive", lambda _h, _p: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("agent.startup._last_reconcile_at", {})

    pool = _SequencedPool(
        [
            [(7,)],  # listing: agent 7 has open pages
            [_row("report", 18001, serve_dir="/data/report")],  # agent 7's rows
        ]
    )
    await reconcile_all_open_pages(pool, interval_s=0.01)  # type: ignore[arg-type]

    # serve() saw the bound identity, not None — without the bind the
    # registration POST would target /agents/None and fail (the P1).
    assert identities == [7]


async def test_reconcile_stamps_own_agent_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pass stamps ITS agent's throttle key only — hosted: one agent's
    scan must never suppress another's (write side of the per-agent dict)."""
    import agent.startup as startup_mod

    monkeypatch.setattr("agent.startup._page_server_alive", lambda _h, _p: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("agent.startup._last_reconcile_at", {})

    pool = _FakePool([_row("report", 18001, serve_dir="/data/report")])
    await reconcile_open_pages(pool, 7)  # type: ignore[arg-type]

    assert 7 in startup_mod._last_reconcile_at
    assert 8 not in startup_mod._last_reconcile_at
