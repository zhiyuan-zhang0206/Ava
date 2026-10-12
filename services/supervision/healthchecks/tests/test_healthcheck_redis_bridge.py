"""The redis-bridge identity probe: a RESP reply through the relay is the only proof."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from services.supervision.healthchecks.redis_bridge import probe_relay


@contextmanager
def _listener(reply: bytes | None) -> Iterator[int]:
    """A TCP listener answering the first connection with `reply` (None: close silently)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()

    def serve() -> None:
        connection, _ = server.accept()
        with connection:
            connection.recv(64)
            if reply is not None:
                connection.sendall(reply)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield server.getsockname()[1]
    finally:
        server.close()
        thread.join(timeout=2)


def test_a_redis_reply_through_the_relay_is_alive() -> None:
    for reply in (b"+PONG\r\n", b"-NOAUTH Authentication required.\r\n"):
        with _listener(reply) as port:
            assert probe_relay("127.0.0.1", port).alive


def test_a_listener_that_forwards_nothing_is_down() -> None:
    with _listener(None) as port:
        assert not probe_relay("127.0.0.1", port).alive
    with _listener(b"HTTP/1.1 400 Bad Request\r\n") as port:
        assert not probe_relay("127.0.0.1", port).alive


def test_no_listener_is_down() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert not probe_relay("127.0.0.1", port).alive
