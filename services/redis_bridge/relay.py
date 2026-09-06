#!/usr/bin/env python3
"""Pure-stdlib TCP relay from a private-network address to loopback Redis.

The installed copy runs under ``/usr/bin/python3`` so macOS's application
firewall recognizes the serving binary.  Redis itself remains loopback-only.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import suppress

_BUFFER_SIZE = 65536
_INITIAL_REBIND_DELAY_S = 1.0
_MAX_REBIND_DELAY_S = 30.0

_sleep = time.sleep


def _log(message: str) -> None:
    sys.stdout.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    sys.stdout.flush()


def _open_listener(address: tuple[str, int]) -> socket.socket:
    family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(address)
        listener.listen(128)
    except BaseException:
        listener.close()
        raise
    return listener


def _pump(
    source: socket.socket,
    destination: socket.socket,
    stopped: threading.Event,
) -> None:
    try:
        with suppress(OSError):
            while data := source.recv(_BUFFER_SIZE):
                destination.sendall(data)
    finally:
        stopped.set()


def _handle(
    client: socket.socket,
    peer: tuple[str, int],
    backend_address: tuple[str, int],
) -> None:
    try:
        backend = socket.create_connection(backend_address, timeout=5.0)
    except OSError as exc:
        _log(f"backend connect failed for {peer[0]}:{peer[1]}: {exc}")
        client.close()
        return

    _log(f"relay {peer[0]}:{peer[1]} <-> backend")
    stopped = threading.Event()
    pumps = (
        threading.Thread(target=_pump, args=(client, backend, stopped), daemon=True),
        threading.Thread(target=_pump, args=(backend, client, stopped), daemon=True),
    )
    for pump in pumps:
        pump.start()
    stopped.wait()
    for connection in (client, backend):
        with suppress(OSError):
            connection.shutdown(socket.SHUT_RDWR)
        with suppress(OSError):
            connection.close()
    for pump in pumps:
        pump.join()


def serve_forever(
    listen_address: tuple[str, int],
    backend_address: tuple[str, int],
    *,
    open_listener: Callable[[tuple[str, int]], socket.socket] = _open_listener,
) -> None:
    """Serve connections, rebuilding the listener after every socket failure.

    A private-network interface can disappear while the process remains alive.
    On macOS that leaves the listening descriptor in ``CLOSED`` and ``accept``
    starts raising.  The listener is the failed resource, so close and recreate
    it; continuing against the same descriptor produces a permanently live but
    unreachable launchd job.
    """

    delay_s = _INITIAL_REBIND_DELAY_S
    while True:
        listener: socket.socket | None = None
        try:
            listener = open_listener(listen_address)
            _log(
                f"relay listening on {listen_address[0]}:{listen_address[1]} -> "
                f"{backend_address[0]}:{backend_address[1]}"
            )
            delay_s = _INITIAL_REBIND_DELAY_S
            while True:
                try:
                    client, peer = listener.accept()
                except OSError as exc:
                    _log(f"listener accept failed: {exc}; rebuilding listener")
                    break
                threading.Thread(
                    target=_handle,
                    args=(client, peer, backend_address),
                    daemon=True,
                ).start()
        except OSError as exc:
            _log(f"listener bind failed: {exc}; retrying in {delay_s:g}s")
        finally:
            if listener is not None:
                with suppress(OSError):
                    listener.close()
        _sleep(delay_s)
        delay_s = min(delay_s * 2.0, _MAX_REBIND_DELAY_S)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-host", required=True)
    parser.add_argument("--listen-port", required=True, type=int)
    parser.add_argument("--backend-host", default="127.0.0.1")
    parser.add_argument("--backend-port", required=True, type=int)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    serve_forever(
        (args.listen_host, args.listen_port),
        (args.backend_host, args.backend_port),
    )


if __name__ == "__main__":
    main()
