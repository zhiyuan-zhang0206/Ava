"""Page server supervisor daemon — doorplate ③ (R3).

The single supervisor of page servers: every open `agent_pages` row whose
serve_dir is an existing directory (ava.ui.serve()/serve_markdown() pages —
`show()` pages are the agent's own servers and are never touched) gets exactly
one page server process on this host, spawned from the row's serve_dir on the
row's port. A vanished serve_dir retries on a bounded degradation ladder before
the daemon closes the unusable row; rows that close (or agents that terminate —
the SQL trigger closes their pages) get their server killed.

The daemon is deliberately decoupled from the agent heartbeat and the
session tree: page servers are detached subprocesses (`start_new_session` —
a new session, outside the daemon's process group), so a
rollout's session-tree rebuild does not kill them and an agent restart does
not orphan them. The truth source is the `agent_pages` table; the managed
process table here is only a cache (invariant 3).

Scope: rows whose host equals this host's reachable address — on a
multi-runner deployment each runner supervises its own agents' pages; on
the single box that is every row.

Usage:
    .venv/bin/python -m services.page_server.daemon

Kept alive via `services/healthchecks/page_server.py` from the agent-runner
watchdog.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import subprocess
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
from services._pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.page_server.degradation import (
    _DegradedServeDir,
    _discard_gone_degraded,
    _PageRow,
    _reconcile_serve_dir,
)
from shared.config import settings
from shared.daemon_health import Liveness, health_port, start_health_server, stop_health_server
from shared.daemon_shutdown import install_graceful_shutdown
from shared.log import init_gateway_process
from shared.machine import machine_name, reachable_host
from shared.paths import legacy_pid_path

_log = logging.getLogger("services.page_server.daemon")

_POLL_INTERVAL_S = settings.daemon.page_server_poll_interval_seconds
_LIVENESS_TIMEOUT_S = 60.0
_PIDFILE = settings.services.page_server_pidfile

# Spawn verification window: the server must answer /health with OUR token
# within this long, else the launch failed (port occupied by a stale
# occupant, bind error, ...) and we kill it and retry next round.
_SPAWN_VERIFY_TIMEOUT_S = 5.0
# Cooldown after a failed spawn: do not hot-loop a broken row every poll.
_SPAWN_BACKOFF_S = 30.0


@dataclass
class _ServerHandle:
    """One managed page server process + the token that proves it is ours."""

    agent_id: int
    name: str
    port: int
    serve_dir: str
    token: str
    proc: subprocess.Popen[bytes]
    log_path: Path


def _write_pidfile() -> None:
    if not acquire_pidfile(_PIDFILE, "services.page_server.daemon"):
        _log.info("[page_server] daemon already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)


def _remove_pidfile() -> None:
    remove_pidfile(_PIDFILE)


def _is_running() -> bool:
    """Whether a daemon is already running (via pidfile, new + legacy paths).

    Pid-reuse-safe: a live pid whose argv does not name this daemon's module
    is a recycled pid, not a running instance (audit round 2, P1)."""
    return pidfile_holds_daemon(_PIDFILE, "services.page_server.daemon") or pidfile_holds_daemon(
        legacy_pid_path("page_server"), "services.page_server.daemon"
    )


def _open_rows(pool: ConnectionPool, host: str) -> list[_PageRow]:
    """Every row this daemon supervises: open, serve_dir set, on this host."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, agent_id, name, port, host, serve_dir FROM agent_pages "
            "WHERE closed_at IS NULL AND serve_dir IS NOT NULL AND host = %s "
            "ORDER BY agent_id, name",
            (host,),
        )
        return [_PageRow(*row) for row in cur.fetchall()]


