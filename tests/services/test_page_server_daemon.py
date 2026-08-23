"""Page server supervisor daemon tests (R3 door ③).

Covers the supervision contract: the agent_pages table is the truth source,
the daemon spawns a server per open serve_dir row on this host, kills it
when the row closes or the process dies, and backs off after a failed
spawn. `_spawn_server` is exercised against a real subprocess in one test
(the server module really answers /health with the token); the reconcile
passes stub the spawn so no stray processes leak from tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import services.page_server.daemon as psd
import services.page_server.degradation as page_degradation
from shared.machine import reset_identity, set_identity
from tests.conftest import spawn_agent

_HOST = "100.64.0.1"  # injected reachable address; rows use it so the daemon claims them


@pytest.fixture(autouse=True)
def _identity() -> Iterator[None]:
    set_identity(host=_HOST)
    yield
    reset_identity()


def _insert_page_row(
    db_conn: psycopg.Connection,
    agent_id: int,
    name: str,
    port: int,
    *,
    serve_dir: str | None,
    host: str = _HOST,
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_pages (agent_id, name, port, host, serve_dir) "
            "VALUES (%s, %s, %s, %s, %s)",
            (agent_id, name, port, host, serve_dir),
        )
    db_conn.commit()


def _close_row(db_conn: psycopg.Connection, agent_id: int, name: str) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_pages SET closed_at = now() "
            "WHERE agent_id = %s AND name = %s AND closed_at IS NULL",
            (agent_id, name),
        )
    db_conn.commit()


@pytest.fixture
def sync_pool(db_conn: psycopg.Connection):
    """A ConnectionPool over the test DB, mirroring the daemon's pool."""

    from shared.config import settings

    pool = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    yield pool
    pool.close()


def _stub_handle(
    port: int = 12345,
    serve_dir: str = "/tmp/serve",  # noqa: S108
    agent_id: int = 1,
    name: str = "p",
) -> psd._ServerHandle:
    import subprocess as _sp
    from typing import cast as _cast

    proc = _cast(
        _sp.Popen[bytes],
        SimpleNamespace(
            poll=lambda: None,
            terminate=lambda: None,
            kill=lambda: None,
            wait=lambda _timeout: None,  # pyright: ignore[reportUnknownLambdaType]
            pid=9999,
            returncode=None,
        ),
    )
    return psd._ServerHandle(
        agent_id=agent_id,
        name=name,
        port=port,
        serve_dir=serve_dir,
        token="t",  # noqa: S106
        proc=proc,
        log_path=Path("/tmp/p.log"),  # noqa: S108
    )


