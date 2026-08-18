"""POST /api/agents/{aid}/pages + DELETE / GET integration tests.

Covers:
- register: new row + idempotent upsert (re-register with same name updates host/port)
  + gateway reverse-proxy URL
- close: open→closed CAS + already-closed → 404
- list: only returns open
- agent terminated: register returns 409
- DB trigger: agents_meta status transitions to terminated → auto cascade-close all
  open pages

Pages are served through the gateway reverse proxy (see test_pages_proxy.py):
url is the gateway's own /api/pages/<id>-<name>/ (composite key; the old
nested /api/agents/<id>/pages/<name>/ path still routes for backwards
compatibility), host:port stays in the registry for the proxy's dialing.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from shared.db import create_agent

_HOST = "127.0.0.1"  # loopback — the single-box posture the SDK registers (audit P1-4: only loopback / the agent's own machine are legal proxy targets)


def _page_rows(conn: psycopg.Connection, agent_id: int) -> list[tuple]:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, port, host, title, serve_dir, closed_at FROM agent_pages "
            "WHERE agent_id = %s ORDER BY id ASC",
            (agent_id,),
        )
        return cur.fetchall()


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
    assert body["url"] == f"http://test-gateway.invalid:8000/api/pages/{aid}-cleanup/"

    db_conn.rollback()  # reset the transaction held by the fixture to get the latest view
    rows = _page_rows(db_conn, aid)  # pyright: ignore[reportUnknownVariableType]
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
    assert resp.json()["url"] == f"http://test-gateway.invalid:8000/api/pages/{aid}-p/"


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
    assert resp.json()["url"] == f"http://testserver/api/pages/{aid}-p/"


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
    assert r2.json()["url"] == f"http://test-gateway.invalid:8000/api/pages/{aid}-p/"

    db_conn.rollback()
    rows = _page_rows(db_conn, aid)  # pyright: ignore[reportUnknownVariableType]
    # Two total rows: r1 now closed, r2 open.
    assert len(rows) == 2  # pyright: ignore[reportUnknownArgumentType]
    closed_row = [r for r in rows if r[5] is not None]  # pyright: ignore[reportUnknownVariableType]
    open_row = [r for r in rows if r[5] is None]  # pyright: ignore[reportUnknownVariableType]
    assert len(closed_row) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert len(open_row) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert open_row[0] == ("p", 8002, "localhost", "v2", None, None)


def test_register_page_missing_host_422() -> None:
    with TestClient(app) as client:
        resp = client.post("/api/agents/1/pages", json={"name": "p", "port": 8001})
    assert resp.status_code == 422


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
    rows = _page_rows(db_conn, aid)  # pyright: ignore[reportUnknownVariableType]
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
        assert body[0]["url"] == f"http://test-gateway.invalid:8000/api/pages/{aid}-a/"
        # Close it — list should be empty.
        client.delete(f"/api/agents/{aid}/pages/a")
        r2 = client.get(f"/api/agents/{aid}/pages")
        assert r2.status_code == 200
        assert r2.json() == []


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
    """DB trigger: agents_meta.status flip to terminated -> all open pages auto closed."""
    # Create two agents, one page each (single-page-per-agent model).
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
        client.post(f"/api/agents/{a2}/pages", json={"name": "y", "port": 8002, "host": _HOST})
    # Terminate both agents.
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (a1,))
        cur.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (a2,))
    db_conn.commit()

    for aid in (a1, a2):
        rows = _page_rows(db_conn, aid)  # pyright: ignore[reportUnknownVariableType]
        assert all(closed_at is not None for (*_rest, closed_at) in rows)  # pyright: ignore[reportUnknownVariableType]


def test_list_open_page_names_returns_open_only(db_conn: psycopg.Connection) -> None:
    """terminate cascade entry: SELECT currently open page names, closed excluded."""
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
    names = list_open_page_names(db_conn, aid)
    assert names == ["y"]  # only y is still open; x was auto-closed


def test_register_page_with_serve_dir(db_conn: psycopg.Connection) -> None:
    """serve_dir（serve()/serve_markdown() 记录的服务目录）随注册透传到行 + 响应。"""
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
    rows = _page_rows(db_conn, aid)  # pyright: ignore[reportUnknownVariableType]
    assert rows == [("report", 8766, _HOST, None, "/data/report", None)]


def test_register_page_upsert_updates_serve_dir(db_conn: psycopg.Connection) -> None:
    """ops.register_page 同名 upsert（UPDATE 分支）：port/title/serve_dir 一起更新，行 id 不变。"""
    from ops.pages import register_page

    aid = create_agent(db_conn)
    r1 = register_page(db_conn, aid, "p", 8001, _HOST, None, serve_dir="/data/a")
    r2 = register_page(db_conn, aid, "p", 8002, "100.64.0.2", "v2", serve_dir="/data/b")
    assert r2.id == r1.id  # UPDATE 而非 INSERT
    assert r2.port == 8002
    assert r2.title == "v2"
    assert r2.serve_dir == "/data/b"
    db_conn.rollback()
    rows = _page_rows(db_conn, aid)  # pyright: ignore[reportUnknownVariableType]
    assert rows == [("p", 8002, "100.64.0.2", "v2", "/data/b", None)]


def test_register_page_without_serve_dir_leaves_null(db_conn: psycopg.Connection) -> None:
    """show() 路径不带 serve_dir → 行里保持 NULL。"""
    aid = create_agent(db_conn)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{aid}/pages", json={"name": "p", "port": 8001, "host": _HOST}
        )
    assert resp.status_code == 201
    assert resp.json()["serve_dir"] is None


def test_cascade_reopen_on_resurrect(db_conn: psycopg.Connection) -> None:
    """DB trigger: status 从 'terminated' 离开（resurrect 的唯一入口）→ terminate 时刻
    cascade 关闭的页面行重开；用户更早主动 close 的历史行不被误重开。"""
    aid = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'running')",
            (aid,),
        )
    db_conn.commit()
    with TestClient(app) as client:
        # 页面 x：用户在 terminate 之前主动 close（历史行，不应被重开）
        client.post(f"/api/agents/{aid}/pages", json={"name": "x", "port": 8001, "host": _HOST})
        client.delete(f"/api/agents/{aid}/pages/x")
        # 页面 y：terminate 时仍 open，会被 cascade 关闭
        client.post(f"/api/agents/{aid}/pages", json={"name": "y", "port": 8002, "host": _HOST})

    # terminate → cascade 关闭 y（x 保持它自己的 closed_at）
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (aid,))
    db_conn.commit()
    db_conn.rollback()
    rows = _page_rows(db_conn, aid)  # pyright: ignore[reportUnknownVariableType]
    assert all(closed_at is not None for (*_rest, closed_at) in rows)  # pyright: ignore[reportUnknownVariableType]

    # resurrect（terminated → running）→ y 重开，x 保持 closed
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status='running' WHERE id=%s", (aid,))
    db_conn.commit()
    db_conn.rollback()
    rows = _page_rows(db_conn, aid)  # pyright: ignore[reportUnknownVariableType]
    by_name = {name: closed_at for name, _port, _host, _title, _sd, closed_at in rows}  # pyright: ignore[reportUnknownVariableType]
    assert by_name["y"] is None
    assert by_name["x"] is not None


def test_cascade_reopen_repeated_terminate_resurrect(db_conn: psycopg.Connection) -> None:
    """两次 terminate/resurrect 循环：每次只重开当次 terminate 关闭的行。"""
    aid = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'running')",
            (aid,),
        )
    db_conn.commit()
    with TestClient(app) as client:
        client.post(f"/api/agents/{aid}/pages", json={"name": "a", "port": 8001, "host": _HOST})
    # 第一轮 terminate → resurrect
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (aid,))
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status='running' WHERE id=%s", (aid,))
    db_conn.commit()
    db_conn.rollback()
    rows = _page_rows(db_conn, aid)  # pyright: ignore[reportUnknownVariableType]
    assert (
        len(rows) == 1 and rows[0][5] is None  # pyright: ignore[reportUnknownArgumentType]
    )  # 重开  # pyright: ignore[reportUnknownArgumentType]

    # 第二轮：重新注册（auto-close 旧行）→ terminate → resurrect → 只重开新行
    with TestClient(app) as client:
        client.post(f"/api/agents/{aid}/pages", json={"name": "b", "port": 8002, "host": _HOST})
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (aid,))
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status='running' WHERE id=%s", (aid,))
    db_conn.commit()
    db_conn.rollback()
    rows = _page_rows(db_conn, aid)  # pyright: ignore[reportUnknownVariableType]
    by_name = {name: closed_at for name, _p, _h, _t, _sd, closed_at in rows}  # pyright: ignore[reportUnknownVariableType]
    assert by_name["b"] is None  # 第二轮重开
    assert by_name["a"] is not None  # 第一轮的旧行（第二轮 terminate 前被 auto-close）不再重开


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
    aid = _agent_with_machine(db_conn, "runner-a", url="http://100.64.0.1:8000", tmp_path=tmp_path)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{aid}/pages",
            json={"name": "p", "port": 8001, "host": "100.64.0.1"},
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
