"""Page server daemon cases: changed port or directory kills then recreates."""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import services.agent_runner.page_server.daemon as psd
import services.agent_runner.page_server.degradation as page_degradation
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from services.agent_runner.page_server.tests.test_page_server_daemon import (
    _HOST,
    _FakeShellBackend,
    _insert_page_row,
    _reconcile,
)
from services.agent_runner.page_server.tests.test_page_server_daemon import (
    _identity as _identity,
)
from services.agent_runner.page_server.tests.test_page_server_daemon import (
    backend as backend,
)
from services.agent_runner.page_server.tests.test_page_server_daemon import (
    sync_pool as sync_pool,
)
from tests.fixtures.units import spawn_agent


def test_changed_port_or_directory_kills_then_recreates_the_page_session(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    backend: _FakeShellBackend,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = spawn_agent(
        catalog=model_catalog, authority=config_authority, database_gate=database_gate
    )
    old_dir = tmp_path / "old"
    old_dir.mkdir()
    new_dir = tmp_path / "new"
    new_dir.mkdir()
    _insert_page_row(db_conn, agent_id, "changed", 12007, old_dir)
    managed: dict[tuple[int, str], psd._ServerHandle] = {}
    backoff: dict[tuple[int, str], float] = {}
    degraded: dict[tuple[int, str], psd._DegradedServeDir] = {}

    _reconcile(sync_pool, managed, backoff, degraded)
    old_session = managed[(agent_id, "changed")].session_name
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_pages SET port = 12008, serve_dir = %s WHERE agent_id = %s AND name = 'changed'",
            (str(new_dir), agent_id),
        )
    db_conn.commit()
    _reconcile(sync_pool, managed, backoff, degraded)
    assert backend.killed == [old_session]
    assert managed[(agent_id, "changed")].session_name == old_session
    assert len(backend.new_calls) == 2


def test_daemon_restart_adopts_a_healthy_live_page_session(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    backend: _FakeShellBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = spawn_agent(
        catalog=model_catalog, authority=config_authority, database_gate=database_gate
    )
    page_session = f"ava-agent-{agent_id}-shell-5-page-adopted"
    backend.sessions.add(page_session)
    _insert_page_row(
        db_conn,
        agent_id,
        "adopted",
        12009,
        tmp_path,
        token=secrets.token_hex(16),
        session=page_session,
    )
    monkeypatch.setattr(psd, "_server_is_healthy", lambda *_args: True)  # pyright: ignore[reportUnknownArgumentType]
    managed: dict[tuple[int, str], psd._ServerHandle] = {}

    _reconcile(sync_pool, managed, {}, {})

    assert set(managed) == {(agent_id, "adopted")}
    assert backend.new_calls == []
    assert backend.killed == []
    assert backend.sent == []


def test_reclaim_preserves_in_session_server_and_kills_detached_orphan(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    backend: _FakeShellBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = spawn_agent(
        catalog=model_catalog, authority=config_authority, database_gate=database_gate
    )
    key = (agent_id, "kept")
    page_session = f"ava-agent-{agent_id}-shell-6-page-kept"
    backend.sessions.add(page_session)
    _insert_page_row(
        db_conn,
        agent_id,
        "kept",
        12010,
        tmp_path,
        token=secrets.token_hex(16),
        session=page_session,
    )
    monkeypatch.setattr(
        psd,
        "_page_server_occupants",
        lambda: {
            12010: (5010, str(psd.ava_home())),
            12011: (5011, str(psd.ava_home())),
        },
    )
    monkeypatch.setattr(psd, "_page_session_owner", lambda pid, _pids: key if pid == 5010 else None)  # pyright: ignore[reportUnknownArgumentType]
    killed: list[int] = []
    monkeypatch.setattr(psd, "_kill_pid", killed.append)
    monkeypatch.setattr(psd, "_server_is_healthy", lambda *_args: True)  # pyright: ignore[reportUnknownArgumentType]

    _reconcile(sync_pool, {}, {}, {})

    assert killed == [5011]


def test_failed_session_creation_uses_spawn_backoff(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    backend: _FakeShellBackend,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = spawn_agent(
        catalog=model_catalog, authority=config_authority, database_gate=database_gate
    )
    _insert_page_row(db_conn, agent_id, "backoff", 12012, tmp_path)
    backend.new_result = False
    managed: dict[tuple[int, str], psd._ServerHandle] = {}
    backoff: dict[tuple[int, str], float] = {}

    _reconcile(sync_pool, managed, backoff, {})
    _reconcile(sync_pool, managed, backoff, {})

    assert managed == {}
    assert len(backend.new_calls) == 1
    assert (agent_id, "backoff") in backoff


def test_missing_serve_dir_uses_the_existing_degradation_ladder(
    sync_pool: ConnectionPool,
    db_conn: psycopg.Connection,
    backend: _FakeShellBackend,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = spawn_agent(
        catalog=model_catalog, authority=config_authority, database_gate=database_gate
    )
    missing = tmp_path / "gone"
    _insert_page_row(db_conn, agent_id, "missing", 12013, missing)
    degraded: dict[tuple[int, str], psd._DegradedServeDir] = {}

    _reconcile(sync_pool, {}, {}, degraded)

    assert backend.new_calls == []
    assert degraded[(agent_id, "missing")].observations == 1
    assert [page_degradation._missing_serve_dir_backoff_s(n) for n in range(1, 6)] == [
        30.0,
        60.0,
        120.0,
        240.0,
        300.0,
    ]


def test_page_session_owner_walks_process_ancestry(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Parent:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    class _Proc:
        @staticmethod
        def parents() -> list[_Parent]:
            return [_Parent(11), _Parent(12)]

    monkeypatch.setattr(psd.psutil, "Process", lambda _pid: _Proc())  # pyright: ignore[reportUnknownArgumentType]
    assert psd._page_session_owner(99, {12: (7, "page")}) == (7, "page")
    assert psd._page_session_owner(99, {11: (8, "near"), 12: (7, "page")}) == (8, "near")
    # Unrelated host ancestry must not acquire a managed page's ownership.
    assert psd._page_session_owner(99, {77: (7, "page")}) is None


def test_server_module_still_serves_a_tokenized_health_endpoint(tmp_path: Path) -> None:
    with socket.socket() as sock:
        sock.bind((_HOST, 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "PAGE_SERVER_TOKEN": "roundtrip"}
    proc = subprocess.Popen(  # noqa: S603 -- server module receives fixed test arguments
        [
            sys.executable,
            "-m",
            "services.agent_runner.page_server.server",
            "--port",
            str(port),
            "--host",
            _HOST,
            "--dir",
            str(tmp_path),
        ],
        cwd=Path.cwd(),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if psd._server_is_healthy(_HOST, port, "roundtrip"):
                break
            time.sleep(0.05)
        assert psd._server_is_healthy(_HOST, port, "roundtrip")
    finally:
        # SIGKILL: a shell session's SIGTERM=SIG_IGN is inherited, so the
        # graceful call would leave the page server alive.
        proc.kill()
        proc.wait(timeout=2.0)