def _server_is_healthy(host: str, port: int, token: str) -> bool:
    """Whether the server on (host, port) answers /health with `token`."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=1.0) as resp:
            return resp.read() == f"ok:{token}".encode()
    except OSError:
        return False


def _spawn_server(row: _PageRow, log_dir: Path) -> _ServerHandle:
    """Start the page server for `row` as a detached subprocess (new
    session — outside the session tree, so a rollout rebuild does not kill
    it) and verify it answers /health with our token.

    Raises RuntimeError when the server does not come up within the
    verification window; the caller kills the half-started process and
    backs off.
    """
    token = secrets.token_hex(16)
    log_path = log_dir / f"page-{row.agent_id}-{row.name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    # The token rides the child env, never argv (audit round-2 security P2-4):
    # "secrets never ride argv" is R2's rule — anything on argv is visible to
    # any local user via `ps`. env is inherited + one extra key; the child is
    # ours, so inheriting the daemon env is the same trust as today.
    child_env = {**os.environ, "PAGE_SERVER_TOKEN": token}
    with log_path.open("ab") as logf:
        proc = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-m",
                "services.page_server.server",
                "--port",
                str(row.port),
                "--host",
                row.host,
                "--dir",
                row.serve_dir,
            ],
            cwd=project_root,
            env=child_env,
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # POSIX: new session — detached from the tree
        )
    handle = _ServerHandle(
        agent_id=row.agent_id,
        name=row.name,
        port=row.port,
        serve_dir=row.serve_dir,
        token=token,
        proc=proc,
        log_path=log_path,
    )
    deadline = time.monotonic() + _SPAWN_VERIFY_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"page server exited early (code {proc.returncode}); log: {log_path}"
            )
        if _server_is_healthy(row.host, row.port, token):
            return handle
        time.sleep(0.1)
    # Verification window expired — kill the half-started process ourselves.
    # The caller only backs off; without this kill every 30s leak window left
    # a new half-started python process behind (audit round 2, P1-3).
    with suppress(OSError):
        proc.kill()
        proc.wait(timeout=2.0)
    raise RuntimeError(
        f"page server did not come up within {_SPAWN_VERIFY_TIMEOUT_S}s; log: {log_path}"
    )


def _kill_server(handle: _ServerHandle) -> None:
    """Terminate a managed server: SIGTERM, escalate to SIGKILL after 2s."""
    if handle.proc.poll() is None:
        handle.proc.terminate()
        try:
            handle.proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            handle.proc.kill()
            try:
                handle.proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                # Extreme case — even SIGKILL did not reap it in 2s. Log and
                # move on; the next reconcile round retries (audit P3: this
                # used to raise and skip the managed-dict cleanup).
                _log.warning(
                    "[page-server] server pid=%s did not die after SIGKILL within 2s",
                    handle.proc.pid,
                )


_PAGE_SERVER_MODULE = "services.page_server.server"


def _page_server_occupants() -> dict[int, tuple[int, str | None]]:
    """port -> (pid, ava_home) for every local page-server process.

    Positive identification by argv (the browser/orphan.py pattern — never
    select by exclusion): a process is a page server only when its command
    line names the module. Unreadable processes are skipped, not guessed.

    ``ava_home`` is the cluster identity of the process that spawned it
    (a cluster's identity IS its home path): the reclaim pass must never
    reap a co-located second cluster's pages as orphans (#1129,
    2026-08-10 — two units share one box's process table and the argv-only
    scan could not tell them apart, so preview's daemon killed main's
    pages in an endless respawn loop). Unreadable env is ``None`` and the
    caller treats it as "not provably ours" — skip, never guess.
    """
    found: dict[int, tuple[int, str | None]] = {}
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            # psutil's .info is untyped — normalize to plain str/int so
            # pyright (strict) sees concrete types.
            raw_cmdline = proc.info.get("cmdline")
            cmdline = [str(c) for c in raw_cmdline] if raw_cmdline else []
            pid = int(proc.info["pid"])
        except (psutil.Error, OSError, TypeError, ValueError):
            continue
        if _PAGE_SERVER_MODULE not in " ".join(cmdline):
            continue
        m = re.search(r"--port (\d+)", " ".join(cmdline))
        if m is None:
            continue
        try:
            env = proc.environ()
        except (psutil.Error, OSError):
            env = {}
        found[int(m.group(1))] = (pid, env.get("AVA_HOME"))
    return found


def _kill_pid(pid: int) -> None:
    """Terminate a stray page-server process: SIGTERM, SIGKILL after 2s."""
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


def _reconcile_once(
    pool: ConnectionPool,
    managed: dict[tuple[int, str], _ServerHandle],
    backoff: dict[tuple[int, str], float],
    degraded: dict[tuple[int, str], _DegradedServeDir],
    host: str,
    log_dir: Path,
) -> None:
    """One supervision pass: spawn what is missing, kill what is gone,
    respawn what died. The agent_pages table is the truth source; `managed`
    is only the cache of what this process already spawned; `backoff`
    remembers ordinary launch failures, while `degraded` remembers missing
    directories on their independent exponential retry ladder."""
    rows = _open_rows(pool, host)
    wanted = {(r.agent_id, r.name): r for r in rows}
    _discard_gone_degraded(degraded, wanted)

    # 0. Reclaim (audit round 2, P1): page servers are detached processes
    # (start_new_session) that survive a daemon restart, while `managed` is
    # per-process memory — after a restart the daemon cannot adopt them and
    # the old code respawned into their ports forever (30s backoff loop),
    # and servers of since-closed rows leaked permanently ("kill what is
    # gone" had no handle). Identify occupants positively by argv and kill
    # them; the spawn step below rebuilds wanted ones with a fresh token.
    # Own processes (in `managed`) are never touched.
    managed_pids = {h.proc.pid for h in managed.values() if h.proc.poll() is None}
    own_home = str(settings.general.ava_home)
    for port, (pid, home) in _page_server_occupants().items():
        if pid in managed_pids:
            continue
        if home != own_home:
            # Foreign cluster (different home) OR unreadable env (None) — the
            # latter is "not provably ours", skip, never guess (#1129's
            # documented contract; the None branch previously fell through and
            # reaped a co-located cluster's pages whose env was unreadable —
            # Task #1141, 2026-08-10).
            _log.info(
                "[page-server] skipping port %s pid=%s (foreign cluster home %s)",
                port,
                pid,
                home,
            )
            continue
        if port in {r.port for r in rows}:
            _log.info(
                "[page-server] reclaiming port %s: killing stale occupant pid=%s (daemon restart)",
                port,
                pid,
            )
        else:
            _log.info(
                "[page-server] closing port %s: killing orphaned page server pid=%s (row gone)",
                port,
                pid,
            )
        _kill_pid(pid)

    # 1. Kill servers whose row is gone or changed (closed / agent
    # terminated / re-registered on another port or dir).
    for key in list(managed):
        row = wanted.get(key)
        handle = managed[key]
        if row is None:
            _log.info("[page-server] closing %s (row gone)", key)
            _kill_server(handle)
            del managed[key]
        elif row.port != handle.port or row.serve_dir != handle.serve_dir:
            _log.info("[page-server] replacing %s (port/dir changed)", key)
            _kill_server(handle)
            del managed[key]
            backoff.pop(key, None)
            degraded.pop(key, None)
        elif handle.proc.poll() is not None:
            _log.warning(
                "[page-server] respawning %s (process died, code=%s)", key, handle.proc.returncode
            )
            _kill_server(handle)
            del managed[key]
            backoff.pop(key, None)
            degraded.pop(key, None)

    # 2. Spawn what is missing. A vanished directory never gets as far as
    # Popen: it has its own degrade/recovery ladder, distinct from failures
    # such as a busy port that retain the fixed failed-spawn cooldown.
    now = time.monotonic()
    for key, row in wanted.items():
        if key in managed:
            continue
        if _reconcile_serve_dir(pool, row, key, degraded, backoff, now):
            continue
        if now < backoff.get(key, 0.0):
            continue
        try:
            handle = _spawn_server(row, log_dir)
        except RuntimeError as e:
            _log.error("[page-server] spawn failed for %s: %s", key, e)
            backoff[key] = now + _SPAWN_BACKOFF_S
            continue
        _log.info(
            "[page-server] spawned %s pid=%s port=%s dir=%s",
            key,
            handle.proc.pid,
            row.port,
            row.serve_dir,
        )
        managed[key] = handle
        backoff.pop(key, None)


async def _reconcile_loop(pool: ConnectionPool, liveness: Liveness) -> None:
    """Main loop: every poll interval, reconcile this host's page servers."""
    host = reachable_host()
    log_dir = settings.services.page_server_log_dir
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
            await asyncio.to_thread(
                _reconcile_once, pool, managed, backoff, degraded, host, log_dir
            )
        except asyncio.CancelledError:
            raise
        except psycopg.ProgrammingError:
            _log.critical(
                "[page-server] schema / syntax error — code<->DB drift; retry will not "
                "self-heal, daemon exiting, restart after fix",
                exc_info=True,
            )
            raise
        except Exception:
            _log.exception("[page-server] poll iteration failed")


async def run() -> None:
    """Start the daemon: pidfile -> healthz -> DB pool -> main loop."""
    if _is_running():
        _log.info("[page-server] daemon already running (pidfile=%s), exiting", _PIDFILE)
        sys.exit(1)

    _write_pidfile()
    _log.info("[page-server] pidfile written: %s", _PIDFILE)

    liveness = Liveness(_LIVENESS_TIMEOUT_S)
    health = await start_health_server("page_server", liveness=liveness)
    _log.info("[page-server] healthz listening on :%s", health_port("page_server"))

    pool = shared.db.pool()
    try:
        await _reconcile_loop(pool, liveness)
    finally:
        await stop_health_server(health)
        pool.close()
        _remove_pidfile()


def main() -> None:
    """Entry point: init logger + run asyncio loop.

    Schema pre-check first, like every other daemon: without it a schema
    drift left the daemon alive but failing every poll iteration, invisible
    to the watchdog (audit round 2, P1)."""
    from shared.migrations import assert_schema_current

    # Pre-startup sanity: schema version must match code; raises SchemaVersionMismatch if not.
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
