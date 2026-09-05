"""POST /api/agents/{aid}/pages + DELETE / GET integration tests.

Covers:
- register: new row + idempotent upsert (re-register with same name updates host/port)
  + gateway reverse-proxy URL
- close: open→closed CAS + already-closed → 404
- list: only returns open
- agent terminated: register returns 409
- DB trigger: agent termination cascade-closes agent-owned show() pages while
  leaving daemon-supervised serve() pages open

Pages are served through the gateway reverse proxy (see test_pages_proxy.py):
url is the gateway's own /pages/<id>-<name>/ (composite key), host:port
stays in the registry for the proxy's dialing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from shared.db import create_agent

_HOST = "127.0.0.1"  # loopback — the single-box posture the SDK registers (audit P1-4: only loopback / the agent's own machine are legal proxy targets)


def _page_rows(conn: psycopg.Connection, agent_id: int) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, port, host, title, serve_dir, closed_at FROM agent_pages "
            "WHERE agent_id = %s ORDER BY id ASC",
            (agent_id,),
        )
        return cur.fetchall()


def _page_deadline(
    conn: psycopg.Connection, agent_id: int, name: str
) -> tuple[datetime | None, datetime | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT expires_at, expired_at FROM agent_pages WHERE agent_id = %s AND name = %s",
            (agent_id, name),
        )
        row = cur.fetchone()
    assert row is not None
    return row[0], row[1]


def test_register_page_inserts_open_row(db_conn: psycopg.Connection) -> None:
    aid = create_agent(db_conn)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{aid}/pages",
            json={"name": "cleanup", "port": 8765, "host": _HOST, "title": "Cleanup picker"},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "cleanup"
    assert body["port"] == 8765
    assert body["title"] == "Cleanup picker"
    assert body["closed_at"] is None
    # direct URL straight at the agent's page server, no reverse proxy
    # gateway reverse-proxy URL — host:port never appears
    assert body["url"] == f"http://test-gateway.invalid:8000/pages/{aid}-cleanup/"

    db_conn.rollback()  # reset the transaction held by the fixture to get the latest view
    rows = _page_rows(db_conn, aid)
    assert rows == [("cleanup", 8765, _HOST, "Cleanup picker", None, None)]


def test_register_page_url_uses_gateway_url_var_not_request_host(
    db_conn: psycopg.Connection,
) -> None:
    """Regression: the delivered URL derives from the Gateway URL variable,
    never from the request's own Host.

    The SDK dials the gateway at its local loopback URL on a single box
    (AVA_GATEWAY_URL=localhost), so absolutizing against the request Host
    produced `http://localhost:8000/...` links that are unreachable from the
    user's other devices. Whatever address the caller dialed, the URL is the
    configured Gateway URL — the same for every client.
    """
    aid = create_agent(db_conn)
    # Simulate the SDK caller: dials the gateway at localhost...
    with TestClient(app, base_url="http://localhost:8000") as client:
        resp = client.post(
            f"/api/agents/{aid}/pages",
            json={"name": "p", "port": 8001, "host": _HOST},
        )
    assert resp.status_code == 201
    # ...yet the URL is the configured Gateway URL variable, not localhost.
    assert resp.json()["url"] == f"http://test-gateway.invalid:8000/pages/{aid}-p/"


def test_register_page_url_falls_back_to_request_base_without_gateway_url(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Gateway URL configured (dev/tests) -> request base, so callers still
    get a usable absolute URL instead of a bare path."""
    from shared.config import settings

    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    aid = create_agent(db_conn)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{aid}/pages",
            json={"name": "p", "port": 8001, "host": _HOST},
        )
    assert resp.status_code == 201
    assert resp.json()["url"] == f"http://testserver/pages/{aid}-p/"


def test_register_page_same_name_auto_closes_then_creates(db_conn: psycopg.Connection) -> None:
    """Single page per agent: re-registering auto-closes the old page, creates a new row."""
    aid = create_agent(db_conn)
    with TestClient(app) as client:
        r1 = client.post(
            f"/api/agents/{aid}/pages", json={"name": "p", "port": 8001, "host": _HOST}
        )
        r2 = client.post(
            f"/api/agents/{aid}/pages",
            json={"name": "p", "port": 8002, "host": "localhost", "title": "v2"},
        )
    assert r1.status_code == 201
    assert r2.status_code == 201
    # Old page is auto-closed, new row gets a new id.
    assert r1.json()["id"] != r2.json()["id"]
    assert r1.json()["closed_at"] is None  # r1 was open at the time
    assert r2.json()["port"] == 8002
    assert r2.json()["title"] == "v2"
    assert r2.json()["url"] == f"http://test-gateway.invalid:8000/pages/{aid}-p/"

    db_conn.rollback()
    rows = _page_rows(db_conn, aid)
    # Two total rows: r1 now closed, r2 open.
    assert len(rows) == 2  # pyright: ignore[reportUnknownArgumentType]
    closed_row = [r for r in rows if r[5] is not None]
    open_row = [r for r in rows if r[5] is None]
    assert len(closed_row) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert len(open_row) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert open_row[0] == ("p", 8002, "localhost", "v2", None, None)


