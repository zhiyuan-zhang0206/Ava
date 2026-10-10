#!/usr/bin/env python3
"""Pure-stdlib TCP relay from a private-network address to loopback Redis.

The installed copy runs under ``/usr/bin/python3`` so macOS's application
firewall recognizes the serving binary. Redis itself remains loopback-only.
"""

from __future__ import annotations

import argparse
import math
import signal
import socket
import sys
import threading
import time
import traceback
from collections.abc import Callable, Sequence
from contextlib import suppress
from types import FrameType

_BUFFER_SIZE = 65536
_INITIAL_REBIND_DELAY_S = 1.0
_MAX_REBIND_DELAY_S = 30.0
_STOP_TIMEOUT_S = 5.0


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
        listener.settimeout(0.25)
    except BaseException:
        listener.close()
        raise
    return listener


def _shutdown(connection: socket.socket) -> None:
    with suppress(OSError):
        connection.shutdown(socket.SHUT_RDWR)


class _Pump:
    """One forwarding direction, retained until its actual worker is joined."""

    def __init__(
        self,
        source: socket.socket,
        destination: socket.socket,
        observe_error: Callable[[BaseException], None],
    ) -> None:
        self._source = source
        self._destination = destination
        self._observe_error = observe_error
        self._stopped = threading.Event()
        self._done = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._forward()
        except BaseException as error:
            self._error = error
            self._observe_error(error)
            try:
                self.request_stop()
            except BaseException as secondary:
                self._observe_error(secondary)
        finally:
            self._done.set()

    def _forward(self) -> None:
        try:
            while not self._stopped.is_set():
                data = self._source.recv(_BUFFER_SIZE)
                if not data:
                    # EOF ends only this direction; the peer can still reply.
                    with suppress(OSError):
                        self._destination.shutdown(socket.SHUT_WR)
                    return
                self._destination.sendall(data)
        except OSError:
            self.request_stop()

    def request_stop(self) -> None:
        self._stopped.set()
        for connection in (self._source, self._destination):
            _shutdown(connection)

    def wait(self, timeout: float) -> bool:
        return self._done.wait(timeout)

    def finished(self) -> bool:
        return not self._thread.is_alive()

    def stop(self, timeout: float) -> bool:
        self.request_stop()
        self._thread.join(timeout=timeout)
        finished = not self._thread.is_alive()
        if self._error is not None:
            raise self._error
        return finished


