"""Frontend healthcheck — run by the agent-runner watchdog every 60s.

Checks whether the Next.js prod build server (port 3000) is alive:
- `curl -fs http://localhost:3000` returns 2xx -> no-op
- returns non-2xx / connection refused -> kill the ava-frontend session
  session + restart `npm run build && exec npm run start -p 3001`

Frontend differs from other services — `npm run start` exposes no PID
hook, so probing goes through HTTP curl rather than pidfile + kill -0.
Restart goes through the session backend: kill first, then launch running
build + start (build is slow, ~30-60s).

Usage (watchdog daemon imports and runs this every 60s; no longer cron):
    .venv/bin/python -m services.healthchecks.frontend
"""

import logging
import subprocess
import sys
from pathlib import Path

import psutil

from shared.cluster import frontend_service_cmd, session_name
from shared.config import settings
from shared.daemon_health import DaemonProbe
from shared.log import init_gateway_process
from shared.service_respawn import respawn_service

_log = logging.getLogger("services.healthchecks.frontend")


def _app_port() -> int:
    """The Next.js app port (AVA_APP_PORT, default entry+1) — NOT the entry
    port: the entry is owned by the always-up gate, which answers 200 even
    while the app is down. Probing the entry would make a dead app look
    alive, and probing a port answered by an old orphan would make it look
    alive too (issue #2123)."""
    from urllib.parse import urlsplit

    entry = urlsplit(settings.services.frontend_healthcheck_url).port or 3000
    return settings.services.app_port or (entry + 1)


def _app_url() -> str:
    """The Next.js app URL the healthcheck probes and respawns."""
    return f"http://localhost:{_app_port()}"


_FRONTEND_URL = _app_url()


def _session_name() -> str:
    """Compose the frontend session name for this host."""
    return session_name("frontend")


def _listener_pids(port: int) -> set[int]:
    """PIDs of the processes with a LISTEN socket on `port`."""
    pids: set[int] = set()
    for proc in psutil.process_iter():
        try:
            connections = proc.net_connections(kind="tcp")
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        for conn in connections:
            if conn.status == psutil.CONN_LISTEN and conn.laddr.port == port:
                pids.add(proc.pid)
    return pids


def _session_owns_listener(port: int) -> bool:
    """Whether the recorded frontend session owns a LISTEN socket on `port`.

    A bare HTTP 200 is not frontend health: the answering process must be the
    current session's leader or one of its descendants (birth-validated). An
    old orphan that answers the port after its own session record is gone
    fails this check (issue #2123).
    """
    from shared.paths import run_dir
    from shared.proc_tree import session_owns_pids
    from shared.session_record import SessionRecord

    record = SessionRecord.read(run_dir() / "sessions" / f"{_session_name()}.json")
    if record is None:
        return False
    return session_owns_pids(record, _listener_pids(port))


