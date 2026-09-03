"""Page-server supervisor daemon.

Each open ``agent_pages`` serve row on this host is manifested as a daemon-owned
persistent shell session for its agent. The shell runs the page server command,
but remains alive when that command exits so this daemon can relaunch it in the
same session. Page sessions are detached from the service tree, so a rollout
does not kill pages; daemon restarts adopt their live sessions from the table.

The ``agent_pages`` table is the truth source. The managed table in this module
is only a per-process cache. Rows with an unavailable ``serve_dir`` retain the
existing degradation ladder; rows that close or change kill their page session.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shlex
import sys
import time
import urllib.request
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import psutil
import psycopg
from psycopg_pool import ConnectionPool

import shared.db
import shared.pty_sessions.cli
from services._pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from shared.cluster import session_name
from shared.config import settings
from shared.daemon_health import Liveness, start_health_server, stop_health_server
from shared.daemon_shutdown import install_graceful_shutdown
from shared.db_transaction import write_transaction
from shared.log import init_gateway_process
from shared.machine import machine_name, reachable_host
from shared.paths import legacy_pid_path
from shared.session_backend import PtySessionBackend, SessionBackend, get_shell_backend
from shared.session_record import SessionRecord

from .degradation import (
    _DegradedServeDir,
    _discard_gone_degraded,
    _PageRow,
    _reconcile_serve_dir,
)

_log = logging.getLogger("services.page_server.daemon")

_POLL_INTERVAL_S = settings.daemon.page_server_poll_interval_seconds
_LIVENESS_TIMEOUT_S = 60.0
_PIDFILE = settings.services.page_server_pidfile

# A new PTY host needs a short window to finish its interactive-shell startup
# and receive its initial command before a health probe can make a decision.
_SPAWN_VERIFY_TIMEOUT_S = 5.0
# Cooldown after a failed launch or a foreign port occupant.
_SPAWN_BACKOFF_S = 30.0
_PAGE_SERVER_MODULE = "services.page_server.server"
_PTY_NAME_RE = re.compile(r"[a-z][a-z0-9-]*")


@dataclass
class _ServerHandle:
    """One page-server command supervised through its persistent shell."""

    agent_id: int
    name: str
    port: int
    serve_dir: str
    token: str
    session_name: str
    last_launch_monotonic: float


def _write_pidfile() -> None:
    if not acquire_pidfile(_PIDFILE, "services.page_server.daemon"):
        _log.info("[page-server] daemon already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)


def _remove_pidfile() -> None:
    remove_pidfile(_PIDFILE)


def _is_running() -> bool:
    """Whether a daemon is already running (via pidfile, new + legacy paths)."""
    return pidfile_holds_daemon(_PIDFILE, "services.page_server.daemon") or pidfile_holds_daemon(
        legacy_pid_path("page_server"), "services.page_server.daemon"
    )


def _open_rows(pool: ConnectionPool, host: str) -> list[_PageRow]:
    """Every open serve row supervised by this host's daemon."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, agent_id, name, port, host, serve_dir, server_token, session_name "
            "FROM agent_pages "
            "WHERE closed_at IS NULL AND expired_at IS NULL "
            "AND serve_dir IS NOT NULL AND host = %s "
            "ORDER BY agent_id, name",
            (host,),
        )
        return [_PageRow(*row) for row in cur.fetchall()]


def _closed_page_sessions(pool: ConnectionPool, host: str) -> list[str]:
    """Persisted page sessions whose rows closed while this daemon was away."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT session_name FROM agent_pages "
            "WHERE (closed_at IS NOT NULL OR expired_at IS NOT NULL) "
            "AND serve_dir IS NOT NULL AND host = %s "
            "AND session_name IS NOT NULL",
            (host,),
        )
        return [str(row[0]) for row in cur.fetchall()]


def _server_command(row: _PageRow) -> str:
    """The shell-safe command that starts a row's page server."""
    return shlex.join(
        [
            sys.executable,
            "-m",
            _PAGE_SERVER_MODULE,
            "--port",
            str(row.port),
            "--host",
            row.host,
            "--dir",
            row.serve_dir,
        ]
    )


def _page_session_name(agent_id: int, name: str, session_index: int) -> str:
    """Build the agent-shell name assigned to one page row."""
    slug = name.lower().replace("_", "-")
    full = f"{session_name(f'agent-{agent_id}')}-shell-{session_index}-page-{slug}"
    if _PTY_NAME_RE.fullmatch(full) is None:
        raise ValueError(f"page session name {full!r} does not match the PTY name contract")
    return full


