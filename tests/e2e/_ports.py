from __future__ import annotations

import atexit
import errno
import os
import socket
from pathlib import Path

RESERVED_PORT_BASE = 20000
RESERVED_PORT_STRIDE = 64
RESERVED_PORT_ATTEMPTS = 8
RESERVED_PORT_BLOCK = RESERVED_PORT_STRIDE * RESERVED_PORT_ATTEMPTS
HEALTH_PORT_ATTEMPTS = 32
HEALTH_PORT_BLOCK = RESERVED_PORT_STRIDE * HEALTH_PORT_ATTEMPTS
HEALTH_PORT_BASE = RESERVED_PORT_BASE + RESERVED_PORT_BLOCK
HEALTH_PORT_KINDS = ("ops", "agent-host")


def ephemeral_port_floor() -> int:
    try:
        return int(Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 49152


class PortReservation:
    def __init__(self, sock: socket.socket) -> None:
        self._sock: socket.socket | None = sock
        self.port: int = sock.getsockname()[1]

    def release(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            sock.close()


def reserve_port(
    slot: int,
    *,
    base: int = RESERVED_PORT_BASE,
    stride: int = RESERVED_PORT_STRIDE,
    attempts: int = RESERVED_PORT_ATTEMPTS,
) -> PortReservation:
    if not 0 <= slot < stride:
        raise ValueError(f"slot {slot} is outside [0, {stride})")
    floor = ephemeral_port_floor()
    top = base + (attempts - 1) * stride + slot
    if top >= floor:
        raise RuntimeError(
            f"reserved port block {base}..{top} reaches the kernel's ephemeral range (from {floor}); "
            "a port in it could be handed to another process before it is used"
        )
    for attempt in range(attempts):
        port = base + attempt * stride + slot
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            sock.close()
            if exc.errno != errno.EADDRINUSE:
                raise
            continue
        return PortReservation(sock)
    raise RuntimeError(
        f"every reserved port candidate for slot {slot} is in use: "
        f"{[base + n * stride + slot for n in range(attempts)]}"
    )


def reserve_health_port(kind: str) -> PortReservation:
    if kind not in HEALTH_PORT_KINDS:
        raise ValueError(f"unknown health-port kind {kind!r}; known: {HEALTH_PORT_KINDS}")
    base = HEALTH_PORT_BASE + HEALTH_PORT_KINDS.index(kind) * HEALTH_PORT_BLOCK
    return reserve_port(_worker_slot(), base=base, attempts=HEALTH_PORT_ATTEMPTS)


def _worker_slot() -> int:
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    slot = int(worker.removeprefix("gw")) + 1 if worker.startswith("gw") else 0
    return slot % RESERVED_PORT_STRIDE


GATEWAY_SOCKET = socket.socket()
GATEWAY_SOCKET.bind(("127.0.0.1", 0))
atexit.register(GATEWAY_SOCKET.close)
GATEWAY_PORT = GATEWAY_SOCKET.getsockname()[1]
GATEWAY_URL = f"http://localhost:{GATEWAY_PORT}"

FRONTEND_RESERVATION = reserve_port(_worker_slot())
atexit.register(FRONTEND_RESERVATION.release)
FRONTEND_PORT = FRONTEND_RESERVATION.port
FRONTEND_URL = f"http://localhost:{FRONTEND_PORT}"
