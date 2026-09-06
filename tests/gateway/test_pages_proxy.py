"""Gateway reverse-proxy tests for agent page servers.

The new page data path: browser -> gateway -> agent page server. The
registered PageRow.url is the gateway's own /pages/<id>-<name>/, auth-gated
like every API route; the gateway forwards GETs to the page server's
registered host:port. Covers:
- proxy serves page content (root + nested path + query string)
- trailing-slash canonicalization
- 404: unknown page / closed page / missing agent
- path-traversal rejection (encoded dot segments, backslashes)
- 502: page server unreachable
- auth required (middleware on: 401 without credentials)
"""

from __future__ import annotations

import http.server
import socket
import socketserver
import threading
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from gateway.app import app
from gateway.routers import pages as pages_router
from shared import config
from shared.db import create_agent
from shared.pages_copy import PAGE_LANGUAGE_DEFAULT, PAGE_SERVER_DOWN_BODY, PAGE_SERVER_TIMEOUT_BODY

_SECRET = "test-cluster-secret"  # noqa: S105 — test fixture


def test_machine_dial_host_cache_evicts_least_recently_used_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page-proxy allowlist cache remains bounded while preserving recent hosts."""

    class _Cursor:
        machine = ""

        def execute(self, _query: str, params: tuple[str]) -> None:
            self.machine = params[0]

        def fetchall(self) -> list[tuple[str]]:
            return [(f"http://{self.machine}.internal:8106",)]

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class _Connection:
        def cursor(self) -> _Cursor:
            return _Cursor()

        def __enter__(self) -> _Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class _Pool:
        def connection(self) -> _Connection:
            return _Connection()

    pages_router.reset_page_host_cache_for_tests()
    monkeypatch.setattr(pages_router, "_PAGE_HOST_CACHE_MAX_ENTRIES", 2)
    pool = _Pool()
    try:
        pages_router._machine_dial_hosts(pool, "first")  # pyright: ignore[reportArgumentType]
        pages_router._machine_dial_hosts(pool, "second")  # pyright: ignore[reportArgumentType]
        pages_router._machine_dial_hosts(pool, "first")  # pyright: ignore[reportArgumentType]
        pages_router._machine_dial_hosts(pool, "third")  # pyright: ignore[reportArgumentType]

        assert list(pages_router._page_host_cache) == ["first", "third"]
    finally:
        pages_router.reset_page_host_cache_for_tests()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class _SSEHandler(http.server.BaseHTTPRequestHandler):
    """Streams two SSE events with a delay between them — proves the proxy
    forwards a chunked, non-buffered response (content-type preserved, both
    events arrive)."""

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b"data: first\n\n")
        self.wfile.flush()
        import time

        time.sleep(0.2)
        self.wfile.write(b"data: second\n\n")
        self.wfile.flush()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def page_server(tmp_path: Path) -> Iterator[int]:
    """A real static page server (what the page-server daemon launches), serving
    tmp_path on 127.0.0.1. Returns its port."""
    (tmp_path / "index.html").write_text("<h1>hello</h1>", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "file.txt").write_text("nested", encoding="utf-8")
    # A file OUTSIDE the served directory — the traversal tests prove the
    # proxy cannot reach it. Distinctive content so an assertion can tell
    # "file served" apart from the 400 detail echoing the rejected path.
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("TOP-SECRET-OUTSIDE-CONTENT", encoding="utf-8")

    # SimpleHTTPRequestHandler takes directory as a constructor arg (a class
    # attribute is ignored — __init__ falls back to cwd).
    handler = lambda *args, **kwargs: _QuietHandler(  # noqa: E731 — factory for ThreadingTCPServer
        *args,  # pyright: ignore[reportUnknownArgumentType]
        directory=str(tmp_path),
        **kwargs,
    )
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)  # pyright: ignore[reportUnknownArgumentType]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _register(client: TestClient, agent_id: int, name: str, port: int) -> None:
    resp = client.post(
        f"/api/agents/{agent_id}/pages",
        json={"name": name, "port": port, "host": "127.0.0.1"},
    )
    assert resp.status_code == 201


def test_proxy_serves_page_content(db_conn, page_server: int) -> None:
    """GET through the gateway returns the page server's content — root,
    nested path, and query string all work."""
    aid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{aid}/pages",
            json={"name": "p", "port": page_server, "host": "127.0.0.1"},
        )
    assert resp.status_code == 201
    # The registered URL is the gateway's own — no host:port leak.
    assert resp.json()["url"] == f"http://test-gateway.invalid:8000/pages/{aid}-p/"

    with TestClient(app) as client:
        root = client.get(f"/pages/{aid}-p/")
        assert root.status_code == 200
        assert root.headers["content-type"].startswith("text/html")
        assert "<h1>hello</h1>" in root.text

        nested = client.get(f"/pages/{aid}-p/sub/file.txt")
        assert nested.status_code == 200
        assert nested.text == "nested"

        with_qs = client.get(f"/pages/{aid}-p/?v=2")
        assert with_qs.status_code == 200
        assert "<h1>hello</h1>" in with_qs.text


def test_proxy_redirects_root_without_trailing_slash(db_conn, page_server: int) -> None:
    """/pages/{name} (no slash) redirects to /pages/{name}/ so relative asset
    links inside the page resolve against the page directory."""
    aid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    with TestClient(app) as client:
        _register(client, aid, "p", page_server)
        resp = client.get(f"/pages/{aid}-p", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == f"/pages/{aid}-p/"


def test_proxy_404_unknown_page(db_conn) -> None:
    """A never-registered page name is a 404, not a proxy attempt."""
    aid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    with TestClient(app) as client:
        resp = client.get(f"/pages/{aid}-never/")
    assert resp.status_code == 404


def test_proxy_404_closed_page(db_conn, page_server: int) -> None:
    """close() removes the reverse-proxy route: a closed page 404s."""
    aid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    with TestClient(app) as client:
        _register(client, aid, "p", page_server)
        client.delete(f"/api/agents/{aid}/pages/p")
        resp = client.get(f"/pages/{aid}-p/")
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/pages/{agent_id}-p/",
    ],
)
def test_proxy_410_expired_page_zh_default(
    db_conn: psycopg.Connection, page_server: int, path: str
) -> None:
    """No user_settings row -> the page copy falls back to the zh default
    (page copy follows display.language, user ruling 2026-08-13)."""
    aid = create_agent(db_conn)
    with TestClient(app) as client:
        _register(client, aid, "p", page_server)
        db_conn.rollback()
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_pages SET expired_at = now() WHERE agent_id = %s AND name = 'p'",
                (aid,),
            )
        db_conn.commit()
        response = client.get(path.format(agent_id=aid))

    assert response.status_code == 410
    assert response.headers["content-type"].startswith("text/html")
    assert "\u9875\u9762\u5df2\u8fc7\u671f" in response.text  # zh title
    assert (
        "\u9875\u9762\u5df2\u8fc7\u671f\uff0c\u8bf7\u8ba9 agent \u91cd\u65b0 serve" in response.text
    )
    assert "Page expired" not in response.text


def test_proxy_410_expired_page_en_when_display_language_en(
    db_conn: psycopg.Connection, page_server: int
) -> None:
    """display.language=en selects the English page-expired copy through the
    full proxy path (the zh default is covered above)."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_settings (key, value) VALUES ('display.language', %s)",
            (Jsonb("en"),),
        )
    aid = create_agent(db_conn)
    with TestClient(app) as client:
        _register(client, aid, "p", page_server)
        db_conn.rollback()
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_pages SET expired_at = now() WHERE agent_id = %s AND name = 'p'",
                (aid,),
            )
        db_conn.commit()
        response = client.get(f"/pages/{aid}-p/")

    assert response.status_code == 410
    assert "Page expired" in response.text
    assert "Page expired - ask the agent to serve it again" in response.text