def _allocate_session_index(pool: ConnectionPool, agent_id: int) -> int:
    """Atomically allocate one shell id from the owning agent's shared counter."""
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET session_index = session_index + 1 "
            "WHERE id = %s RETURNING session_index",
            (agent_id,),
        )
        row = cur.fetchone()
        conn.commit()
    if row is None:
        raise RuntimeError(f"agent {agent_id} not in agents_meta table — cannot allocate session")
    return int(row[0]) - 1


def _ensure_token(pool: ConnectionPool, row: _PageRow) -> str:
    """Return the row's durable health token, minting it on first adoption.

    The mint only persists for a still-open row; a row that closed or expired
    while this pass reconciled is terminal and never written (the caller
    drops it this pass anyway), so the minted value is returned unpersisted
    rather than raising and aborting the whole reconcile pass."""
    if row.server_token is not None:
        return row.server_token
    minted = secrets.token_hex(16)
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_pages SET server_token = COALESCE(server_token, %s) "
            "WHERE id = %s AND closed_at IS NULL AND expired_at IS NULL "
            "RETURNING server_token",
            (minted, row.id),
        )
        persisted = cur.fetchone()
        conn.commit()
    if persisted is None or persisted[0] is None:
        return minted
    return str(persisted[0])


def _create_page_session(
    pool: ConnectionPool, backend: SessionBackend, row: _PageRow, token: str
) -> str:
    """Create or recreate the shell for a page row and persist its name."""
    page_session = row.session_name
    if page_session is None:
        page_session = _page_session_name(
            row.agent_id, row.name, _allocate_session_index(pool, row.agent_id)
        )
    env = {**os.environ, "PAGE_SERVER_TOKEN": token}
    if not backend.new_session(
        page_session,
        _server_command(row),
        Path(row.serve_dir),
        env=env,
    ):
        raise RuntimeError(f"failed to create page session {page_session!r}")
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_pages SET session_name = %s "
            "WHERE id = %s AND closed_at IS NULL AND expired_at IS NULL "
            "AND serve_dir IS NOT NULL AND host = %s",
            (page_session, row.id, row.host),
        )
        if cur.rowcount == 0:
            # The row left the open set between the liveness re-check and this
            # write (closed, expired, or re-registered without serve_dir) —
            # the caller's snapshot can be seconds stale. A session without a
            # live row is a recordless ghost the next pass would reap as an
            # orphan; roll the just-created session back instead.
            _log.warning(
                "[page-server] page row %s left the open set while creating its session; "
                "rolling back %s",
                row.id,
                page_session,
            )
            backend.kill_session(page_session)
            raise RuntimeError(f"page row {row.id} left the open set while creating its session")
        conn.commit()
    return page_session


def _launch_in_session(backend: SessionBackend, handle: _ServerHandle, row: _PageRow) -> None:
    """Relaunch a crashed server without replacing its POSIX shell session."""
    command = _server_command(row)
    try:
        backend.send(handle.session_name, command)
        backend.send_keys(handle.session_name, "Enter")
    except NotImplementedError as exc:
        # Windows has no interactive PTY transport. Its native shell backend
        # recreates the named session, retaining the durable session identity.
        backend.kill_session(handle.session_name)
        if not backend.new_session(
            handle.session_name,
            command,
            Path(row.serve_dir),
            env={**os.environ, "PAGE_SERVER_TOKEN": handle.token},
        ):
            raise RuntimeError(f"failed to recreate page session {handle.session_name!r}") from exc


def _server_is_healthy(host: str, port: int, token: str) -> bool:
    """Whether the server answers /health with this row's durable token."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=1.0) as resp:
            return resp.read() == f"ok:{token}".encode()
    except OSError:
        return False


def _probe_port(host: str, port: int) -> str | None:
    """Return /health's body, or None when no HTTP server answers the port."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=1.0) as resp:
            return resp.read().decode(errors="replace")
    except OSError:
        return None


def _page_server_occupants() -> dict[int, tuple[int, str | None]]:
    """Map each local page-server port to its process and cluster home."""
    found: dict[int, tuple[int, str | None]] = {}
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            raw_cmdline = proc.info.get("cmdline")
            cmdline = [str(part) for part in raw_cmdline] if raw_cmdline else []
            pid = int(proc.info["pid"])
        except (psutil.Error, OSError, TypeError, ValueError):
            continue
        rendered = " ".join(cmdline)
        if _PAGE_SERVER_MODULE not in rendered:
            continue
        match = re.search(r"--port (\d+)", rendered)
        if match is None:
            continue
        try:
            env = proc.environ()
        except (psutil.Error, OSError):
            env = {}
        found[int(match.group(1))] = (pid, env.get("AVA_HOME"))
    return found


