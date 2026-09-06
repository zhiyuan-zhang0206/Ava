"""Exercise forwarding over real sockets, including the real connect timeout."""

from __future__ import annotations

import socket
import struct
import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

import pytest

from services.redis_bridge import relay


@pytest.fixture
def connection() -> Iterator[tuple[socket.socket, socket.socket, threading.Thread]]:
    client, relay_client = socket.socketpair()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3.0)
        handler = threading.Thread(
            target=relay._handle,
            args=(relay_client, ("127.0.0.1", 12345), listener.getsockname()),
            daemon=True,
        )
        handler.start()
        backend, _ = listener.accept()
    client.settimeout(3.0)
    backend.settimeout(3.0)
    try:
        yield client, backend, handler
    finally:
        for endpoint in (client, backend):
            with suppress(OSError):
                endpoint.shutdown(socket.SHUT_RDWR)
            endpoint.close()
        handler.join(timeout=3.0)
        assert not handler.is_alive(), "relay must reclaim both pumps on peer closure"
        assert relay_client.fileno() == -1


def _read_to_eof(endpoint: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while chunk := endpoint.recv(65536):
        chunks.append(chunk)
    return b"".join(chunks)


def test_idle_connection_survives_the_five_second_connect_deadline(
    connection: tuple[socket.socket, socket.socket, threading.Thread],
) -> None:
    client, backend, handler = connection
    backend.sendall(b"ready")
    assert client.recv(5) == b"ready"

    # A real idle wait is intentional: a socketpair mock never inherits the
    # timeout socket.create_connection installs on the connected backend.
    handler.join(timeout=6.0)
    assert handler.is_alive(), "an established connection must have no idle deadline"
    backend.sendall(b"wake")
    assert client.recv(4) == b"wake"
    client.sendall(b"ack")
    assert backend.recv(3) == b"ack"


@pytest.mark.parametrize("initiator", ["client", "backend"])
def test_half_close_delivers_the_complete_reverse_stream(
    connection: tuple[socket.socket, socket.socket, threading.Thread],
    initiator: str,
) -> None:
    client, backend, handler = connection
    first, second = (client, backend) if initiator == "client" else (backend, client)
    first.sendall(b"request")
    first.shutdown(socket.SHUT_WR)
    assert _read_to_eof(second) == b"request"
    payload = bytes(range(256)) * 512

    def reply() -> None:
        second.sendall(payload)
        second.shutdown(socket.SHUT_WR)

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(reply)
        received = _read_to_eof(first)
        result.result(timeout=3.0)
    assert received == payload
    handler.join(timeout=3.0)
    assert not handler.is_alive()


def test_reset_backend_reclaims_a_pump_blocked_on_an_open_client(
    connection: tuple[socket.socket, socket.socket, threading.Thread],
) -> None:
    client, backend, handler = connection
    client.sendall(b"ready")
    assert backend.recv(5) == b"ready"
    linger_format = "HH" if sys.platform == "win32" else "ii"
    backend.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack(linger_format, 1, 0))
    backend.close()
    handler.join(timeout=3.0)
    assert not handler.is_alive(), "a transport failure must interrupt the reverse pump"
    assert client.recv(1) == b""