def test_register_page_missing_host_422() -> None:
    with TestClient(app) as client:
        resp = client.post("/api/agents/1/pages", json={"name": "p", "port": 8001})
    assert resp.status_code == 422


def test_register_page_rejects_zero_ttl() -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/api/agents/1/pages",
            json={"name": "p", "port": 8001, "host": _HOST, "ttl_seconds": 0},
        )
    assert resp.status_code == 422


def test_register_page_sets_explicit_expiry(db_conn: psycopg.Connection) -> None:
    aid = create_agent(db_conn)
    before = datetime.now(UTC)
    with TestClient(app) as client:
        response = client.post(
            f"/api/agents/{aid}/pages",
            json={"name": "timed", "port": 8001, "host": _HOST, "ttl_seconds": 120},
        )
    assert response.status_code == 201
    db_conn.rollback()
    expires_at, expired_at = _page_deadline(db_conn, aid, "timed")
    assert expires_at is not None
    assert (
        before + timedelta(seconds=119) <= expires_at <= datetime.now(UTC) + timedelta(seconds=121)
    )
    assert expired_at is None


def test_register_page_applies_gateway_default_expiry(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.config import settings

    monkeypatch.setattr(settings.daemon, "page_default_ttl_seconds", 300.0)
    aid = create_agent(db_conn)
    before = datetime.now(UTC)
    with TestClient(app) as client:
        response = client.post(
            f"/api/agents/{aid}/pages",
            json={"name": "defaulted", "port": 8001, "host": _HOST},
        )
    assert response.status_code == 201
    db_conn.rollback()
    expires_at, _ = _page_deadline(db_conn, aid, "defaulted")
    assert expires_at is not None
    assert (
        before + timedelta(seconds=299) <= expires_at <= datetime.now(UTC) + timedelta(seconds=301)
    )


def test_register_page_revives_expired_row_and_resets_deadline(
    db_conn: psycopg.Connection,
) -> None:
    from ops.pages import register_page

    aid = create_agent(db_conn)
    original = register_page(db_conn, aid, "revive", 8001, _HOST, None, ttl_seconds=30)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_pages SET expired_at = now() WHERE id = %s",
            (original.id,),
        )
    db_conn.commit()

    before = datetime.now(UTC)
    revived = register_page(db_conn, aid, "revive", 8002, _HOST, "Again", ttl_seconds=240)

    assert revived.id == original.id
    assert revived.port == 8002
    expires_at, expired_at = _page_deadline(db_conn, aid, "revive")
    assert expires_at is not None
    assert (
        before + timedelta(seconds=239) <= expires_at <= datetime.now(UTC) + timedelta(seconds=241)
    )
    assert expired_at is None


def test_register_page_invalid_name_422() -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/api/agents/1/pages", json={"name": "bad/name", "port": 8001, "host": _HOST}
        )
    assert resp.status_code == 422


def test_register_page_404_when_agent_missing() -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/api/agents/99999/pages", json={"name": "p", "port": 8001, "host": _HOST}
        )
    assert resp.status_code == 404


def test_register_page_409_when_agent_terminated(db_conn: psycopg.Connection) -> None:
    aid = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'terminated')",
            (aid,),
        )
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{aid}/pages", json={"name": "p", "port": 8001, "host": _HOST}
        )
    assert resp.status_code == 409


def test_close_page_cas_open_to_closed(db_conn: psycopg.Connection) -> None:
    aid = create_agent(db_conn)
    with TestClient(app) as client:
        client.post(f"/api/agents/{aid}/pages", json={"name": "p", "port": 8001, "host": _HOST})
        r = client.delete(f"/api/agents/{aid}/pages/p")
    assert r.status_code == 200
    assert r.json()["closed_at"] is not None

    db_conn.rollback()
    rows = _page_rows(db_conn, aid)
    assert len(rows) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert rows[0][5] is not None  # closed_at