def test_proxy_404_missing_agent(db_conn, page_server: int) -> None:
    with TestClient(app) as client:
        resp = client.get("/pages/99999-p/")
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "bad_path",
    [
        "%2e%2e/outside-secret.txt",  # ../
        "sub/%2e%2e/%2e%2e/outside-secret.txt",  # ../../
        "%2e%2e%2foutside-secret.txt",  # encoded ../ without slash split
        "%2e%2e%5coutside-secret.txt",  # ..\ (Windows separator)
        "sub/..%5coutside-secret.txt",
    ],
)
def test_proxy_rejects_traversal(db_conn, page_server: int, bad_path: str) -> None:
    """Encoded dot-segments and backslashes are rejected with 400 — the
    gateway never forwards a path that could escape the page directory."""
    aid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    with TestClient(app) as client:
        _register(client, aid, "p", page_server)
        resp = client.get(f"/pages/{aid}-p/{bad_path}")
    assert resp.status_code == 400
    # The outside file's content never crossed the proxy (the 400 detail may
    # echo the rejected path, but not the file itself).
    assert "TOP-SECRET-OUTSIDE-CONTENT" not in resp.text


def test_proxy_502_when_page_server_unreachable(db_conn) -> None:
    """A registered page whose server is down surfaces as 502 with the
    friendly zh copy (the default display language) — never the raw host:port."""
    aid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    # Grab a free port and release it — nothing will be listening there.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{aid}/pages",
            json={"name": "p", "port": free_port, "host": "127.0.0.1"},
        )
        assert resp.status_code == 201
        resp = client.get(f"/pages/{aid}-p/")
    assert resp.status_code == 502
    # Friendly copy (zh default) replaces the raw host:port detail.
    assert resp.json()["detail"] == PAGE_SERVER_DOWN_BODY[PAGE_LANGUAGE_DEFAULT]
    assert "127.0.0.1" not in resp.json()["detail"]


