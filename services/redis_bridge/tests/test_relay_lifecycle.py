"""Service-level ownership, finite teardown, and real forwarding regressions."""

from __future__ import annotations

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from typing import cast

import pytest

from services.redis_bridge import relay


class _EndServingError(RuntimeError):
    pass


class _AcceptOnce:
    def __init__(self, client: socket.socket) -> None:
        self.client = client
        self.accepted = False
        self.closed = False

    def accept(self) -> tuple[socket.socket, tuple[str, int]]:
        if self.accepted:
            raise _EndServingError("caller failure")
        self.accepted = True
        return self.client, ("127.0.0.1", 1)

    def close(self) -> None:
        self.closed = True


def _close(*connections: socket.socket) -> None:
    for connection in connections:
        with suppress(OSError):
            connection.shutdown(socket.SHUT_RDWR)
        connection.close()


def test_serving_exit_reclaims_an_accepted_idle_connection() -> None:
    """The original relay returned with its accepted socket/handler still alive."""
    client, accepted = socket.socketpair()
    backend: socket.socket | None = None
    existing_threads = set(threading.enumerate())
    with socket.socket() as backend_listener:
        backend_listener.bind(("127.0.0.1", 0))
        backend_listener.listen()
        backend_listener.settimeout(2.0)
        client.settimeout(2.0)

        class Listener(_AcceptOnce):
            def accept(self) -> tuple[socket.socket, tuple[str, int]]:
                nonlocal backend
                if self.accepted:
                    backend, _ = backend_listener.accept()
                    backend.sendall(b"ready")
                    assert client.recv(5) == b"ready"
                return super().accept()

        listener = Listener(accepted)
        try:
            with pytest.raises(_EndServingError, match="caller failure"):
                relay.serve_forever(
                    ("127.0.0.1", 1),
                    backend_listener.getsockname(),
                    open_listener=lambda _address: cast(socket.socket, listener),
                )
            assert listener.closed
            assert accepted.fileno() == -1
            assert client.recv(1) == b""
        finally:
            _close(client, accepted)
            if backend is not None:
                _close(backend)
            for worker in set(threading.enumerate()) - existing_threads:
                worker.join(timeout=2.0)
                assert not worker.is_alive(), "regression experiment must reclaim its workers"


def test_stop_fences_admission_and_reclaims_idle_pumps() -> None:
    with socket.socket() as backend_listener:
        backend_listener.bind(("127.0.0.1", 0))
        backend_listener.listen()
        backend_listener.settimeout(2.0)
        service = relay.RelayService(("127.0.0.1", 0), backend_listener.getsockname())
        client, accepted = socket.socketpair()
        service._admit(accepted, ("127.0.0.1", 1))
        backend, _ = backend_listener.accept()
        owners = tuple(service._connections)
        try:
            backend.sendall(b"ready")
            client.settimeout(2.0)
            assert client.recv(5) == b"ready"
            assert service.stop(2.0)
            assert all(owner.completed() for owner in owners)
            assert not service._connections
            refused, refused_accepted = socket.socketpair()
            try:
                service._admit(refused_accepted, ("127.0.0.1", 2))
                assert refused_accepted.fileno() == -1
                assert not service._connections
            finally:
                _close(refused, refused_accepted)
        finally:
            _close(client, backend)
            service.stop(2.0)


