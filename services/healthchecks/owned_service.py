"""Readiness for services whose protocol has no native Ava identity payload."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from pathlib import Path

import psutil

from shared.daemon_health import DaemonProbe
from shared.native_process.ownership import OwnedProcess, capture_tree
from shared.root_control.client import RootClientError, owned_process, peer_pid


def _owned_ping(resolve_owner: Callable[[], OwnedProcess | None], path: Path | str) -> DaemonProbe:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(3)
        sock.connect(str(path))
        owner = resolve_owner()
        if owner is None:
            return DaemonProbe.port_taken("Unix endpoint has no root-owned generation")
        members = {identity.pid: identity for identity in capture_tree(owner)}
        peer = members.get(peer_pid(sock))
        if peer is None or not peer.live():
            return DaemonProbe.port_taken("Unix endpoint belongs to a different generation")
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
            return DaemonProbe.down("owned Unix endpoint rejected ping")
        if not owner.live() or not peer.live():
            return DaemonProbe.down("generation exited during ping")
        return DaemonProbe.up("root-owned Unix peer answered ping")


def listener_pids(port: int) -> set[int]:
    """Observe TCP listeners without requiring a privileged system-wide query."""
    listeners: set[int] = set()
    for process in psutil.process_iter():
        try:
            connections = process.net_connections(kind="tcp")
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        if any(c.status == psutil.CONN_LISTEN and c.laddr.port == port for c in connections):
            listeners.add(process.pid)
    return listeners


def absent_listener(port: int) -> DaemonProbe:
    """Separate a refused connection from an endpoint native inspection missed."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return DaemonProbe.unavailable(
                f"port {port} accepts connections but its owner is unobservable"
            )
    except ConnectionRefusedError:
        return DaemonProbe.down(f"no listener on port {port}")
    except OSError as exc:
        return DaemonProbe.unavailable(f"cannot establish port {port} absence: {exc}")


def _owned_tcp(
    owner: OwnedProcess, port: int, protocol: Callable[[], bool | DaemonProbe]
) -> DaemonProbe:
    members = {identity.pid: identity for identity in capture_tree(owner)}
    # A permission failure observing our own generation is unknown, never DOWN.
    for identity in members.values():
        psutil.Process(identity.pid).net_connections(kind="tcp")
    listeners = listener_pids(port)
    if not listeners:
        return absent_listener(port)
    if listeners - members.keys():
        return DaemonProbe.port_taken(
            f"port {port} has a listener outside the root-owned generation"
        )
    result = protocol()
    if (
        listener_pids(port) != listeners
        or not owner.live()
        or any(not members[pid].live() for pid in listeners)
    ):
        return DaemonProbe.down("listener generation changed during the protocol probe")
    if isinstance(result, DaemonProbe):
        return result
    return (
        DaemonProbe.up("root-owned listener passed its protocol probe")
        if result
        else DaemonProbe.down("root-owned listener failed its protocol probe")
    )


def probe_endpoint(
    service: str, port: int, protocol: Callable[[], bool | DaemonProbe]
) -> DaemonProbe:
    """Bind a protocol result to its current root generation, including first start."""
    try:
        if not listener_pids(port):
            return absent_listener(port)
        owner = owned_process(service)
        if owner is None:
            return DaemonProbe.port_taken(f"port {port} has no root-owned generation")
        return _owned_tcp(owner, port, protocol)
    except (RootClientError, psutil.Error, RuntimeError) as exc:
        return DaemonProbe.unavailable(f"cannot establish service ownership: {exc}")


def _observe(service: str) -> DaemonProbe:
    from shared.config import settings
    from shared.paths import chrome_mcp_socket, computer_mcp_socket, mcp_daemon_shared_socket

    sockets = {
        "browser-mcp": chrome_mcp_socket,
        "computer-mcp": computer_mcp_socket,
        "mcp-daemon": mcp_daemon_shared_socket,
    }
    if service in sockets:
        return _owned_ping(lambda: owned_process(service), sockets[service]())
    if service == "milvus":
        from services.healthchecks.milvus import _is_alive

        return probe_endpoint(service, settings.services.milvus_port, _is_alive)
    if service == "memory-search":
        from services.healthchecks.memory_search import _probe

        return probe_endpoint(service, settings.services.memory_search_port, _probe)
    raise ValueError(f"unsupported root-owned protocol probe: {service}")


def probe(service: str) -> DaemonProbe:
    """Require captured root ownership and an actual application response."""
    try:
        return _observe(service)
    except (RootClientError, psutil.Error, RuntimeError) as exc:
        return DaemonProbe.unavailable(f"cannot establish service ownership: {exc}")
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return DaemonProbe.down(f"protocol probe failed: {exc}")