class TestOpenRows:
    def test_filters_by_host_and_serve_dir(
        self, sync_pool, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        agent_id = spawn_agent()
        serve_dir = str(tmp_path)
        _insert_page_row(db_conn, agent_id, "managed", 12001, serve_dir=serve_dir)
        _insert_page_row(
            db_conn, agent_id, "other-host", 12002, serve_dir=serve_dir, host="10.0.0.9"
        )
        _insert_page_row(db_conn, agent_id, "agent-owned", 12003, serve_dir=None)
        _insert_page_row(db_conn, agent_id, "closed", 12004, serve_dir=serve_dir)
        _close_row(db_conn, agent_id, "closed")

        rows = psd._open_rows(sync_pool, _HOST)  # pyright: ignore[reportUnknownArgumentType]
        names = {(r.agent_id, r.name) for r in rows}
        assert (agent_id, "managed") in names
        assert (agent_id, "other-host") not in names
        assert (agent_id, "agent-owned") not in names
        assert (agent_id, "closed") not in names


class TestSpawnServer:
    def test_spawn_verify_kill_roundtrip(self, sync_pool, tmp_path: Path) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        """A real server subprocess comes up, answers /health with our token,
        and dies on _kill_server."""
        import socket
        import time

        # Find a free port, release it, let the server take it.
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        row = psd._PageRow(1, 1, "p", port, "127.0.0.1", str(tmp_path))
        handle = psd._spawn_server(row, tmp_path / "logs")
        try:
            assert handle.proc.poll() is None, "server process is alive"
            assert psd._server_is_healthy("127.0.0.1", port, handle.token)
            assert not psd._server_is_healthy("127.0.0.1", port, "wrong-token")
        finally:
            psd._kill_server(handle)
        time.sleep(0.2)
        assert handle.proc.poll() is not None, "server killed"
        assert not psd._server_is_healthy("127.0.0.1", port, handle.token)


class TestReconcile:
    def test_spawns_missing_and_kills_closed(
        self,
        sync_pool,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        tmp_path: Path,
    ) -> None:
        spawned: list[tuple[int, str]] = []
        killed: list[tuple[int, str]] = []

        def fake_spawn(row: psd._PageRow, log_dir: Path) -> psd._ServerHandle:
            spawned.append((row.agent_id, row.name))
            return _stub_handle(row.port, row.serve_dir, row.agent_id, row.name)

        def fake_kill(handle: psd._ServerHandle) -> None:
            killed.append((handle.agent_id, handle.name))

        monkeypatch.setattr(psd, "_spawn_server", fake_spawn)
        monkeypatch.setattr(psd, "_kill_server", fake_kill)

        agent_id = spawn_agent()
        _insert_page_row(db_conn, agent_id, "a", 12011, serve_dir=str(tmp_path))
        _insert_page_row(db_conn, agent_id, "b", 12012, serve_dir=str(tmp_path))

        managed: dict[tuple[int, str], psd._ServerHandle] = {}
        backoff: dict[tuple[int, str], float] = {}
        degraded: dict[tuple[int, str], psd._DegradedServeDir] = {}
        psd._reconcile_once(sync_pool, managed, backoff, degraded, _HOST, Path("/tmp/logs"))  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
        assert set(spawned) == {(agent_id, "a"), (agent_id, "b")}
        assert set(managed) == {(agent_id, "a"), (agent_id, "b")}

        # Close one row: next pass kills its server only.
        _close_row(db_conn, agent_id, "a")
        psd._reconcile_once(sync_pool, managed, backoff, degraded, _HOST, Path("/tmp/logs"))  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
        assert killed == [(agent_id, "a")]
        assert set(managed) == {(agent_id, "b")}

    def test_respawns_dead_process(
        self,
        sync_pool,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        tmp_path: Path,
    ) -> None:
        import subprocess as _sp
        from typing import cast as _cast

        dead = _stub_handle(12021, serve_dir=str(tmp_path))
        dead.proc = _cast(
            _sp.Popen[bytes],
            SimpleNamespace(
                poll=lambda: 1,
                terminate=lambda: None,
                kill=lambda: None,
                wait=lambda _timeout: None,  # pyright: ignore[reportUnknownLambdaType]
                pid=1,
                returncode=1,
            ),
        )
        managed: dict[tuple[int, str], psd._ServerHandle] = {(1, "p"): dead}
        backoff: dict[tuple[int, str], float] = {}

        spawned: list[tuple[int, str]] = []

        def fake_spawn(row: psd._PageRow, log_dir: Path) -> psd._ServerHandle:
            spawned.append((row.agent_id, row.name))
            return _stub_handle(row.port, row.serve_dir, row.agent_id, row.name)

        monkeypatch.setattr(psd, "_spawn_server", fake_spawn)
        monkeypatch.setattr(psd, "_kill_server", lambda _h: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

        agent_id = spawn_agent()
        _insert_page_row(db_conn, agent_id, "p", 12021, serve_dir=str(tmp_path))
        # The managed key must match the row's key for the dead-process branch.
        managed = {(agent_id, "p"): dead}
        degraded: dict[tuple[int, str], psd._DegradedServeDir] = {}
        psd._reconcile_once(sync_pool, managed, backoff, degraded, _HOST, Path("/tmp/logs"))  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
        assert (agent_id, "p") in spawned, "dead process respawned"
        assert (agent_id, "p") in managed

    def test_backoff_after_failed_spawn(
        self,
        sync_pool,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        tmp_path: Path,
    ) -> None:
        calls: list[bool] = []

        def flaky_spawn(row: psd._PageRow, log_dir: Path) -> psd._ServerHandle:
            calls.append(True)
            raise RuntimeError("port in use")

        monkeypatch.setattr(psd, "_spawn_server", flaky_spawn)

        agent_id = spawn_agent()
        _insert_page_row(db_conn, agent_id, "p", 12031, serve_dir=str(tmp_path))

        managed: dict[tuple[int, str], psd._ServerHandle] = {}
        backoff: dict[tuple[int, str], float] = {}
        degraded: dict[tuple[int, str], psd._DegradedServeDir] = {}
        psd._reconcile_once(sync_pool, managed, backoff, degraded, _HOST, Path("/tmp/logs"))  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
        assert len(calls) == 1
        assert (agent_id, "p") in backoff, "failed spawn recorded for backoff"
        assert (agent_id, "p") not in managed

        # Immediate re-pass: backed off, no spawn attempt.
        psd._reconcile_once(sync_pool, managed, backoff, degraded, _HOST, Path("/tmp/logs"))  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
        assert len(calls) == 1, "no hot-looping a broken row"

    def test_degrades_missing_serve_dir_without_spawning(
        self,
        sync_pool,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        tmp_path: Path,
    ) -> None:
        agent_id = spawn_agent()
        name = "missing"
        serve_dir = tmp_path / "removed"
        _insert_page_row(db_conn, agent_id, name, 12032, serve_dir=str(serve_dir))

        spawned: list[tuple[int, str]] = []
        warnings: list[str] = []

        def fake_spawn(row: psd._PageRow, log_dir: Path) -> psd._ServerHandle:
            spawned.append((row.agent_id, row.name))
            return _stub_handle(row.port, row.serve_dir, row.agent_id, row.name)

        def fake_warning(message: str, *args: object, **kwargs: object) -> None:
            del kwargs
            warnings.append(message % args)

        monkeypatch.setattr(psd, "_spawn_server", fake_spawn)
        monkeypatch.setattr(psd._log, "warning", fake_warning)

        managed: dict[tuple[int, str], psd._ServerHandle] = {}
        backoff: dict[tuple[int, str], float] = {}
        degraded: dict[tuple[int, str], psd._DegradedServeDir] = {}
        psd._reconcile_once(sync_pool, managed, backoff, degraded, _HOST, Path("/tmp/logs"))  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]

        key = (agent_id, name)
        assert spawned == []
        assert degraded[key].observations == 1
        assert degraded[key].retry_at >= 30.0
        assert backoff == {}
        assert any("degrading" in warning and str(serve_dir) in warning for warning in warnings)
        psd._reconcile_once(sync_pool, managed, backoff, degraded, _HOST, Path("/tmp/logs"))  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
        assert warnings == [
            f"[page-server] degrading {key}: serve_dir is missing or not a directory: {serve_dir}"
        ]
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT closed_at FROM agent_pages WHERE agent_id = %s AND name = %s",
                (agent_id, name),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] is None

    def test_missing_serve_dir_backoff_grows_and_caps(self) -> None:
        assert [
            page_degradation._missing_serve_dir_backoff_s(observations)
            for observations in range(1, 6)
        ] == [30.0, 60.0, 120.0, 240.0, 300.0]

    def test_auto_closes_persistently_missing_serve_dir_and_publishes_page_closed(
        self,
        sync_pool,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        tmp_path: Path,
    ) -> None:
        from shared.live_events import PageClosed

        agent_id = spawn_agent()
        name = "missing"
        serve_dir = tmp_path / "removed"
        _insert_page_row(db_conn, agent_id, name, 12033, serve_dir=str(serve_dir))

        clock = [0.0]
        published: list[tuple[str, str, str]] = []
        emitted: list[str] = []

        def monotonic() -> float:
            return clock[0]

        async def fake_publish(channel: str, payload: str, *, context: str = "") -> int:
            published.append((channel, payload, context))
            return 0

        def fake_emit(category: str, event_name: str, **kwargs: object) -> None:
            del category, kwargs
            emitted.append(event_name)

        monkeypatch.setattr(psd.time, "monotonic", monotonic)
        monkeypatch.setattr(page_degradation, "publish_best_effort", fake_publish)
        monkeypatch.setattr(page_degradation.telemetry, "emit", fake_emit)

        managed: dict[tuple[int, str], psd._ServerHandle] = {}
        backoff: dict[tuple[int, str], float] = {}
        degraded: dict[tuple[int, str], psd._DegradedServeDir] = {}
        for now in (0.0, 30.0, 90.0, 210.0, 450.0):
            clock[0] = now
            psd._reconcile_once(sync_pool, managed, backoff, degraded, _HOST, tmp_path / "logs")  # pyright: ignore[reportUnknownArgumentType]

        assert published == [
            (
                psd.settings.data_plane.events_channel,
                PageClosed(agent_id=agent_id, name=name).model_dump_json(),
                "page_server_serve_dir_missing",
            )
        ]
        assert emitted == ["page_serve_dir_missing", "page_serve_dir_missing"]
        assert degraded == {}
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT closed_at FROM agent_pages WHERE agent_id = %s AND name = %s",
                (agent_id, name),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] is not None

    def test_recovers_when_serve_dir_reappears(
        self,
        sync_pool,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        tmp_path: Path,
    ) -> None:
        agent_id = spawn_agent()
        name = "recovered"
        serve_dir = tmp_path / "recreated"
        _insert_page_row(db_conn, agent_id, name, 12034, serve_dir=str(serve_dir))

        spawned: list[tuple[int, str]] = []
        infos: list[str] = []

        def fake_spawn(row: psd._PageRow, log_dir: Path) -> psd._ServerHandle:
            spawned.append((row.agent_id, row.name))
            return _stub_handle(row.port, row.serve_dir, row.agent_id, row.name)

        def fake_info(message: str, *args: object, **kwargs: object) -> None:
            del kwargs
            infos.append(message % args)

        monkeypatch.setattr(psd, "_spawn_server", fake_spawn)
        monkeypatch.setattr(psd._log, "info", fake_info)

        managed: dict[tuple[int, str], psd._ServerHandle] = {}
        backoff: dict[tuple[int, str], float] = {}
        degraded: dict[tuple[int, str], psd._DegradedServeDir] = {}
        psd._reconcile_once(sync_pool, managed, backoff, degraded, _HOST, Path("/tmp/logs"))  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
        assert degraded[(agent_id, name)].observations == 1

        serve_dir.mkdir()
        psd._reconcile_once(sync_pool, managed, backoff, degraded, _HOST, Path("/tmp/logs"))  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]

        assert spawned == [(agent_id, name)]
        assert (agent_id, name) in managed
        assert degraded == {}
        assert any("recovered" in info for info in infos)