def _live_session_records(backend: SessionBackend) -> dict[str, SessionRecord] | None:
    """One in-process scan of the live pty session records, or None when the
    backend is not the PTY supervisor (the Windows native backend keeps no
    pty record store).

    The PTY backend's per-name ``has_session`` costs a subprocess round-trip
    (~0.25 s each, measured 2026-08-28); a reconcile pass that asked once per
    managed row serialized dozens of Python startups and stretched the 2 s
    poll to ~30 s (2026-08-28 incident — serve()'s 15 s wait timed out about
    half the time). The record scan is the same liveness rule read
    in-process.
    """
    if not isinstance(backend, PtySessionBackend):
        return None
    return shared.pty_sessions.cli.live_sessions()


def _session_is_live(
    backend: SessionBackend, live_names: set[str] | None, session_name: str
) -> bool:
    """Whether a page shell is alive: membership in the pass's in-process
    record scan when one exists, else the backend's per-name check (the
    Windows native backend, whose has_session is in-process)."""
    if live_names is not None:
        return session_name in live_names
    return backend.has_session(session_name)


def _page_session_shell_pids(
    wanted: dict[tuple[int, str], _PageRow], live: dict[str, SessionRecord] | None
) -> dict[int, tuple[int, str]]:
    """Map live page-shell PIDs to their page-row keys on POSIX.

    ``live`` is the pass's pty record scan (None on Windows, where the native
    backend keeps no pty records)."""
    if live is None:
        return {}
    session_keys = {
        row.session_name: key for key, row in wanted.items() if row.session_name is not None
    }
    return {record.pid: session_keys[name] for name, record in live.items() if name in session_keys}


def _page_session_owner(pid: int, shell_pids: dict[int, tuple[int, str]]) -> tuple[int, str] | None:
    """Return the page row whose live shell is an ancestor of ``pid``."""
    try:
        parents = psutil.Process(pid).parents()
    except psutil.Error:
        return None
    for parent in parents:
        if owner := shell_pids.get(parent.pid):
            return owner
    return None


def _kill_pid(pid: int) -> None:
    """Terminate one positively identified detached legacy page server."""
    try:
        proc = psutil.Process(pid)
    except psutil.Error:
        return
    with suppress(psutil.Error):
        proc.terminate()
        proc.wait(timeout=2.0)
        return
    with suppress(psutil.Error):
        proc.kill()
        proc.wait(timeout=2.0)


def _kill_page_session(backend: SessionBackend, handle: _ServerHandle) -> None:
    """Stop the shell that owns the page server and every child it started."""
    backend.kill_session(handle.session_name)


def _reclaim_occupants(
    rows: list[_PageRow],
    occupants: dict[int, tuple[int, str | None]],
    shell_pids: dict[int, tuple[int, str]],
) -> None:
    """Kill detached legacy/orphan servers while preserving live page shells."""
    wanted_ports = {row.port for row in rows}
    own_home = str(settings.general.ava_home)
    for port, (pid, home) in occupants.items():
        if _page_session_owner(pid, shell_pids) is not None:
            continue
        if home != own_home:
            _log.info(
                "[page-server] skipping port %s pid=%s (foreign cluster home %s)",
                port,
                pid,
                home,
            )
            continue
        if port in wanted_ports:
            _log.info("[page-server] reclaiming detached port %s pid=%s", port, pid)
        else:
            _log.info("[page-server] closing orphaned port %s pid=%s", port, pid)
        _kill_pid(pid)


def _close_persisted_sessions(
    backend: SessionBackend, session_names: list[str], live_names: set[str] | None
) -> None:
    """Finish closed-page teardown after a daemon restart lost its cache."""
    for page_session in session_names:
        if _session_is_live(backend, live_names, page_session):
            _log.info("[page-server] closing persisted page session %s", page_session)
            backend.kill_session(page_session)


def _drop_stale_handles(
    backend: SessionBackend,
    wanted: dict[tuple[int, str], _PageRow],
    managed: dict[tuple[int, str], _ServerHandle],
    backoff: dict[tuple[int, str], float],
    degraded: dict[tuple[int, str], _DegradedServeDir],
    live_names: set[str] | None,
) -> None:
    """Remove managed entries whose rows or sessions no longer match."""
    for key in list(managed):
        row = wanted.get(key)
        handle = managed[key]
        if row is None:
            _log.info("[page-server] closing %s (row gone)", key)
            _kill_page_session(backend, handle)
            del managed[key]
        elif row.port != handle.port or row.serve_dir != handle.serve_dir:
            _log.info("[page-server] replacing %s (port/dir changed)", key)
            _kill_page_session(backend, handle)
            del managed[key]
            backoff.pop(key, None)
            degraded.pop(key, None)
        elif not _session_is_live(backend, live_names, handle.session_name):
            _log.warning("[page-server] page session died for %s", key)
            del managed[key]