def test_one_stop_budget_retains_blocked_dns_then_reaps_late_backend(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    started = threading.Barrier(3)
    release = threading.Event()
    backends: list[socket.socket] = []
    peers: list[socket.socket] = []
    calls: list[tuple[tuple[str, int], float]] = []

    def connect(address: tuple[str, int], timeout: float) -> socket.socket:
        calls.append((address, timeout))
        backend, peer = socket.socketpair()
        backends.append(backend)
        peers.append(peer)
        started.wait(timeout=2.0)
        assert release.wait(2.0)
        return backend

    monkeypatch.setattr(relay.socket, "create_connection", connect)
    service = relay.RelayService(("127.0.0.1", 0), ("redis.example", 6380))
    clients = [socket.socketpair() for _ in range(2)]
    for _client, accepted in clients:
        service._admit(accepted, ("127.0.0.1", 1))
    owners = tuple(service._connections)
    try:
        started.wait(timeout=2.0)
        before = time.monotonic()
        assert not service.stop(0.08)
        assert time.monotonic() - before < 0.3
        assert len(service._connections) == 2
        assert "2 connection owner(s) retained" in capsys.readouterr().out
        assert all(accepted.fileno() == -1 for _, accepted in clients)
        release.set()
        assert service.stop(2.0)
        assert all(owner.completed() and not owner._pumps for owner in owners)
        assert all(backend.fileno() == -1 for backend in backends)
        assert calls == [(("redis.example", 6380), 5.0)] * 2
    finally:
        release.set()
        service.stop(2.0)
        _close(*(endpoint for pair in clients for endpoint in pair), *peers, *backends)


def test_late_unknown_error_is_visible_and_collected_by_the_original_service(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entered = threading.Event()
    release = threading.Event()
    error = ValueError("late resolver implementation defect")

    def connect(*_args: object, **_kwargs: object) -> socket.socket:
        entered.set()
        assert release.wait(2.0)
        raise error

    monkeypatch.setattr(relay.socket, "create_connection", connect)
    service = relay.RelayService(("127.0.0.1", 0), ("redis.example", 6380))
    client, accepted = socket.socketpair()
    service._admit(accepted, ("127.0.0.1", 1))
    owner = service._connections[0]
    try:
        assert entered.wait(2.0)
        assert not service.stop(0.01)
        release.set()
        assert owner._done.wait(2.0)
        assert "late resolver implementation defect" in capsys.readouterr().err
        with pytest.raises(ValueError) as collected:
            service.stop(2.0)
        assert collected.value is error
        assert not service._connections
        assert owner.completed()
    finally:
        release.set()
        with suppress(ValueError):
            service.stop(2.0)
        _close(client, accepted)


class _RecvDefect:
    def __init__(self, connection: socket.socket, error: BaseException) -> None:
        self.connection = connection
        self.error = error

    def recv(self, _size: int) -> bytes:
        raise self.error

    def sendall(self, data: bytes) -> None:
        self.connection.sendall(data)

    def shutdown(self, how: int) -> None:
        self.connection.shutdown(how)

    def close(self) -> None:
        self.connection.close()


def test_unknown_pump_error_interrupts_reverse_and_is_raised_by_service() -> None:
    error = LookupError("forwarding implementation defect")
    with socket.socket() as backend_listener:
        backend_listener.bind(("127.0.0.1", 0))
        backend_listener.listen()
        backend_listener.settimeout(2.0)
        client, accepted = socket.socketpair()
        service = relay.RelayService(("127.0.0.1", 0), backend_listener.getsockname())
        service._admit(cast(socket.socket, _RecvDefect(accepted, error)), ("127.0.0.1", 1))
        owner = service._connections[0]
        backend, _ = backend_listener.accept()
        try:
            assert owner._done.wait(2.0), "unknown pump failure must release the reverse pump"
            owner._thread.join(timeout=2.0)
            service._reap()
            assert not service._connections
            with pytest.raises(LookupError) as collected:
                service.stop(2.0)
            assert collected.value is error
            assert owner.completed()
            assert not service._connections
        finally:
            with suppress(LookupError):
                service.stop(2.0)
            _close(client, accepted, backend)


def test_cleanup_keeps_primary_caller_error_and_records_secondary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entered = threading.Event()
    release = threading.Event()
    secondary = ValueError("secondary backend defect")

    def connect(*_args: object, **_kwargs: object) -> socket.socket:
        entered.set()
        assert release.wait(2.0)
        raise secondary

    class Listener(_AcceptOnce):
        def accept(self) -> tuple[socket.socket, tuple[str, int]]:
            if self.accepted:
                assert entered.wait(2.0)
                release.set()
            return super().accept()

    monkeypatch.setattr(relay.socket, "create_connection", connect)
    client, accepted = socket.socketpair()
    service = relay.RelayService(
        ("127.0.0.1", 0),
        ("redis.example", 6380),
        open_listener=lambda _address: cast(socket.socket, Listener(accepted)),
    )
    try:
        with pytest.raises(_EndServingError, match="caller failure"):
            relay._run_service(service)
        assert service._errors == [secondary]
        assert "secondary backend defect" in capsys.readouterr().err
        assert accepted.fileno() == -1
    finally:
        release.set()
        with suppress(ValueError):
            service.stop(2.0)
        _close(client, accepted)


@pytest.mark.parametrize("timeout", [-1.0, float("inf"), float("nan")])
def test_stop_rejects_nonfinite_or_negative_budget(timeout: float) -> None:
    service = relay.RelayService(("127.0.0.1", 0), ("127.0.0.1", 6380))
    with pytest.raises(ValueError, match="finite and nonnegative"):
        service.stop(timeout)


class _RebindAfterOneAccept:
    def __init__(self, listener: socket.socket) -> None:
        self.listener = listener
        self.accepted = False

    def accept(self) -> tuple[socket.socket, tuple[str, int]]:
        if self.accepted:
            raise OSError("interface lost")
        connection = self.listener.accept()
        self.accepted = True
        return connection

    def close(self) -> None:
        self.listener.close()

    def shutdown(self, how: int) -> None:
        self.listener.shutdown(how)


def test_rebound_listener_preserves_the_old_live_connection() -> None:
    listeners = [relay._open_listener(("127.0.0.1", 0)) for _ in range(2)]
    first_address, second_address = [listener.getsockname() for listener in listeners]
    rebound = threading.Event()
    opens = 0

    def open_listener(_address: tuple[str, int]) -> socket.socket:
        nonlocal opens
        opens += 1
        if opens == 1:
            return cast(socket.socket, _RebindAfterOneAccept(listeners[0]))
        rebound.set()
        return listeners[1]

    with socket.socket() as backend_listener:
        backend_listener.bind(("127.0.0.1", 0))
        backend_listener.listen()
        backend_listener.settimeout(2.0)
        service = relay.RelayService(
            first_address,
            backend_listener.getsockname(),
            open_listener=open_listener,
            sleep=lambda _delay: None,
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            serving = executor.submit(relay._run_service, service)
            client = socket.create_connection(first_address, timeout=2.0)
            backend, _ = backend_listener.accept()
            try:
                assert rebound.wait(2.0)
                client.sendall(b"old request")
                assert backend.recv(11) == b"old request"
                backend.sendall(b"old response")
                assert client.recv(12) == b"old response"
                new_client = socket.create_connection(second_address, timeout=2.0)
                new_backend, _ = backend_listener.accept()
                try:
                    new_client.sendall(b"new")
                    assert new_backend.recv(3) == b"new"
                finally:
                    _close(new_client, new_backend)
            finally:
                service.request_stop()
                serving.result(timeout=3.0)
                _close(client, backend, *listeners)
            assert not service._connections


def test_failed_worker_admission_closes_the_unowned_accepted_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("worker admission failed")

    def fail_start(_thread: threading.Thread) -> None:
        raise error

    monkeypatch.setattr(relay.threading.Thread, "start", fail_start)
    service = relay.RelayService(("127.0.0.1", 0), ("127.0.0.1", 6380))
    client, accepted = socket.socketpair()
    try:
        with pytest.raises(RuntimeError) as refused:
            service._admit(accepted, ("127.0.0.1", 1))
        assert refused.value is error
        assert accepted.fileno() == -1
        assert not service._connections
        assert service.stop(0.0)
    finally:
        _close(client, accepted)


def test_serving_reaps_completed_connections_without_stopping_admission() -> None:
    with socket.socket() as backend_listener:
        backend_listener.bind(("127.0.0.1", 0))
        backend_listener.listen()
        backend_listener.settimeout(2.0)
        client, accepted = socket.socketpair()
        client.settimeout(2.0)
        service = relay.RelayService(("127.0.0.1", 0), backend_listener.getsockname())
        service._admit(accepted, ("127.0.0.1", 1))
        owner = service._connections[0]
        backend, _ = backend_listener.accept()
        backend.settimeout(2.0)
        try:
            client.shutdown(socket.SHUT_WR)
            assert backend.recv(1) == b""
            backend.shutdown(socket.SHUT_WR)
            assert client.recv(1) == b""
            assert owner._done.wait(2.0)
            owner._thread.join(timeout=2.0)
            service._reap()
            assert not service._connections
            assert not service._stopped.is_set()
            assert owner.completed()
        finally:
            service.stop(2.0)
            _close(client, accepted, backend)


@pytest.mark.parametrize("worker_primary", [False, True])
def test_pump_cleanup_preserves_primary_and_collects_every_secondary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    worker_primary: bool,
) -> None:
    primary = LookupError("first worker or pump cleanup defect")
    secondary = ValueError("later pump cleanup defect")
    with socket.socket() as backend_listener:
        backend_listener.bind(("127.0.0.1", 0))
        backend_listener.listen()
        backend_listener.settimeout(2.0)
        client, accepted = socket.socketpair()
        client.settimeout(2.0)
        service = relay.RelayService(("127.0.0.1", 0), backend_listener.getsockname())
        source = cast(socket.socket, _RecvDefect(accepted, primary)) if worker_primary else accepted
        service._admit(source, ("127.0.0.1", 1))
        owner = service._connections[0]
        backend, _ = backend_listener.accept()
        backend.settimeout(2.0)
        try:
            if not worker_primary:
                client.shutdown(socket.SHUT_WR)
                assert backend.recv(1) == b""
                backend.shutdown(socket.SHUT_WR)
                assert client.recv(1) == b""
            assert owner._done.wait(2.0)
            owner._thread.join(timeout=2.0)
            first, second = owner._pumps
            original_stop = relay._Pump.stop

            def stop_with_cleanup_defect(pump: relay._Pump, timeout: float) -> bool:
                finished = original_stop(pump, timeout)
                if pump is first and not worker_primary:
                    raise primary
                if pump is second:
                    raise secondary
                return finished

            with monkeypatch.context() as patched:
                patched.setattr(relay._Pump, "stop", stop_with_cleanup_defect)
                with pytest.raises(LookupError) as collected:
                    service.stop(2.0)
            assert collected.value is primary
            assert service._errors == [primary, secondary]
            stderr = capsys.readouterr().err
            assert stderr.count(str(primary)) == 1
            assert stderr.count(str(secondary)) == 1
            assert owner.completed()
            assert not service._connections
        finally:
            with suppress(LookupError):
                service.stop(2.0)
            _close(client, accepted, backend)


def test_worker_failure_cleanup_retains_both_original_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    primary = LookupError("pump worker primary defect")
    secondary = ValueError("pump worker shutdown secondary defect")
    client, accepted = socket.socketpair()
    source = cast(socket.socket, _RecvDefect(accepted, primary))
    original_request_stop = relay._Pump.request_stop

    def request_stop_with_defect(pump: relay._Pump) -> None:
        original_request_stop(pump)
        if pump._source is source and threading.current_thread() is pump._thread:
            raise secondary

    monkeypatch.setattr(relay._Pump, "request_stop", request_stop_with_defect)
    with socket.socket() as backend_listener:
        backend_listener.bind(("127.0.0.1", 0))
        backend_listener.listen()
        backend_listener.settimeout(2.0)
        service = relay.RelayService(("127.0.0.1", 0), backend_listener.getsockname())
        service._admit(source, ("127.0.0.1", 1))
        owner = service._connections[0]
        backend, _ = backend_listener.accept()
        try:
            assert owner._done.wait(2.0)
            with pytest.raises(LookupError) as collected:
                service.stop(2.0)
            assert collected.value is primary
            assert service._errors == [primary, secondary]
            stderr = capsys.readouterr().err
            assert str(primary) in stderr and str(secondary) in stderr
            assert owner.completed()
            assert not service._connections
        finally:
            with suppress(LookupError):
                service.stop(2.0)
            _close(client, accepted, backend)


def test_connect_failure_cleanup_preserves_primary_and_retains_secondary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    primary = LookupError("connect primary defect")
    secondary = ValueError("connection close secondary defect")
    original_close = relay._Connection._close_sockets

    def fail_connect(*_args: object, **_kwargs: object) -> socket.socket:
        raise primary

    def close_with_defect(owner: relay._Connection) -> None:
        original_close(owner)
        if threading.current_thread() is owner._thread:
            raise secondary

    monkeypatch.setattr(relay.socket, "create_connection", fail_connect)
    monkeypatch.setattr(relay._Connection, "_close_sockets", close_with_defect)
    client, accepted = socket.socketpair()
    service = relay.RelayService(("127.0.0.1", 0), ("redis.example", 6380))
    service._admit(accepted, ("127.0.0.1", 1))
    owner = service._connections[0]
    try:
        assert owner._done.wait(2.0)
        with pytest.raises(LookupError) as collected:
            service.stop(2.0)
        assert collected.value is primary
        assert service._errors == [primary, secondary]
        stderr = capsys.readouterr().err
        assert str(primary) in stderr and str(secondary) in stderr
        assert owner.completed()
        assert not service._connections
    finally:
        with suppress(LookupError, ValueError):
            service.stop(2.0)
        _close(client, accepted)
