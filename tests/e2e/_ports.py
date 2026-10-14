"""e2e ports -- dynamically acquire free ports from the kernel.

The gateway socket stays bound in the pytest worker through frontend build and
every gateway restart. Uvicorn inherits its descriptor; no bind-close-rebind gap.
Next has no inherited socket interface here, so the frontend port is a reservation
instead: a port from a block below the kernel's ephemeral range (nothing the kernel
allocates can take it), one candidate per xdist worker slot (workers never share
one), held by a bound socket through the frontend build and released only right
before `next start` is spawned (`reserve_port`, below).

`*_PORT` / `*_URL` are allocated once at module import time; within the same worker
process, import order is irrelevant, each test file gets the same set. CI runs e2e
serially with `-n 1`: a single dedicated box runs one complete stack at a time --
for resource determinism (one stack owns the CPU), not to avoid port clashes (under
execnet, each xdist worker is a separate process, each re-importing to get its own
kernel-allocated ports).
"""

from __future__ import annotations

import atexit
import errno
import os
import socket
from pathlib import Path

# The ports `reserve_port` hands out come from this block. It sits below the kernel's
# ephemeral range, so no `bind(0)` and no outgoing connection's source port can ever be the
# number a reservation holds; the span covers one candidate per `attempt` for every slot.
RESERVED_PORT_BASE = 20000
RESERVED_PORT_STRIDE = 64
RESERVED_PORT_ATTEMPTS = 8


def ephemeral_port_floor() -> int:
    """The lowest port the kernel may pick for a `bind(0)` or an outgoing connection.

    Linux publishes it in /proc; elsewhere (macOS, BSD) the IANA dynamic range starts at 49152."""
    try:
        return int(Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 49152


class PortReservation:
    """A localhost TCP port kept by a bound, never listening socket until `release()`.

    While it is held nothing else can bind the port, including a listener that sets
    SO_REUSEADDR the way Node does. `release()` is the handoff: close it right before the
    process that is meant to listen on the port is spawned."""

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
    """Hold a port for `slot` that nothing the kernel allocates can collide with.

    `free_port` and its kin take a number from the kernel's ephemeral range and close it, so
    between that and the real bind the number can go to any other process on the host: another
    xdist worker's `bind(0)` or health-port pool, or the source port of any outgoing connection.
    A frontend that lost this race died with EADDRINUSE and every browser test behind it failed.
    Here the candidates are `base + attempt * stride + slot`, in a block below the ephemeral
    range, so the kernel never hands them out, and different slots never share a candidate. The
    socket stays bound until `release()`, so a concurrent run that probes the same candidate
    sees it taken and moves to its next one.

    Raises:
        ValueError: `slot` is outside `[0, stride)`.
        RuntimeError: the block reaches into the ephemeral range, or every candidate is taken.
    """
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


def _worker_slot() -> int:
    """0 for the controller or a serial run, `n + 1` for xdist worker `gw<n>`.

    Past `RESERVED_PORT_STRIDE` workers the slots wrap; two workers sharing a slot then
    take different candidates because the reservation of the first is held."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    slot = int(worker.removeprefix("gw")) + 1 if worker.startswith("gw") else 0
    return slot % RESERVED_PORT_STRIDE


GATEWAY_SOCKET = socket.socket()
GATEWAY_SOCKET.bind(("127.0.0.1", 0))
atexit.register(GATEWAY_SOCKET.close)
GATEWAY_PORT = GATEWAY_SOCKET.getsockname()[1]
# `localhost`, like the frontend and like a started cluster's own gateway URL: the
# gateway authenticates the browser with a SameSite=Lax session cookie, which a page
# on `localhost` sends to another `localhost` port (same site) and not to `127.0.0.1`.
# The retained listener binds 127.0.0.1, which is what `localhost` resolves to for IPv4.
GATEWAY_URL = f"http://localhost:{GATEWAY_PORT}"

# Playwright navigates on `localhost` to align with next dev's `Local:` line.
# Gateway CORS covers both localhost / 127.0.0.1 sets.
FRONTEND_RESERVATION = reserve_port(_worker_slot())
atexit.register(FRONTEND_RESERVATION.release)
FRONTEND_PORT = FRONTEND_RESERVATION.port
FRONTEND_URL = f"http://localhost:{FRONTEND_PORT}"