class _Connection:
    """Own the accepted socket, backend lookup/connect, and both pump workers."""

    def __init__(
        self,
        client: socket.socket,
        peer: tuple[str, int],
        backend_address: tuple[str, int],
        observe_error: Callable[[BaseException], None],
    ) -> None:
        self._client = client
        self._peer = peer
        self._backend_address = backend_address
        self._observe_error = observe_error
        self._backend: socket.socket | None = None
        self._pumps: list[_Pump] = []
        self._lock = threading.RLock()
        self._stopped = threading.Event()
        self._done = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._forward()
        except BaseException as error:
            self._error = error
            self._observe_error(error)
        finally:
            self._done.set()

    def _forward(self) -> None:
        try:
            self._connect_and_forward()
        except BaseException as error:
            self._observe_error(error)
            try:
                self._close_sockets()
            except BaseException as secondary:
                self._observe_error(secondary)
            raise
        else:
            self._close_sockets()

    def _connect_and_forward(self) -> None:
        if self._stopped.is_set():
            return
        try:
            # Resolve the hostname on each connection, as create_connection did.
            backend = socket.create_connection(self._backend_address, timeout=5.0)
        except OSError as exc:
            _log(f"backend connect failed for {self._peer[0]}:{self._peer[1]}: {exc}")
            return
        with self._lock:
            self._backend = backend
            if self._stopped.is_set():
                return
            # Establishment is bounded; connected Redis Pub/Sub has no idle limit.
            backend.settimeout(None)
            _log(f"relay {self._peer[0]}:{self._peer[1]} <-> backend")
            self._pumps.append(_Pump(self._client, backend, self._observe_error))
            self._pumps.append(_Pump(backend, self._client, self._observe_error))
        for pump in self._pumps:
            while not pump.wait(0.1):
                if self._stopped.is_set():
                    pump.request_stop()

    def _close_sockets(self) -> None:
        with self._lock:
            for connection in (self._client, self._backend):
                if connection is not None:
                    _shutdown(connection)
                    with suppress(OSError):
                        connection.close()

    def request_stop(self) -> None:
        with self._lock:
            self._stopped.set()
            for connection in (self._client, self._backend):
                if connection is not None:
                    _shutdown(connection)
            for pump in self._pumps:
                pump.request_stop()
            self._close_sockets()

    def stop(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        self.request_stop()
        self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
        finished = not self._thread.is_alive()
        error = self._error
        with self._lock:
            pumps = tuple(self._pumps)
        for pump in pumps:
            try:
                finished = pump.stop(max(0.0, deadline - time.monotonic())) and finished
            except BaseException as secondary:
                self._observe_error(secondary)
                error = error if error is not None else secondary
        if error is not None:
            raise error
        return finished

    def completed(self) -> bool:
        with self._lock:
            return not self._thread.is_alive() and all(pump.finished() for pump in self._pumps)


class RelayService:
    """Own admission and retain connections, unfinished workers, and original errors."""

    def __init__(
        self,
        listen_address: tuple[str, int],
        backend_address: tuple[str, int],
        *,
        open_listener: Callable[[tuple[str, int]], socket.socket] = _open_listener,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._listen_address = listen_address
        self._backend_address = backend_address
        self._open_listener = open_listener
        self._stopped = threading.Event()
        self._sleep = sleep if sleep is not None else self._stopped.wait
        self._lock = threading.RLock()
        self._listener: socket.socket | None = None
        self._connections: list[_Connection] = []
        self._errors: list[BaseException] = []

    def _observe_error(self, error: BaseException) -> None:
        with self._lock:
            if any(recorded is error for recorded in self._errors):
                return
            self._errors.append(error)
        # Keep the original exception for stop; show failures even after stop returns.
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)

    def _admit(self, client: socket.socket, peer: tuple[str, int]) -> None:
        with self._lock:
            if self._stopped.is_set():
                client.close()
                return
            try:
                connection = _Connection(client, peer, self._backend_address, self._observe_error)
            except BaseException:
                client.close()
                raise
            self._connections.append(connection)

    def _reap(self) -> None:
        with self._lock:
            for connection in tuple(self._connections):
                if connection.completed():
                    try:
                        finished = connection.stop(0.0)
                    except BaseException as error:
                        self._observe_error(error)
                        finished = connection.completed()
                    if finished:
                        self._connections.remove(connection)

    def request_stop(self) -> None:
        with self._lock:
            self._stopped.set()
            if self._listener is not None:
                _shutdown(self._listener)
                with suppress(OSError):
                    self._listener.close()
            for connection in self._connections:
                connection.request_stop()

    def stop(self, timeout: float = _STOP_TIMEOUT_S) -> bool:
        """Close admission, interrupt sockets, then join within one finite budget.

        False reports retained unfinished workers (for example blocked DNS). Unknown
        worker errors are immediately visible and the first original is raised here.
        The same owner can call stop again to collect a late completion or error.
        """
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("relay stop timeout must be finite and nonnegative")
        deadline = time.monotonic() + timeout
        self.request_stop()
        with self._lock:
            connections = tuple(self._connections)
        unfinished = 0
        for connection in connections:
            try:
                finished = connection.stop(max(0.0, deadline - time.monotonic()))
            except BaseException as error:
                self._observe_error(error)
                finished = connection.completed()
            if finished:
                with self._lock:
                    if connection in self._connections:
                        self._connections.remove(connection)
            else:
                unfinished += 1
        if unfinished:
            _log(f"relay stop unfinished: {unfinished} connection owner(s) retained")
        with self._lock:
            if self._errors:
                raise self._errors[0]
        return unfinished == 0

    def serve_forever(self) -> None:
        """Rebind a failed listener while established connections keep forwarding."""
        delay_s = _INITIAL_REBIND_DELAY_S
        # quiesce-exempt: a TCP relay; no database
        while not self._stopped.is_set():
            listener: socket.socket | None = None
            try:
                listener = self._open_listener(self._listen_address)
                with self._lock:
                    self._listener = listener
                _log(
                    f"relay listening on {self._listen_address[0]}:{self._listen_address[1]} -> "
                    f"{self._backend_address[0]}:{self._backend_address[1]}"
                )
                delay_s = _INITIAL_REBIND_DELAY_S
                self._accept(listener)
            except OSError as exc:
                _log(f"listener bind failed: {exc}; retrying in {delay_s:g}s")
            finally:
                if listener is not None:
                    with suppress(OSError):
                        listener.close()
                with self._lock:
                    self._listener = None
            if not self._stopped.is_set():
                self._sleep(delay_s)
                delay_s = min(delay_s * 2.0, _MAX_REBIND_DELAY_S)

    def _accept(self, listener: socket.socket) -> None:
        while not self._stopped.is_set():
            self._reap()
            try:
                client, peer = listener.accept()
            except OSError as exc:
                # macOS's installed Python 3.9 has a distinct socket.timeout type.
                if isinstance(exc, socket.timeout):
                    continue
                if not self._stopped.is_set():
                    _log(f"listener accept failed: {exc}; rebuilding listener")
                return
            self._admit(client, peer)


def _run_service(service: RelayService) -> None:
    try:
        service.serve_forever()
    except BaseException:
        # Cleanup must not replace a primary listener/caller failure. Worker
        # secondary errors have already been made visible by their observer.
        try:
            service.stop()
        except BaseException as secondary:
            service._observe_error(secondary)
        raise
    else:
        service.stop()


def serve_forever(
    listen_address: tuple[str, int],
    backend_address: tuple[str, int],
    *,
    open_listener: Callable[[tuple[str, int]], socket.socket] = _open_listener,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Serve with an owner that stops and collects connections on every exit."""
    _run_service(
        RelayService(listen_address, backend_address, open_listener=open_listener, sleep=sleep)
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-host", required=True)
    parser.add_argument("--listen-port", required=True, type=int)
    parser.add_argument("--backend-host", default="127.0.0.1")
    parser.add_argument("--backend-port", required=True, type=int)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    service = RelayService(
        (args.listen_host, args.listen_port), (args.backend_host, args.backend_port)
    )

    def stop_handler(_signum: int, _frame: FrameType | None) -> None:
        service.request_stop()

    previous_int = signal.signal(signal.SIGINT, stop_handler)
    previous_term = signal.signal(signal.SIGTERM, stop_handler)
    try:
        _run_service(service)
    finally:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)


if __name__ == "__main__":
    main()
