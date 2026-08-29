"""`reconcile_open_pages` — probe every open page's server and restore it.

Runs at agent boot and on each heartbeat, as the catch-all for page-server
death after daemon supervision inside persistent page sessions. Per open row:
server alive -> keep; dead + serve_dir -> re-serve; dead + no serve_dir ->
close the row so the dead link stops showing as open (PageClosed event).

These tests stub the DB pool and the probe to pin the orchestration, plus
the probe itself against a real local HTTP server.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from agent.startup import _page_server_alive, reconcile_open_pages
from agent.state import AgentState


class _FakeCursor:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows  # pyright: ignore[reportUnknownMemberType]
        self.executed: list[tuple[str, tuple | None]] = []

    async def execute(self, sql: str, params: tuple | None = None) -> None:
        self.executed.append((sql, params))  # pyright: ignore[reportUnknownMemberType]

    async def fetchall(self) -> list[tuple]:
        return self._rows  # pyright: ignore[reportUnknownMemberType]

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeConn:
    def __init__(self, rows: list[tuple]) -> None:
        self._cursor = _FakeCursor(rows)

    def cursor(self) -> _FakeCursor:
        return self._cursor

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakePool:
    def __init__(self, rows: list[tuple]) -> None:
        self._conn = _FakeConn(rows)

    def connection(self) -> _FakeConn:
        # Mirrors AsyncConnectionPool.connection(): returns a (sync) context
        # object whose __aenter__ is awaited by `async with`.
        return self._conn

    @property
    def executed(self) -> list[tuple[str, tuple | None]]:
        return self._conn._cursor.executed  # pyright: ignore[reportUnknownMemberType]


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

    # report re-served; plain closed + PageClosed
    assert served == [((("/data/report", "report", 18001, None), {}))]
    updates = [p for sql, p in pool.executed if "UPDATE agent_pages" in sql]  # pyright: ignore[reportUnknownMemberType]
    assert updates == [(7, "plain")]
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