class TestReclaim:
    """Daemon restart leaves detached page servers behind; the reconcile
    pass must kill them (audit round 2, P1) — `managed` is empty but the
    processes survive."""

    def _reconcile_with_occupants(
        self,
        sync_pool,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        occupants: dict[int, int],
    ) -> tuple[list[int], list[tuple[int, str]]]:
        killed: list[int] = []
        spawned: list[tuple[int, str]] = []

        # occupants entries are (pid, ava_home); the helper fills in this
        # daemon's own home so existing call sites pass plain {port: pid}.
        monkeypatch.setattr(
            psd,
            "_page_server_occupants",
            lambda: {p: (pid, str(psd.settings.general.ava_home)) for p, pid in occupants.items()},
        )
        monkeypatch.setattr(psd, "_kill_pid", killed.append)
        monkeypatch.setattr(
            psd,
            "_spawn_server",
            lambda row, _log_dir: _stub_handle(row.port, row.serve_dir, row.agent_id, row.name),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
        )

        managed: dict[tuple[int, str], psd._ServerHandle] = {}
        backoff: dict[tuple[int, str], float] = {}
        degraded: dict[tuple[int, str], psd._DegradedServeDir] = {}
        psd._reconcile_once(sync_pool, managed, backoff, degraded, _HOST, Path("/tmp/logs"))  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
        return killed, spawned

    def test_reclaims_wanted_port_occupant(
        self,
        sync_pool,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        tmp_path: Path,
    ) -> None:
        """After a restart, an occupant on a still-open row's port is killed
        and the row respawned with a fresh token (the old token is
        unknowable — adopt is impossible, kill+respawn is the fix)."""
        agent_id = spawn_agent()
        _insert_page_row(db_conn, agent_id, "p", 12041, serve_dir=str(tmp_path))
        killed, _ = self._reconcile_with_occupants(sync_pool, db_conn, monkeypatch, {12041: 5555})  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        assert killed == [5555]

    def test_kills_orphan_of_closed_row(
        self,
        sync_pool,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        tmp_path: Path,
    ) -> None:
        """A server of a since-closed row (daemon was down when it closed)
        is killed — previously it leaked the process and port forever."""
        agent_id = spawn_agent()
        _insert_page_row(db_conn, agent_id, "gone", 12042, serve_dir=str(tmp_path))
        _close_row(db_conn, agent_id, "gone")
        killed, _ = self._reconcile_with_occupants(sync_pool, db_conn, monkeypatch, {12042: 5556})  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        assert killed == [5556]

    def test_never_kills_own_managed_process(
        self,
        sync_pool,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        tmp_path: Path,
    ) -> None:
        """The reclaim pass must not kill a process this daemon itself
        manages (normal steady state — occupant and managed are the same)."""
        agent_id = spawn_agent()
        _insert_page_row(db_conn, agent_id, "p", 12043, serve_dir=str(tmp_path))
        own = _stub_handle(12043, serve_dir=str(tmp_path), agent_id=agent_id)
        managed: dict[tuple[int, str], psd._ServerHandle] = {(agent_id, "p"): own}
        backoff: dict[tuple[int, str], float] = {}
        degraded: dict[tuple[int, str], psd._DegradedServeDir] = {}
        killed: list[int] = []

        monkeypatch.setattr(
            psd,
            "_page_server_occupants",
            lambda: {12043: (9999, str(psd.settings.general.ava_home))},
        )
        monkeypatch.setattr(psd, "_kill_pid", killed.append)
        psd._reconcile_once(sync_pool, managed, backoff, degraded, _HOST, Path("/tmp/logs"))  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
        assert killed == [], "own managed process must not be killed"

    def test_skips_foreign_cluster_occupant(
        self,
        sync_pool,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        tmp_path: Path,
    ) -> None:
        """A page-server process whose AVA_HOME names another cluster's home
        is NOT this daemon's orphan: killing it would tear down a co-located
        second cluster's pages (#1129, 2026-08-10 — preview's daemon reaped
        main's pages on the shared box in an endless respawn loop)."""
        agent_id = spawn_agent()
        _insert_page_row(db_conn, agent_id, "p", 12044, serve_dir=str(tmp_path))
        killed: list[int] = []
        managed: dict[tuple[int, str], psd._ServerHandle] = {}
        backoff: dict[tuple[int, str], float] = {}
        degraded: dict[tuple[int, str], psd._DegradedServeDir] = {}

        monkeypatch.setattr(
            psd, "_page_server_occupants", lambda: {12044: (5557, "/other/cluster/home")}
        )
        monkeypatch.setattr(psd, "_kill_pid", killed.append)
        psd._reconcile_once(sync_pool, managed, backoff, degraded, _HOST, Path("/tmp/logs"))  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
        assert killed == [], "foreign-cluster page server must not be killed"

    def test_skips_occupant_with_unreadable_env(
        self,
        sync_pool,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        tmp_path: Path,
    ) -> None:
        """A page-server whose AVA_HOME cannot be read (None) is NOT provably
        ours — skip, never guess. The pre-fix code fell through on None and
        reaped a co-located cluster's pages whose env was unreadable (Task
        #1141, 2026-08-10 — three SIGTERM waves on main's pages coincided
        with preview worker activity)."""
        agent_id = spawn_agent()
        _insert_page_row(db_conn, agent_id, "p", 12045, serve_dir=str(tmp_path))
        killed: list[int] = []
        managed: dict[tuple[int, str], psd._ServerHandle] = {}
        backoff: dict[tuple[int, str], float] = {}
        degraded: dict[tuple[int, str], psd._DegradedServeDir] = {}

        monkeypatch.setattr(psd, "_page_server_occupants", lambda: {12045: (5558, None)})
        monkeypatch.setattr(psd, "_kill_pid", killed.append)
        psd._reconcile_once(sync_pool, managed, backoff, degraded, _HOST, Path("/tmp/logs"))  # noqa: S108  # pyright: ignore[reportUnknownArgumentType]
        assert killed == [], "unreadable-env page server must not be killed"
