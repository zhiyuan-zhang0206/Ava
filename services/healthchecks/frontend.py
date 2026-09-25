"""Identity-bound frontend readiness. Service recovery belongs to ava-root."""

import subprocess

import psutil

from services.ava_root.client import RootClientError, owned_process
from services.healthchecks.owned_service import absent_listener
from services.healthchecks.owned_service import listener_pids as _listener_pids
from shared.config import settings
from shared.daemon_health import DaemonProbe
from shared.proc_tree import OwnedProcess, capture_tree


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
    """The Next.js application endpoint, behind the entry gate."""
    return f"http://localhost:{_app_port()}"


_FRONTEND_URL = _app_url()


def _expected_owner() -> OwnedProcess | None:
    return owned_process("frontend")


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


def probe_frontend() -> DaemonProbe:
    """Distinguish owned readiness failure from an unrelated listener."""
    port = _app_port()
    try:
        listeners = _listener_pids(port)
        if not listeners:
            return absent_listener(port)
        owner = _expected_owner()
        members = {} if owner is None else {p.pid: p for p in capture_tree(owner)}
    except (RootClientError, psutil.Error, RuntimeError) as exc:
        return DaemonProbe.unavailable(f"frontend ownership unavailable: {exc}")
    if owner is not None and listeners <= members.keys():
        if (
            _http_ok()
            and _listener_pids(port) == listeners
            and owner.live()
            and all(members[p].live() for p in listeners)
        ):
            return DaemonProbe.up(f"frontend owns the {port} listener and it answers 2xx")
        return DaemonProbe.down(f"frontend owns the {port} listener but is not ready")
    return DaemonProbe.port_taken(
        f"port {port} is answered by pid(s) {sorted(listeners)} outside the frontend's "
        "expected owner — an old orphan's 200 is not frontend health"
    )