def test_close_page_already_closed_404(db_conn: psycopg.Connection) -> None:
    aid = create_agent(db_conn)
    with TestClient(app) as client:
        client.post(f"/api/agents/{aid}/pages", json={"name": "p", "port": 8001, "host": _HOST})
        r1 = client.delete(f"/api/agents/{aid}/pages/p")
        r2 = client.delete(f"/api/agents/{aid}/pages/p")
    assert r1.status_code == 200
    assert r2.status_code == 404


def test_list_pages_returns_only_open(db_conn: psycopg.Connection) -> None:
    """Single page per agent: GET returns the one open page (or empty)."""
    aid = create_agent(db_conn)
    with TestClient(app) as client:
        # Register page "a" — this is the only open page.
        client.post(f"/api/agents/{aid}/pages", json={"name": "a", "port": 8001, "host": _HOST})
        r = client.get(f"/api/agents/{aid}/pages")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        assert body[0]["name"] == "a"
        assert body[0]["url"] == f"http://test-gateway.invalid:8000/pages/{aid}-a/"
        # Close it — list should be empty.
        client.delete(f"/api/agents/{aid}/pages/a")
        r2 = client.get(f"/api/agents/{aid}/pages")
        assert r2.status_code == 200
        assert r2.json() == []


def test_list_pages_excludes_expired_rows(db_conn: psycopg.Connection) -> None:
    aid = create_agent(db_conn)
    with TestClient(app) as client:
        response = client.post(
            f"/api/agents/{aid}/pages",
            json={"name": "expired", "port": 8001, "host": _HOST},
        )
        assert response.status_code == 201
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_pages SET expired_at = now() WHERE agent_id = %s AND name = 'expired'",
            (aid,),
        )
    db_conn.commit()
    with TestClient(app) as client:
        response = client.get(f"/api/agents/{aid}/pages")
        fleet_response = client.get("/api/pages")
    assert response.status_code == 200
    assert response.json() == []
    assert (aid, "expired") not in {(row["agent_id"], row["name"]) for row in fleet_response.json()}


def test_list_all_pages_spans_agents_open_only(db_conn: psycopg.Connection) -> None:
    """GET /api/pages (fleet view): every agent's open pages in one fetch, closed excluded."""
    a1 = create_agent(db_conn)
    a2 = create_agent(db_conn)
    a3 = create_agent(db_conn)
    with TestClient(app) as client:
        client.post(f"/api/agents/{a1}/pages", json={"name": "p1", "port": 8001, "host": _HOST})
        client.post(f"/api/agents/{a2}/pages", json={"name": "p2", "port": 8002, "host": _HOST})
        # a3 has a page that we'll close.
        client.post(f"/api/agents/{a3}/pages", json={"name": "gone", "port": 8003, "host": _HOST})
        client.delete(f"/api/agents/{a3}/pages/gone")
        r = client.get("/api/pages")
    assert r.status_code == 200
    body = r.json()
    by_agent = {(row["agent_id"], row["name"]) for row in body}
    assert (a1, "p1") in by_agent
    assert (a2, "p2") in by_agent
    assert (a3, "gone") not in by_agent  # closed excluded


def test_cascade_close_on_agent_terminate(db_conn: psycopg.Connection) -> None:
    """Agent termination closes show() pages but keeps serve() pages listed."""
    # Create two agents: one owns its server and the other is daemon-supervised.
    a1 = create_agent(db_conn)
    a2 = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'running')",
            (a1,),
        )
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'running')",
            (a2,),
        )
    db_conn.commit()
    with TestClient(app) as client:
        client.post(f"/api/agents/{a1}/pages", json={"name": "x", "port": 8001, "host": _HOST})
        client.post(
            f"/api/agents/{a2}/pages",
            json={"name": "y", "port": 8002, "host": _HOST, "serve_dir": "/data/y"},
        )
    # Terminate both agents.
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (a1,))
        cur.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (a2,))
    db_conn.commit()

    assert _page_rows(db_conn, a1)[0][5] is not None
    assert _page_rows(db_conn, a2)[0][5] is None
    with TestClient(app) as client:
        response = client.get(f"/api/agents/{a2}/pages")
    assert response.status_code == 200
    assert [page["name"] for page in response.json()] == ["y"]


