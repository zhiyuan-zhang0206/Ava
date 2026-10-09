"""Page-server daemon tests for persistent agent page shells."""

from __future__ import annotations

import os
import secrets
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import services.agent_runner.page_server.daemon as psd
from base.cluster.machine import reset_identity, set_identity
from base.config.service_read import ConfigAuthority
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from tests.fixtures.units import spawn_agent

_HOST = "127.0.0.1"


@pytest.fixture(autouse=True)
def _identity() -> Iterator[None]:
    set_identity(host=_HOST)
    yield
    reset_identity()


class _FakeShellBackend:
    def __init__(self) -> None:
        self.sessions: set[str] = set()
        self.new_calls: list[tuple[str, str, Path, dict[str, str]]] = []
        self.sent: list[tuple[str, str]] = []
        self.keys: list[tuple[str, tuple[str, ...]]] = []
        self.killed: list[str] = []
        self.new_result = True
        self.supports_send = True
        self.send_error: Exception | None = None

    def has_session(self, name: str) -> bool:
        return name in self.sessions

    def new_session(
        self, name: str, command: str, cwd: Path, *, env: dict[str, str], **_kwargs: object
    ) -> bool:
        self.new_calls.append((name, command, cwd, env))
        if self.new_result:
            self.sessions.add(name)
        return self.new_result

    def send(self, name: str, text: str) -> None:
        if not self.supports_send:
            raise NotImplementedError
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((name, text))

    def send_keys(self, name: str, *keys: str) -> None:
        self.keys.append((name, keys))

    def kill_session(self, name: str, **_kwargs: object) -> tuple[bool, str]:
        self.killed.append(name)
        self.sessions.discard(name)
        return True, "forced"


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> _FakeShellBackend:
    fake = _FakeShellBackend()
    monkeypatch.setattr(psd, "get_shell_backend", lambda: fake)
    monkeypatch.setattr(psd, "_page_server_occupants", dict)
    monkeypatch.setattr(
        psd,
        "_page_session_shell_pids",
        lambda _wanted, _live=None: {},  # pyright: ignore[reportUnknownArgumentType]
    )
    return fake


@pytest.fixture
def sync_pool(db_conn: psycopg.Connection) -> Iterator[ConnectionPool]:
    from base.config import settings

    pool: ConnectionPool = ConnectionPool(
        settings.data_plane.db_url, min_size=1, max_size=2, open=True
    )
    yield pool
    pool.close()


def _insert_page_row(
    conn: psycopg.Connection,
    agent_id: int,
    name: str,
    port: int,
    serve_dir: Path,
    *,
    token: str | None = None,
    session: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_pages (agent_id, name, port, host, serve_dir, server_token, session_name) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (agent_id, name, port, _HOST, str(serve_dir), token, session),
        )
    conn.commit()


def _close_row(conn: psycopg.Connection, agent_id: int, name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_pages SET closed_at = now() WHERE agent_id = %s AND name = %s",
            (agent_id, name),
        )
    conn.commit()


def _reconcile(
    pool: ConnectionPool,
    managed: dict[tuple[int, str], psd._ServerHandle],
    backoff: dict[tuple[int, str], float],
    degraded: dict[tuple[int, str], psd._DegradedServeDir],
) -> None:
    psd._reconcile_once(pool, managed, backoff, degraded, _HOST, EventBus.from_settings())


def _token_and_session(conn: psycopg.Connection, agent_id: int, name: str) -> tuple[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT server_token, session_name FROM agent_pages WHERE agent_id = %s AND name = %s",
            (agent_id, name),
        )
        row = cur.fetchone()
    assert row is not None and row[0] is not None and row[1] is not None
    return str(row[0]), str(row[1])