def _row_matches_snapshot(pool: ConnectionPool, row: _PageRow) -> bool:
    """Whether the row this pass snapshotted is still the live supervised truth.

    ``_reconcile_once`` reads open rows once and then does slow work (process
    scans, session spawns — a pass can take tens of seconds under load). A
    page can be closed or re-registered (new port / serve_dir) during that
    window; creating a session from the stale snapshot would make the next
    pass reap the fresh session as an orphan — row gone / port not wanted —
    the user-visible serve 502 race. Freshly re-read the row and require it
    to still be open and unchanged before a session is created for it.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT port, serve_dir FROM agent_pages "
            "WHERE id = %s AND closed_at IS NULL AND expired_at IS NULL "
            "AND serve_dir IS NOT NULL AND host = %s",
            (row.id, row.host),
        )
        fresh = cur.fetchone()
    return fresh is not None and (fresh[0], fresh[1]) == (row.port, row.serve_dir)


def _ensure_handle(
    pool: ConnectionPool,
    backend: SessionBackend,
    row: _PageRow,
    key: tuple[int, str],
    managed: dict[tuple[int, str], _ServerHandle],
    backoff: dict[tuple[int, str], float],
    degraded: dict[tuple[int, str], _DegradedServeDir],
    now: float,
    live_names: set[str] | None,
) -> tuple[_ServerHandle | None, bool]:
    """Return a managed handle and whether this pass created its shell."""
    if handle := managed.get(key):
        return handle, False
    if _reconcile_serve_dir(pool, row, key, degraded, backoff, now):
        return None, False
    if now < backoff.get(key, 0.0):
        return None, False
    if row.session_name is not None and _session_is_live(backend, live_names, row.session_name):
        handle = _ServerHandle(
            row.agent_id,
            row.name,
            row.port,
            row.serve_dir,
            _ensure_token(pool, row),
            row.session_name,
            now - _SPAWN_VERIFY_TIMEOUT_S,
        )
        managed[key] = handle
        return handle, False
    if not _row_matches_snapshot(pool, row):
        # The row closed or was re-registered while this pass reconciled —
        # never create a session from stale snapshot state. The next pass
        # (poll interval away) adopts the current row truth.
        return None, False
    try:
        token = _ensure_token(pool, row)
        page_session = _create_page_session(pool, backend, row, token)
    except RuntimeError as exc:
        _log.error("[page-server] session create failed for %s: %s", key, exc)
        backoff[key] = now + _SPAWN_BACKOFF_S
        return None, False
    handle = _ServerHandle(
        row.agent_id, row.name, row.port, row.serve_dir, token, page_session, now
    )
    managed[key] = handle
    backoff.pop(key, None)
    _log.info(
        "[page-server] created session %s for %s port=%s dir=%s",
        page_session,
        key,
        row.port,
        row.serve_dir,
    )
    return handle, True


def _supervise_handle(
    backend: SessionBackend,
    row: _PageRow,
    key: tuple[int, str],
    handle: _ServerHandle,
    managed: dict[tuple[int, str], _ServerHandle],
    backoff: dict[tuple[int, str], float],
    occupants: dict[int, tuple[int, str | None]],
    shell_pids: dict[int, tuple[int, str]],
    now: float,
) -> None:
    """Health-check one page server and repair the matching shell as needed."""
    if now < backoff.get(key, 0.0) or now - handle.last_launch_monotonic < _SPAWN_VERIFY_TIMEOUT_S:
        return
    if _server_is_healthy(row.host, row.port, handle.token):
        backoff.pop(key, None)
        return
    if _probe_port(row.host, row.port) is None:
        try:
            _launch_in_session(backend, handle, row)
        except RuntimeError as exc:
            _log.error("[page-server] relaunch failed for %s: %s", key, exc)
            backoff[key] = now + _SPAWN_BACKOFF_S
            return
        handle.last_launch_monotonic = now
        backoff.pop(key, None)
        _log.warning("[page-server] relaunched server in session %s", handle.session_name)
        return
    occupant = occupants.get(row.port)
    if occupant is not None and _page_session_owner(occupant[0], shell_pids) == key:
        _log.warning("[page-server] replacing stale server in session %s", handle.session_name)
        _kill_page_session(backend, handle)
        del managed[key]
        backoff.pop(key, None)
        return
    _log.warning("[page-server] port %s is occupied by a foreign server; backing off", row.port)
    backoff[key] = now + _SPAWN_BACKOFF_S


def _reconcile_once(
    pool: ConnectionPool,
    managed: dict[tuple[int, str], _ServerHandle],
    backoff: dict[tuple[int, str], float],
    degraded: dict[tuple[int, str], _DegradedServeDir],
    host: str,
) -> None:
    """Reconcile page rows with durable page-shell sessions and their health.

    Newly registered rows are adopted FIRST — before the pass's slow
    housekeeping (process scans, per-row health probes) — so a serve() caller
    waits one poll (~2s), not one full pass. Liveness checks read the pty
    record store once in-process instead of asking the PTY backend per name
    (each has_session is a subprocess round-trip, ~0.25s; serialized over
    every open row it stretched a pass to tens of seconds)."""
    backend = get_shell_backend()
    rows = _open_rows(pool, host)
    wanted = {(row.agent_id, row.name): row for row in rows}
    _discard_gone_degraded(degraded, wanted)
    live = _live_session_records(backend)
    live_names = set(live) if live is not None else None
    now = time.monotonic()
    # Fast path: new/unadopted rows get their session now, before the slow
    # full-pass work. _ensure_handle's per-row snapshot re-check still guards
    # the stale-row race (a row closed or re-registered mid-pass is skipped).
    for key, row in wanted.items():
        if key not in managed:
            _ensure_handle(pool, backend, row, key, managed, backoff, degraded, now, live_names)
    # Re-scan: sessions created above must be visible to the liveness checks
    # below, or a stale scan would reap them as dead in this same pass.
    live = _live_session_records(backend)
    live_names = set(live) if live is not None else None
    occupants = _page_server_occupants()
    shell_pids = _page_session_shell_pids(wanted, live)
    _reclaim_occupants(rows, occupants, shell_pids)
    _drop_stale_handles(backend, wanted, managed, backoff, degraded, live_names)
    _close_persisted_sessions(backend, _closed_page_sessions(pool, host), live_names)
    now = time.monotonic()
    for key, row in wanted.items():
        handle = managed.get(key)
        if handle is None:
            # A handle dropped mid-pass (row changed / session died) is
            # re-created here, after the teardown work — same-pass recovery
            # the pre-fast-path loop always had.
            handle, _ = _ensure_handle(
                pool, backend, row, key, managed, backoff, degraded, now, live_names
            )
        if handle is not None:
            _supervise_handle(
                backend, row, key, handle, managed, backoff, occupants, shell_pids, now
            )


async def _reconcile_loop(pool: ConnectionPool, liveness: Liveness) -> None:
    """Run page-shell reconciliation at the configured poll interval."""
    host = reachable_host()
    managed: dict[tuple[int, str], _ServerHandle] = {}
    backoff: dict[tuple[int, str], float] = {}
    degraded: dict[tuple[int, str], _DegradedServeDir] = {}
    _log.info(
        "[page-server] daemon started, pid=%s, machine=%s, host=%s",
        os.getpid(),
        machine_name(),
        host,
    )
    while True:
        liveness.beat()
        try:
            await asyncio.sleep(_POLL_INTERVAL_S)
            await asyncio.to_thread(_reconcile_once, pool, managed, backoff, degraded, host)
        except asyncio.CancelledError:
            raise
        except psycopg.ProgrammingError:
            _log.critical(
                "[page-server] schema / syntax error — code<->DB drift; retry will not self-heal",
                exc_info=True,
            )
            raise
        except Exception:
            _log.exception("[page-server] poll iteration failed")


async def run() -> None:
    """Start the daemon and keep its health endpoint alive while it reconciles."""
    if _is_running():
        _log.info("[page-server] daemon already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)
    _write_pidfile()
    liveness = Liveness(_LIVENESS_TIMEOUT_S)
    health = await start_health_server("page_server", liveness=liveness)
    pool = shared.db.pool()
    try:
        await _reconcile_loop(pool, liveness)
    finally:
        await stop_health_server(health)
        pool.close()
        _remove_pidfile()


def main() -> None:
    """Initialize the daemon after verifying the database schema version."""
    from shared.migrations import assert_schema_current

    assert_schema_current(settings.data_plane.db_url)
    init_gateway_process(name="page_server")
    install_graceful_shutdown("page_server")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        _log.info("[page-server] interrupted, shutting down")
    except Exception:
        _log.exception("[page-server] daemon crashed — uncaught exception escaped run()")
        raise


if __name__ == "__main__":
    main()