def test_list_open_page_names_returns_open_only(db_conn: psycopg.Connection) -> None:
    """Terminate events list only show() pages that the cascade will close."""
    from ops.pages import list_open_page_names

    aid = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'running')",
            (aid,),
        )
    db_conn.commit()
    with TestClient(app) as client:
        # Register one page — it's the only open one.
        client.post(f"/api/agents/{aid}/pages", json={"name": "x", "port": 8001, "host": _HOST})
        # Register another — this auto-closes "x" (single page per agent).
        client.post(f"/api/agents/{aid}/pages", json={"name": "y", "port": 8002, "host": _HOST})
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_pages (agent_id, name, port, host, serve_dir) "
            "VALUES (%s, 'serve', 8003, %s, '/data/serve')",
            (aid, _HOST),
        )
    db_conn.commit()
    names = list_open_page_names(db_conn, aid)
    assert names == ["y"]  # x is closed; serve is daemon-supervised


def test_register_page_with_serve_dir(db_conn: psycopg.Connection) -> None:
    """serve_dir (the directory serve() records) passes through registration to row and response."""
    aid = create_agent(db_conn)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{aid}/pages",
            json={"name": "report", "port": 8766, "host": _HOST, "serve_dir": "/data/report"},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["serve_dir"] == "/data/report"
    assert body["title"] is None

    db_conn.rollback()
    rows = _page_rows(db_conn, aid)
    assert rows == [("report", 8766, _HOST, None, "/data/report", None)]


def test_register_page_upsert_updates_serve_dir(db_conn: psycopg.Connection) -> None:
    """ops.register_page \u540c\u540d upsert\uff08UPDATE \u5206\u652f\uff09\uff1aport/title/serve_dir \u4e00\u8d77\u66f4\u65b0\uff0c\u884c id \u4e0d\u53d8\u3002"""
    from ops.pages import register_page

    aid = create_agent(db_conn)
    r1 = register_page(db_conn, aid, "p", 8001, _HOST, None, serve_dir="/data/a")
    r2 = register_page(db_conn, aid, "p", 8002, "10.0.0.2", "v2", serve_dir="/data/b")
    assert r2.id == r1.id  # UPDATE, not INSERT
    assert r2.port == 8002
    assert r2.title == "v2"
    assert r2.serve_dir == "/data/b"
    db_conn.rollback()
    rows = _page_rows(db_conn, aid)
    assert rows == [("p", 8002, "10.0.0.2", "v2", "/data/b", None)]


def test_register_page_without_serve_dir_leaves_null(db_conn: psycopg.Connection) -> None:
    """show() \u8def\u5f84\u4e0d\u5e26 serve_dir → \u884c\u91cc\u4fdd\u6301 NULL\u3002"""
    aid = create_agent(db_conn)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{aid}/pages", json={"name": "p", "port": 8001, "host": _HOST}
        )
    assert resp.status_code == 201
    assert resp.json()["serve_dir"] is None


def test_cascade_reopen_on_resurrect(db_conn: psycopg.Connection) -> None:
    """Resurrect reopens only show() rows the terminate cascade closed."""
    aid = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'running')",
            (aid,),
        )
    db_conn.commit()
    with TestClient(app) as client:
        # x: explicitly closed before termination and must remain historical.
        client.post(f"/api/agents/{aid}/pages", json={"name": "x", "port": 8001, "host": _HOST})
        client.delete(f"/api/agents/{aid}/pages/x")
        # y: an open show() page, so termination closes it.
        client.post(f"/api/agents/{aid}/pages", json={"name": "y", "port": 8002, "host": _HOST})
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_pages (agent_id, name, port, host, serve_dir) "
            "VALUES (%s, 'serve', 8003, %s, '/data/serve')",
            (aid, _HOST),
        )
    db_conn.commit()

    # Termination closes y and leaves both the old x row and serve row unchanged.
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (aid,))
    db_conn.commit()
    db_conn.rollback()
    rows = _page_rows(db_conn, aid)
    by_name = {name: closed_at for name, _port, _host, _title, _sd, closed_at in rows}
    assert by_name["x"] is not None
    assert by_name["y"] is not None
    assert by_name["serve"] is None

    # Resurrect reopens y only; x remains closed and serve never needed reopening.
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status='running' WHERE id=%s", (aid,))
    db_conn.commit()
    db_conn.rollback()
    rows = _page_rows(db_conn, aid)
    by_name = {name: closed_at for name, _port, _host, _title, _sd, closed_at in rows}
    assert by_name["y"] is None
    assert by_name["x"] is not None
    assert by_name["serve"] is None