def test_open_row_creates_persistent_page_session_and_persists_token(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    backend: _FakeShellBackend,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    agent_id = spawn_agent(catalog=model_catalog, authority=config_authority)
    _insert_page_row(db_conn, agent_id, "My_Page", 12001, tmp_path)
    managed: dict[tuple[int, str], psd._ServerHandle] = {}
    backoff: dict[tuple[int, str], float] = {}
    degraded: dict[tuple[int, str], psd._DegradedServeDir] = {}

    _reconcile(sync_pool, managed, backoff, degraded)

    token, page_session = _token_and_session(db_conn, agent_id, "My_Page")
    assert page_session == f"ava-agent-{agent_id}-shell-0-page-my-page"
    assert set(managed) == {(agent_id, "My_Page")}
    assert backend.new_calls == [
        (
            page_session,
            f"{sys.executable} -m services.agent_runner.page_server.server --port 12001 --host {_HOST} --dir {tmp_path}",
            tmp_path,
            {**os.environ, "PAGE_SERVER_TOKEN": token},
        )
    ]

    _reconcile(sync_pool, managed, backoff, degraded)
    assert _token_and_session(db_conn, agent_id, "My_Page") == (token, page_session)
    assert len(backend.new_calls) == 1


def test_new_row_session_created_before_slow_housekeeping(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    backend: _FakeShellBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """A freshly registered row's session is created BEFORE the pass's slow
    housekeeping (the process scan) — serve()'s wait is one poll, not one
    full pass. The re-scan after creation must keep the new session alive
    through the rest of the pass (a stale scan would reap it as dead)."""
    from types import SimpleNamespace

    agent_id = spawn_agent(catalog=model_catalog, authority=config_authority)
    key = (agent_id, "fast-lane")
    _insert_page_row(db_conn, agent_id, key[1], 12030, tmp_path)
    events: list[str] = []

    def _occupants() -> dict[int, tuple[int, str | None]]:
        events.append("occupants")
        return {}

    monkeypatch.setattr(psd, "_page_server_occupants", _occupants)
    # The pty record scan mirrors the fake backend's session set: the session
    # created in the fast path must appear in the post-creation re-scan.
    monkeypatch.setattr(
        psd,
        "_live_session_records",
        lambda _b: {name: SimpleNamespace(pid=12345) for name in backend.sessions},  # pyright: ignore[reportUnknownArgumentType]
    )
    orig_new = backend.new_session

    def _new(name: str, command: str, cwd: Path, *, env: dict[str, str], **_kw: object) -> bool:
        events.append("create")
        return orig_new(name, command, cwd, env=env, **_kw)

    backend.new_session = _new
    managed: dict[tuple[int, str], psd._ServerHandle] = {}

    _reconcile(sync_pool, managed, {}, {})

    assert events[0] == "create"
    assert "occupants" in events
    assert set(managed) == {key}
    assert backend.killed == []


def test_managed_row_liveness_uses_record_scan_not_backend_round_trip(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    backend: _FakeShellBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """Managed-row liveness comes from the pass's in-process pty record scan,
    not the backend's per-name has_session — a subprocess round-trip (~0.25s)
    per row that stretched a pass to tens of seconds."""
    from types import SimpleNamespace

    agent_id = spawn_agent(catalog=model_catalog, authority=config_authority)
    key = (agent_id, "scan-live")
    page_session = f"ava-agent-{agent_id}-shell-9-page-scan-live"
    _insert_page_row(
        db_conn,
        agent_id,
        key[1],
        12031,
        tmp_path,
        token=secrets.token_hex(16),
        session=page_session,
    )
    monkeypatch.setattr(
        psd,
        "_live_session_records",
        lambda _b: {page_session: SimpleNamespace(pid=12345)},  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(psd, "_server_is_healthy", lambda *_args: True)  # pyright: ignore[reportUnknownArgumentType]
    calls: list[str] = []
    orig_has = backend.has_session

    def _has(name: str) -> bool:
        calls.append(name)
        return orig_has(name)

    backend.has_session = _has
    managed: dict[tuple[int, str], psd._ServerHandle] = {}

    _reconcile(sync_pool, managed, {}, {})

    assert set(managed) == {key}
    assert backend.new_calls == []
    assert calls == []


def test_healthy_page_is_not_resent(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    backend: _FakeShellBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    agent_id = spawn_agent(catalog=model_catalog, authority=config_authority)
    page_session = f"ava-agent-{agent_id}-shell-3-page-live"
    backend.sessions.add(page_session)
    _insert_page_row(
        db_conn,
        agent_id,
        "live",
        12002,
        tmp_path,
        token=secrets.token_hex(16),
        session=page_session,
    )
    monkeypatch.setattr(psd, "_server_is_healthy", lambda *_args: True)  # pyright: ignore[reportUnknownArgumentType]
    managed: dict[tuple[int, str], psd._ServerHandle] = {}

    _reconcile(sync_pool, managed, {}, {})

    assert set(managed) == {(agent_id, "live")}
    assert backend.new_calls == []
    assert backend.sent == []


def test_crashed_server_is_relaunched_in_same_session(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    backend: _FakeShellBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    agent_id = spawn_agent(catalog=model_catalog, authority=config_authority)
    page_session = f"ava-agent-{agent_id}-shell-3-page-crashed"
    backend.sessions.add(page_session)
    _insert_page_row(
        db_conn,
        agent_id,
        "crashed",
        12003,
        tmp_path,
        token=secrets.token_hex(16),
        session=page_session,
    )
    monkeypatch.setattr(psd, "_server_is_healthy", lambda *_args: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(psd, "_probe_port", lambda *_args: None)  # pyright: ignore[reportUnknownArgumentType]
    managed: dict[tuple[int, str], psd._ServerHandle] = {}

    _reconcile(sync_pool, managed, {}, {})

    assert backend.sent == [
        (
            page_session,
            f"{sys.executable} -m services.agent_runner.page_server.server --port 12003 --host {_HOST} --dir {tmp_path}",
        )
    ]
    assert backend.keys == [(page_session, ("Enter",))]
    assert backend.new_calls == []


def test_wedged_session_relaunch_failure_kills_and_recreates_the_session(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    backend: _FakeShellBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """A session whose shell cannot run the server command — its host gone
    or wedged while the record still reads alive — is torn down and rebuilt
    fresh instead of backing off against the dead transport forever
    (task #2670: the page-server ghost record class)."""
    agent_id = spawn_agent(catalog=model_catalog, authority=config_authority)
    page_session = f"ava-agent-{agent_id}-shell-3-page-wedged"
    backend.sessions.add(page_session)
    backend.send_error = RuntimeError("pty session host is not answering")
    _insert_page_row(
        db_conn,
        agent_id,
        "wedged",
        12016,
        tmp_path,
        token=secrets.token_hex(16),
        session=page_session,
    )
    monkeypatch.setattr(psd, "_server_is_healthy", lambda *_args: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(psd, "_probe_port", lambda *_args: None)  # pyright: ignore[reportUnknownArgumentType]
    managed: dict[tuple[int, str], psd._ServerHandle] = {}
    backoff: dict[tuple[int, str], float] = {}

    _reconcile(sync_pool, managed, backoff, {})

    assert backend.killed == [page_session], "the wedged session must be torn down"
    assert [call[0] for call in backend.new_calls] == [page_session], (
        "the torn-down session must be recreated fresh"
    )
    assert set(managed) == {(agent_id, "wedged")}
    assert backoff == {}, "relaunch failure must not wedge the row in a backoff loop"


def test_windows_style_backend_recreates_the_session_when_it_cannot_send(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    backend: _FakeShellBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    agent_id = spawn_agent(catalog=model_catalog, authority=config_authority)
    page_session = f"ava-agent-{agent_id}-shell-3-page-windows"
    backend.sessions.add(page_session)
    backend.supports_send = False
    _insert_page_row(
        db_conn,
        agent_id,
        "windows",
        12015,
        tmp_path,
        token=secrets.token_hex(16),
        session=page_session,
    )
    monkeypatch.setattr(psd, "_server_is_healthy", lambda *_args: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(psd, "_probe_port", lambda *_args: None)  # pyright: ignore[reportUnknownArgumentType]

    _reconcile(sync_pool, {}, {}, {})

    assert backend.killed == [page_session]
    assert [call[0] for call in backend.new_calls] == [page_session]


def test_stale_server_in_its_page_session_replaces_that_session(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    backend: _FakeShellBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    agent_id = spawn_agent(catalog=model_catalog, authority=config_authority)
    key = (agent_id, "stale")
    page_session = f"ava-agent-{agent_id}-shell-3-page-stale"
    backend.sessions.add(page_session)
    _insert_page_row(
        db_conn,
        agent_id,
        "stale",
        12004,
        tmp_path,
        token=secrets.token_hex(16),
        session=page_session,
    )
    monkeypatch.setattr(psd, "_server_is_healthy", lambda *_args: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(psd, "_probe_port", lambda *_args: "ok:old-token")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(psd, "_page_server_occupants", lambda: {12004: (5001, str(psd.ava_home()))})
    monkeypatch.setattr(psd, "_page_session_owner", lambda pid, _pids: key if pid == 5001 else None)  # pyright: ignore[reportUnknownArgumentType]
    managed: dict[tuple[int, str], psd._ServerHandle] = {}

    _reconcile(sync_pool, managed, {}, {})

    assert backend.killed == [page_session]
    assert managed == {}


def test_foreign_port_occupant_is_left_alone_and_backed_off(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    backend: _FakeShellBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    agent_id = spawn_agent(catalog=model_catalog, authority=config_authority)
    page_session = f"ava-agent-{agent_id}-shell-3-page-foreign"
    backend.sessions.add(page_session)
    _insert_page_row(
        db_conn,
        agent_id,
        "foreign",
        12005,
        tmp_path,
        token=secrets.token_hex(16),
        session=page_session,
    )
    monkeypatch.setattr(psd, "_server_is_healthy", lambda *_args: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(psd, "_probe_port", lambda *_args: "wrong")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(psd, "_page_server_occupants", lambda: {12005: (5002, None)})
    managed: dict[tuple[int, str], psd._ServerHandle] = {}
    backoff: dict[tuple[int, str], float] = {}

    _reconcile(sync_pool, managed, backoff, {})

    assert backend.killed == []
    assert backend.sent == []
    assert backoff[(agent_id, "foreign")] > time.monotonic()


def test_closed_row_kills_its_page_session(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    backend: _FakeShellBackend,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    agent_id = spawn_agent(catalog=model_catalog, authority=config_authority)
    _insert_page_row(db_conn, agent_id, "closed", 12006, tmp_path)
    managed: dict[tuple[int, str], psd._ServerHandle] = {}

    _reconcile(sync_pool, managed, {}, {})
    page_session = managed[(agent_id, "closed")].session_name
    _close_row(db_conn, agent_id, "closed")
    _reconcile(sync_pool, managed, {}, {})

    assert backend.killed == [page_session]
    assert managed == {}


def test_stale_snapshot_never_creates_session_for_closed_row(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    backend: _FakeShellBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """The pass snapshot can be seconds stale (process scans, session spawns
    happen after the row read). A row closed after the snapshot must not get a
    session created for it — the next pass would reap the fresh session as an
    orphan, the user-visible serve 502 race (rows 1643/1667 incidents)."""
    agent_id = spawn_agent(catalog=model_catalog, authority=config_authority)
    key = (agent_id, "stale-race")
    _insert_page_row(db_conn, agent_id, key[1], 12020, tmp_path)
    stale = psd._open_rows(sync_pool, _HOST)[0]
    _close_row(db_conn, agent_id, key[1])
    monkeypatch.setattr(psd, "_open_rows", lambda _pool, _host: [stale])  # pyright: ignore[reportUnknownArgumentType]
    managed: dict[tuple[int, str], psd._ServerHandle] = {}

    _reconcile(sync_pool, managed, {}, {})

    assert backend.new_calls == []
    assert backend.killed == []
    assert managed == {}
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT session_name, server_token FROM agent_pages WHERE agent_id = %s AND name = %s",
            (agent_id, key[1]),
        )
        row = cur.fetchone()
        assert row is not None and row[0] is None
        # No token is minted for a closed row (the mint is deferred until
        # after the liveness re-check, never evaluated eagerly).
        assert row[1] is None


def test_stale_snapshot_skips_row_re_registered_mid_pass(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    backend: _FakeShellBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """A row re-registered (port/serve_dir changed) after the snapshot must
    not be created from the obsolete state either — the new registration is
    adopted on the next pass instead."""
    agent_id = spawn_agent(catalog=model_catalog, authority=config_authority)
    key = (agent_id, "re-registered")
    _insert_page_row(db_conn, agent_id, key[1], 12021, tmp_path)
    stale = psd._open_rows(sync_pool, _HOST)[0]
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_pages SET port = 12022 WHERE agent_id = %s AND name = %s",
            (agent_id, key[1]),
        )
    db_conn.commit()
    monkeypatch.setattr(psd, "_open_rows", lambda _pool, _host: [stale])  # pyright: ignore[reportUnknownArgumentType]
    managed: dict[tuple[int, str], psd._ServerHandle] = {}

    _reconcile(sync_pool, managed, {}, {})

    assert backend.new_calls == []
    assert managed == {}
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT session_name FROM agent_pages WHERE agent_id = %s AND name = %s",
            (agent_id, key[1]),
        )
        row = cur.fetchone()
        assert row is not None and row[0] is None


def test_session_rolled_back_when_row_expires_during_create(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    backend: _FakeShellBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """A row that expires in the window between the liveness re-check and the
    session_name write is rolled back too — the UPDATE's liveness condition
    matches the re-check, so an expired row never keeps a ghost session."""
    agent_id = spawn_agent(catalog=model_catalog, authority=config_authority)
    key = (agent_id, "mid-create-expire")
    _insert_page_row(db_conn, agent_id, key[1], 12024, tmp_path)
    stale = psd._open_rows(sync_pool, _HOST)[0]
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_pages SET expired_at = now() WHERE agent_id = %s AND name = %s",
            (agent_id, key[1]),
        )
    db_conn.commit()
    monkeypatch.setattr(psd, "_open_rows", lambda _pool, _host: [stale])  # pyright: ignore[reportUnknownArgumentType]
    # The liveness re-check passed a moment ago; expiry lands after it.
    monkeypatch.setattr(psd, "_row_matches_snapshot", lambda _pool, _row: True)  # pyright: ignore[reportUnknownArgumentType]
    managed: dict[tuple[int, str], psd._ServerHandle] = {}
    backoff: dict[tuple[int, str], float] = {}

    _reconcile(sync_pool, managed, backoff, {})

    assert len(backend.new_calls) == 1
    assert backend.killed == [backend.new_calls[0][0]]
    assert managed == {}
    assert backoff[key] > time.monotonic()


def test_session_created_for_row_closed_during_create_is_rolled_back(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    backend: _FakeShellBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """The row can still close in the narrow window between the liveness
    re-check and the session_name write. The just-created session is rolled
    back (killed) instead of lingering as a recordless ghost the next pass
    would reap as an orphan."""
    agent_id = spawn_agent(catalog=model_catalog, authority=config_authority)
    key = (agent_id, "mid-create-close")
    _insert_page_row(db_conn, agent_id, key[1], 12023, tmp_path)
    stale = psd._open_rows(sync_pool, _HOST)[0]
    _close_row(db_conn, agent_id, key[1])
    monkeypatch.setattr(psd, "_open_rows", lambda _pool, _host: [stale])  # pyright: ignore[reportUnknownArgumentType]
    # The liveness re-check passed a moment ago; the close lands after it.
    monkeypatch.setattr(psd, "_row_matches_snapshot", lambda _pool, _row: True)  # pyright: ignore[reportUnknownArgumentType]
    managed: dict[tuple[int, str], psd._ServerHandle] = {}
    backoff: dict[tuple[int, str], float] = {}

    _reconcile(sync_pool, managed, backoff, {})

    assert len(backend.new_calls) == 1
    assert backend.killed == [backend.new_calls[0][0]]
    assert managed == {}
    assert backoff[key] > time.monotonic()


def test_terminated_agent_keeps_daemon_page_session(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    backend: _FakeShellBackend,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    """Agent termination does not close a daemon-supervised page row or shell."""
    agent_id = spawn_agent(catalog=model_catalog, authority=config_authority)
    key = (agent_id, "persistent")
    _insert_page_row(db_conn, agent_id, key[1], 12016, tmp_path)
    managed: dict[tuple[int, str], psd._ServerHandle] = {}

    _reconcile(sync_pool, managed, {}, {})
    page_session = managed[key].session_name
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
    db_conn.commit()

    _reconcile(sync_pool, managed, {}, {})

    assert backend.killed == []
    assert backend.sessions == {page_session}
    assert set(managed) == {key}
    assert [(row.agent_id, row.name) for row in psd._open_rows(sync_pool, _HOST)] == [key]


def test_daemon_restart_kills_the_persisted_session_of_a_closed_row(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    backend: _FakeShellBackend,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    agent_id = spawn_agent(catalog=model_catalog, authority=config_authority)
    page_session = f"ava-agent-{agent_id}-shell-4-page-closed-after-restart"
    backend.sessions.add(page_session)
    _insert_page_row(
        db_conn, agent_id, "closed-after-restart", 12014, tmp_path, session=page_session
    )
    _close_row(db_conn, agent_id, "closed-after-restart")

    _reconcile(sync_pool, {}, {}, {})

    assert backend.killed == [page_session]