def _http_ok() -> bool:
    """curl -fs probe; `-f` makes non-2xx exit non-zero, `-s` is silent."""
    try:
        result = subprocess.run(
            ["curl", "-fs", "-o", "/dev/null", _FRONTEND_URL],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _is_alive() -> bool:
    """Identity-bound liveness: 2xx AND the answering listener belongs to the
    current frontend session. An anonymous 200 (old orphan, gate proxy) does
    not count."""
    return _http_ok() and _session_owns_listener(_app_port())


def probe_frontend() -> DaemonProbe:
    """The `ava status` / start-path identity probe for the frontend.

    Same identity question the watchdog asks, with the verdict granularity a
    human reading a status row needs: ALIVE only when the session owns the
    app-port listener and it answers 2xx; PORT_TAKEN when the port is answered
    by something outside the current session (an old orphan — respawn cannot
    evict it); DOWN otherwise.
    """
    port = _app_port()
    listeners = sorted(_listener_pids(port))
    if _is_alive():
        return DaemonProbe.up(f"frontend session owns the {port} listener and it answers 2xx")
    if listeners:
        return DaemonProbe.port_taken(
            f"port {port} is answered by pid(s) {listeners} outside the current "
            "frontend session — an old orphan's 200 is not frontend health"
        )
    return DaemonProbe.down(f"no frontend listener on {port}")


def _session_exists() -> bool:
    """Check whether the frontend session exists (may be in build).

    Delegates to the session backend — the native supervisor on POSIX, winproc
    on Windows: the same name-keyed backend respawn_service launches into
    (the frontend is a native service session since S6).
    Without this the build-in-progress gate in main() would always read "no
    session" and kill-restart mid-build every tick.
    """
    from shared.session_backend import get_backend

    return get_backend().has_session(_session_name())


def _restart() -> bool:
    """kill old session + start a new one running build + start (cwd = repo/ui/web/)."""
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    frontend_dir = project_root / "ui" / "web"
    from shared.runtime_interpreter import WHEEL_RUNTIME, runtime_frontend_dir, runtime_venv

    if WHEEL_RUNTIME:
        project_root = runtime_venv().parent
        frontend_dir = runtime_frontend_dir() / "server"
    if not frontend_dir.is_dir():
        _log.error("[frontend healthcheck] %s does not exist", frontend_dir)
        return False

    from urllib.parse import urlsplit

    port = urlsplit(_FRONTEND_URL).port or 3000
    # Both platforms route through respawn_service (the shared service-respawn
    # helper): it kills any stale session — on both backends during the
    # legacy->native transition — and launches through the session backend.
    # Single source for the launch command: shared.cluster.frontend_service_cmd
    # builds the SAME string as `ava start`'s ServiceSpec (ops/spec.py), so a
    # watchdog restart can never drift from the canonical command (the 2026-08-27
    # prod outage: the respawn's missing `exec` made the session validator reject
    # the command, so a dead frontend could never self-heal). `exec` on the serve
    # stage hands the shell's pid to `npm run start`; `-p <port>` binds the
    # cluster's allocated frontend port (Next.js defaults to 3000 otherwise — a
    # watchdog restart would silently revert off-cluster); the NEXT_PUBLIC_*
    # build-env prefix rides the command so a restart can never bake a stale
    # gateway port into the bundle.
    cmd = frontend_service_cmd(port, frontend_dir)
    # The session starts in ui/web/ (npm must run there), but the code it runs
    # belongs to the checkout above it — which is what the launch-site guard
    # judges. Passing the subdirectory as both made the guard compare
    # `<checkout>/ui/web` against the prod home's anchored checkout, so it read
    # every legitimate prod restart as a dev checkout and refused it: the
    # frontend was the one service that could never self-heal.
    return respawn_service(
        "frontend",
        cmd,
        frontend_dir,
        checkout=project_root,
        extra_env={"AVA_PROCESS_PROFILE": "gateway"},
    )


def main() -> None:
    # Raise the fd ceiling: this daemon and its respawns may run under a
    # launchd-256 chain; the respawn helper raises again before spawning.
    from shared.platform import raise_fd_limit

    raise_fd_limit(65536)
    init_gateway_process(name="frontend-healthcheck")

    if _is_alive():
        _log.debug("[frontend healthcheck] frontend alive, no-op")
        return

    # Session present but curl unreachable = most likely build in
    # progress or just-started not yet bound to the port (`npm run
    # build && exec npm run start` first time ~30-60s). Cron ticks every
    # minute; without this gate we would kill-session mid-build and
    # fall into an infinite restart loop. Trade-off: if the session is
    # truly hung (process there but hanging) this healthcheck also
    # skips, and the human fallback is killing the ava-frontend session's
    # pid. Hang is rarer than build window; the trade-off is
    # accepted.
    if _session_exists():
        _log.info(
            "[frontend healthcheck] session present but app not answering, "
            "assume build / startup in progress, skip restart"
        )
        return

    # No session AND the app port is answered: an orphan outside any live
    # session (issue #2123). Respawning cannot evict it — the new session's
    # `next start` would walk into EADDRINUSE and die, restarting this loop
    # forever. Refuse loudly; the operator reaps the orphan.
    listeners = _listener_pids(_app_port())
    if listeners:
        _log.error(
            "[frontend healthcheck] app port %s is answered by pid(s) %s outside "
            "the frontend session — refusing to respawn into an occupied port; "
            "reap the orphan first",
            _app_port(),
            sorted(listeners),
        )
        return

    _log.info("[frontend healthcheck] frontend dead (no session), restarting...")
    if _restart():
        _log.info("[frontend healthcheck] frontend restart launched (build ~30-60s)")
    else:
        _log.error("[frontend healthcheck] frontend restart FAILED — manual intervention needed")
        sys.exit(1)


if __name__ == "__main__":
    main()