def test_cascade_reopen_repeated_terminate_resurrect(db_conn: psycopg.Connection) -> None:
    """Each terminate/resurrect cycle reopens only its closed show() rows."""
    aid = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'running')",
            (aid,),
        )
    db_conn.commit()
    with TestClient(app) as client:
        client.post(f"/api/agents/{aid}/pages", json={"name": "a", "port": 8001, "host": _HOST})
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_pages (agent_id, name, port, host, serve_dir) "
            "VALUES (%s, 'serve', 8003, %s, '/data/serve')",
            (aid, _HOST),
        )
    db_conn.commit()
    # First terminate → resurrect cycle.
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (aid,))
    db_conn.commit()
    rows = _page_rows(db_conn, aid)
    by_name = {name: closed_at for name, _p, _h, _t, _sd, closed_at in rows}
    assert by_name["a"] is not None
    assert by_name["serve"] is None
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status='running' WHERE id=%s", (aid,))
    db_conn.commit()
    db_conn.rollback()
    rows = _page_rows(db_conn, aid)
    by_name = {name: closed_at for name, _p, _h, _t, _sd, closed_at in rows}
    assert by_name["a"] is None
    assert by_name["serve"] is None

    # Second cycle: the old show() page is closed before termination, the new
    # show() page is cascade-closed/reopened, and the daemon page stays open.
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_pages SET closed_at = now() WHERE agent_id = %s AND name = 'a'",
            (aid,),
        )
        cur.execute(
            "INSERT INTO agent_pages (agent_id, name, port, host) VALUES (%s, 'b', 8002, %s)",
            (aid, _HOST),
        )
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (aid,))
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status='running' WHERE id=%s", (aid,))
    db_conn.commit()
    db_conn.rollback()
    rows = _page_rows(db_conn, aid)
    by_name = {name: closed_at for name, _p, _h, _t, _sd, closed_at in rows}
    assert by_name["b"] is None  # reopened by the second cycle
    assert by_name["a"] is not None  # closed before the second termination
    assert by_name["serve"] is None


# ── audit round-2 P1-4: page proxy SSRF guard ─────────────────────────


def _agent_with_machine(
    db_conn: psycopg.Connection,
    machine: str,
    url: str | None = None,
    tmp_path: Path | None = None,
) -> int:
    """Create an agent whose home machine is `machine`, optionally with a
    registered machine_units dial URL (what the cluster itself uses to reach
    that machine)."""
    aid = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, status, machine) VALUES (%s, 'running', %s)",
            (aid, machine),
        )
        if url is not None:
            cur.execute(
                "INSERT INTO machine_units (machine_name, home, serve_agent_runner, url) "
                "VALUES (%s, %s, true, %s)",
                (machine, str(tmp_path) if tmp_path else f"home-{machine}", url),
            )
    db_conn.commit()
    return aid


def test_register_page_rejects_foreign_host(db_conn: psycopg.Connection) -> None:
    """SSRF guard (audit P1-4): a page host that is neither loopback nor the
    agent's own machine is refused — the reverse proxy must not become a
    probe/access primitive for arbitrary addresses (the cluster's Postgres /
    Redis / IM bridge / any other host on the network)."""
    aid = _agent_with_machine(db_conn, "test-mc")
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{aid}/pages",
            json={"name": "p", "port": 8001, "host": "10.0.0.99"},
        )
    assert resp.status_code == 400
    assert "refusing to proxy" in resp.json()["detail"]


def test_register_page_allows_agents_own_machine_host(
    db_conn: psycopg.Connection,
    tmp_path: Path,
) -> None:
    """A split-deployment agent registers its machine's advertised address —
    the gateway must keep proxying to it (machine_units.url is the authority
    for what the cluster reaches that machine at)."""
    aid = _agent_with_machine(db_conn, "runner-a", url="http://10.0.0.1:8000", tmp_path=tmp_path)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{aid}/pages",
            json={"name": "p", "port": 8001, "host": "10.0.0.1"},
        )
    assert resp.status_code == 201, resp.text


def test_register_page_allows_machine_name_host(db_conn: psycopg.Connection) -> None:
    """The agent's machine NAME is a legal host — it resolves to the machine
    itself (a single box's agents register loopback anyway; the name is the
    split-deployment spelling of "my own machine")."""
    aid = _agent_with_machine(db_conn, "test-mc")
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{aid}/pages",
            json={"name": "p", "port": 8001, "host": "test-mc"},
        )
    assert resp.status_code == 201, resp.text


def test_register_page_rejects_privileged_port(db_conn: psycopg.Connection) -> None:
    """Privileged ports are the system's own services — never a valid page
    server, even on loopback (defense in depth under the host check)."""
    aid = _agent_with_machine(db_conn, "test-mc")
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{aid}/pages",
            json={"name": "p", "port": 80, "host": "127.0.0.1"},
        )
    assert resp.status_code == 400
    assert "privileged" in resp.json()["detail"]