def test_proxy_502_friendly_copy_en_when_display_language_en(
    db_conn: psycopg.Connection,
) -> None:
    """display.language=en selects the English 502 copy through the full proxy path."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_settings (key, value) VALUES ('display.language', %s)",
            (Jsonb("en"),),
        )
    aid = create_agent(db_conn)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{aid}/pages",
            json={"name": "p", "port": free_port, "host": "127.0.0.1"},
        )
        assert resp.status_code == 201
        resp = client.get(f"/pages/{aid}-p/")
    assert resp.status_code == 502
    assert (
        resp.json()["detail"]
        == "The page server just restarted or is no longer available - ask the agent that created it to republish"
    )


def test_proxy_502_emits_structured_log_line(db_conn) -> None:
    """The 502 branch writes one structured log line carrying trace_id,
    agent_id, page name, host:port, and the exception type + message — the
    traceability the report asked for (task #2212)."""
    from typing import Any as _Any

    from loguru import logger as _loguru

    records: list[_Any] = []

    def _capture(message: _Any) -> None:
        records.append(message.record)

    aid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()
    sink_id = _loguru.add(_capture, level="WARNING")
    try:
        with TestClient(app) as client:
            client.post(
                f"/api/agents/{aid}/pages",
                json={"name": "p", "port": free_port, "host": "127.0.0.1"},
            )
            client.get(f"/pages/{aid}-p/")
    finally:
        _loguru.remove(sink_id)

    hits = [r for r in records if r["extra"].get("event") == "page_proxy_502"]
    assert len(hits) == 1
    extra: dict[str, object] = hits[0]["extra"]
    assert extra["agent_id"] == aid
    assert extra["page"] == "p"
    assert extra["host"] == "127.0.0.1"
    assert extra["port"] == free_port
    assert extra["trace_id"]
    assert extra["exc_type"]  # e.g. ConnectError
    assert extra["exc_message"]


def test_proxy_504_timeout_friendly_copy_and_log(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dial timeout surfaces as 504 with friendly copy (never the raw
    host:port) and one structured log line carrying the timeout exception —
    the 504 branch's traceability mirrors the 502 branch (task #2212)."""
    from typing import Any as _Any

    import httpx
    from loguru import logger as _loguru

    async def _send_times_out(
        self: object, request: object, *args: object, **kwargs: object
    ) -> None:
        raise httpx.TimeoutException("connect timed out")

    records: list[_Any] = []

    def _capture(message: _Any) -> None:
        records.append(message.record)

    aid = create_agent(db_conn)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()
    sink_id = _loguru.add(_capture, level="WARNING")
    monkeypatch.setattr(httpx.AsyncClient, "send", _send_times_out)
    try:
        with TestClient(app) as client:
            client.post(
                f"/api/agents/{aid}/pages",
                json={"name": "p", "port": free_port, "host": "127.0.0.1"},
            )
            resp = client.get(f"/pages/{aid}-p/")
    finally:
        _loguru.remove(sink_id)

    assert resp.status_code == 504
    # Friendly copy (zh default) replaces the raw host:port detail.
    assert resp.json()["detail"] == PAGE_SERVER_TIMEOUT_BODY[PAGE_LANGUAGE_DEFAULT]
    assert "127.0.0.1" not in resp.json()["detail"]

    hits = [r for r in records if r["extra"].get("event") == "page_proxy_504"]
    assert len(hits) == 1
    extra: dict[str, object] = hits[0]["extra"]
    assert extra["agent_id"] == aid
    assert extra["page"] == "p"
    assert extra["host"] == "127.0.0.1"
    assert extra["port"] == free_port
    assert extra["trace_id"]
    assert extra["exc_type"] == "TimeoutException"
    assert extra["exc_message"]


def test_proxy_502_language_lookup_failure_falls_back_to_default(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DB failure while reading the copy language must not turn the 502
    into a 500 — the default-language copy is served instead (QA nit A)."""

    def _boom(_conn: object) -> str:
        raise RuntimeError("pg down")

    monkeypatch.setattr("gateway.routers.pages.display_language", _boom)
    aid = create_agent(db_conn)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()
    with TestClient(app) as client:
        client.post(
            f"/api/agents/{aid}/pages",
            json={"name": "p", "port": free_port, "host": "127.0.0.1"},
        )
        resp = client.get(f"/pages/{aid}-p/")
    assert resp.status_code == 502
    assert resp.json()["detail"] == PAGE_SERVER_DOWN_BODY[PAGE_LANGUAGE_DEFAULT]


def test_proxy_revalidates_nonloopback_registry_target_before_dialing(
    db_conn: psycopg.Connection,
) -> None:
    """Rows inserted outside registration cannot turn the proxy into SSRF."""
    pages_router.reset_page_host_cache_for_tests()
    aid = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET machine = 'runner-a' WHERE id = %s", (aid,))
        cur.execute(
            "INSERT INTO agent_pages (agent_id, name, host, port) VALUES (%s, 'unsafe', %s, 8000)",
            (aid, "foreign.internal"),
        )
    db_conn.commit()

    with TestClient(app) as client:
        response = client.get(f"/pages/{aid}-unsafe/")

    assert response.status_code == 403
    assert "refusing to proxy" in response.json()["detail"]


def test_proxy_streams_sse_chunked_content(db_conn) -> None:
    """A chunked upstream (SSE) passes through with its content-type and all
    events — the proxy streams instead of buffering."""
    sse_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SSEHandler)
    thread = threading.Thread(target=sse_server.serve_forever, daemon=True)
    thread.start()
    try:
        port = sse_server.server_address[1]
        aid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        with TestClient(app) as client:
            _register(client, aid, "sse", port)
            resp = client.get(f"/pages/{aid}-sse/")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/event-stream"
        assert "data: first" in resp.text
        assert "data: second" in resp.text
    finally:
        sse_server.shutdown()
        sse_server.server_close()


def test_proxy_requires_auth(monkeypatch: pytest.MonkeyPatch, db_conn) -> None:
    """The reverse proxy is auth-gated like every other API route: no
    credentials -> 401; a valid bearer passes the middleware (404 here
    because the page was never registered, proving it reached the route)."""
    monkeypatch.setattr(config.settings.gateway, "auth_middleware_enabled", True)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    aid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    with TestClient(app) as client:
        no_auth = client.get(f"/pages/{aid}-p/")
        assert no_auth.status_code == 401

        with_auth = client.get(
            f"/pages/{aid}-p/",
            headers={"Authorization": f"Bearer {_SECRET}"},
        )
        assert with_auth.status_code == 404


# --- New URL shape: /pages/<agent_id>-<name>/ ---


def test_new_url_parses_page_key_with_dashes_in_name() -> None:
    """Name charset allows dashes; the composite key splits on the FIRST
    dash only (agent_id is numeric), so `12-my-page` parses to
    (12, "my-page"). The proxy then serves that page."""
    from gateway.routers.pages import _parse_page_key

    assert _parse_page_key("12-my-page") == (12, "my-page")
    assert _parse_page_key("7-page") == (7, "page")
    assert _parse_page_key("nope") is None
    assert _parse_page_key("12-") == (12, "")
    assert _parse_page_key("12") is None


def test_old_page_urls_no_longer_route(db_conn, page_server) -> None:
    """One-time switch, no backwards compatibility: the previous
    /api/pages/<id>-<name>/ and /api/agents/<id>/pages/<name>/ URLs are gone
    (404), only /pages/<id>-<name>/ serves content."""
    port = page_server
    aid = create_agent(db_conn)  # pyright: ignore[reportUnknownArgumentType]
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{aid}/pages",
            json={"name": "p", "port": port, "host": "127.0.0.1"},
        )
        assert resp.status_code == 201
        old = client.get(f"/api/pages/{aid}-p/")
        assert old.status_code == 404
        # the old nested path only keeps its DELETE close API — GET is gone
        legacy = client.get(f"/api/agents/{aid}/pages/p/")
        assert legacy.status_code == 405
        # the new URL still serves
        current = client.get(f"/pages/{aid}-p/")
        assert current.status_code == 200
        assert current.text == "<h1>hello</h1>"
