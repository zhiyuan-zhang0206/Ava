"""Gate protocol readiness; the roster binds the listener to root custody."""

import json
import urllib.error
import urllib.request
from typing import cast

from services.gate.daemon import entry_port
from shared.daemon_health import DaemonProbe
from shared.paths import ava_home


def probe() -> DaemonProbe:
    """A Gate response proves its own readiness, independent of app availability."""
    url = f"http://127.0.0.1:{entry_port()}/__ava/healthz"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            payload = json.loads(response.read(4096))
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return DaemonProbe.down(f"gate protocol unavailable: {exc}")
    if not isinstance(payload, dict):
        return DaemonProbe.down("gate health endpoint returned an invalid identity")
    payload = cast("dict[str, object]", payload)
    if payload.get("name") != "gate":
        return DaemonProbe.down("gate health endpoint returned an invalid identity")
    if payload.get("home") != str(ava_home()):
        return DaemonProbe.port_taken("gate health endpoint belongs to another home")
    return DaemonProbe.up("gate health endpoint is ready")
