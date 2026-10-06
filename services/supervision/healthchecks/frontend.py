"""Frontend application readiness. Service recovery belongs to ava-root."""

import subprocess

from base.config import settings
from base.daemon.health import DaemonProbe


def _app_port() -> int:
    """The Next.js app port (AVA_APP_PORT, default entry+1) — NOT the entry
    port: the entry is owned by the always-up gate, which answers 200 even
    while the app is down. Probing the entry would make a dead app look
    alive."""
    from urllib.parse import urlsplit

    entry = urlsplit(settings.services.frontend_healthcheck_url).port or 3000
    return settings.services.app_port or (entry + 1)


def _app_url() -> str:
    """The Next.js application endpoint, behind the entry gate."""
    return f"http://localhost:{_app_port()}"


def _http_ok() -> bool:
    """curl -fs probe; `-f` makes non-2xx exit non-zero, `-s` is silent."""
    try:
        result = subprocess.run(
            ["curl", "-fs", "-o", "/dev/null", _app_url()],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def probe_frontend() -> DaemonProbe:
    """Probe the application behind the entry gate."""
    port = _app_port()
    if _http_ok():
        return DaemonProbe.up(f"frontend application on {port} answers HTTP")
    return DaemonProbe.down(f"frontend application on {port} is not ready")
