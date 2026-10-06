"""Bounded application protocol observations; lifecycle owners handle recovery."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from pathlib import Path

from base.daemon.health import DaemonProbe


def ping(path: Path | str) -> DaemonProbe:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(3)
        sock.connect(str(path))
        sock.sendall(b'{"id":0,"method":"ping","agent_id":null}\n')
        response = bytearray()
        while b"\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                return DaemonProbe.down("ping stream closed before a response")
            response.extend(chunk)
            if len(response) > 65536:
                return DaemonProbe.down("ping response exceeds the protocol limit")
        payload = json.loads(response.split(b"\n", 1)[0])
        if payload["id"] != 0 or payload["ok"] is not True:
            return DaemonProbe.down("Unix endpoint rejected ping")
        return DaemonProbe.up("Unix endpoint answered ping")


def probe_protocol(protocol: Callable[[], bool | DaemonProbe]) -> DaemonProbe:
    """Report the protocol result without native process or listener inspection."""
    result = protocol()
    if isinstance(result, DaemonProbe):
        return result
    return DaemonProbe.up("protocol answered") if result else DaemonProbe.down("protocol failed")


def _observe(service: str) -> DaemonProbe:
    from base.paths import chrome_mcp_socket, computer_mcp_socket, mcp_daemon_shared_socket
    from base.sessions.pty.paths import service_socket_path

    sockets = {
        "browser-mcp": chrome_mcp_socket,
        "computer-mcp": computer_mcp_socket,
        "mcp-daemon": mcp_daemon_shared_socket,
        "pty-sessions": service_socket_path,
    }
    if service in sockets:
        return ping(sockets[service]())
    if service == "memory-search":
        from services.supervision.healthchecks.memory_search import _probe

        return probe_protocol(_probe)
    raise ValueError(f"unsupported protocol probe: {service}")


def probe(service: str) -> DaemonProbe:
    """Observe the configured endpoint through its application protocol."""
    try:
        return _observe(service)
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return DaemonProbe.down(f"protocol probe failed: {exc}")
