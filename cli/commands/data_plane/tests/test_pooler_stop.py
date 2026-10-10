"""Owned PgBouncer shutdown remains non-escalating across retries and callers."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlsplit

import psutil
import psycopg
import pytest

from base.cluster import ownership
from base.cluster.dataplane import pooler as base_pooler
from base.cluster.dataplane.pg_tools import throwaway_postgres
from base.config import settings
from base.native_process.ownership import OwnedProcess
from base.telemetry import EventPipeline
from cli.commands.data_plane import _pooler_stop, maintenance_stop
from cli.commands.data_plane import pgbouncer as pooler
from cli.commands.data_plane._pooler_stop import OwnedPooler, _native_birth
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="native POSIX pooler")


# Port 1 on loopback never listens for an unprivileged test: a refused dial is
# the stop path's proof that this home has no Redis, and no other process can
# take the port between allocation and use (the placeholder URL's choice).
_ABSENT_REDIS = "redis://127.0.0.1:1"
_BIND_ATTEMPTS = 5
# The pooler port comes from a range no OS ephemeral allocator hands out (macOS
# 49152+, Linux 32768+) and no cluster port block uses (18000-20000): a draining
# pooler closes its listener while it waits, and a port-0 allocation elsewhere in
# the suite must not be able to land on it before the final stop checks custody.
_PRIVATE_PORTS = range(21000, 30000)


def _private_port() -> int:
    for _attempt in range(64):
        port = _PRIVATE_PORTS[secrets.randbelow(len(_PRIVATE_PORTS))]
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    pytest.fail("no free private pooler port")


def _pooler_config(directory: Path, port: int, backend_port: int) -> str:
    return (
        "[databases]\n"
        f"test = host=127.0.0.1 port={backend_port} dbname=ava_citest user=ava\n"
        "[pgbouncer]\nlisten_addr=127.0.0.1\n"
        f"listen_port={port}\nauth_type=trust\nauth_file={directory / 'userlist.txt'}\n"
        f"pidfile={directory / 'pgbouncer.pid'}\nlogfile={directory / 'pgbouncer.log'}\n"
        "pool_mode=transaction\nunix_socket_dir=\nadmin_users=ava,ava_pooler_admin\n"
    )


def _kill(identity: OwnedProcess | None) -> None:
    if identity is not None and identity.live():
        process = psutil.Process(identity.pid)
        assert OwnedProcess.capture(process) == identity
        process.kill()
        with contextlib.suppress(psutil.NoSuchProcess):
            process.wait(timeout=5)


def _owned_listener(identity: OwnedProcess | None, port: int) -> bool:
    """Whether `identity` alone listens on `port` (bounded wait for its bind)."""
    deadline = time.monotonic() + 5
    while identity is not None and identity.live() and time.monotonic() < deadline:
        try:
            if ownership.require_listener(identity, port):
                return True
        except RuntimeError:
            # A foreign listener took the port between allocation and bind, or
            # the pooler has not bound yet; only our own listener proves custody.
            if ownership.strict_listeners_on(port):
                return False
        time.sleep(0.01)
    return False


def _start_private_pooler(
    binary: str, directory: Path, backend_port: int
) -> tuple[OwnedProcess, int]:
    """Start the fixture pooler on a port it provably owns.

    A free-port probe is only a hint: another process can bind the port before
    PgBouncer does. Custody is proven by the listener table, and a lost port is
    retried on a fresh one instead of failing the test that follows.
    """
    config = directory / "pgbouncer.ini"
    record = directory / "pgbouncer.pid"
    for _attempt in range(_BIND_ATTEMPTS):
        port = _private_port()
        config.write_text(_pooler_config(directory, port, backend_port))
        record.unlink(missing_ok=True)
        subprocess.run([binary, "-d", str(config)], check=True, capture_output=True, timeout=5)
        deadline = time.monotonic() + 5
        while not record.exists():
            assert time.monotonic() < deadline, "private pooler did not publish its PID"
            time.sleep(0.01)
        identity = ownership.pooler(config, record)
        if _owned_listener(identity, port):
            assert identity is not None
            return identity, port
        _kill(identity)
    pytest.fail(f"no private pooler port could be bound in {_BIND_ATTEMPTS} attempts")


@pytest.fixture
def native_pooler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[OwnedPooler, str, str]]:
    binary = pooler.pgbouncer_bin()
    if not (Path(binary).exists() or shutil.which(binary)):
        pytest.skip("native PgBouncer is not installed")
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    monkeypatch.setattr(base_pooler, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(settings.data_plane, "db_url", "postgresql://ava@127.0.0.1:12345/test")
    monkeypatch.setattr(settings.data_plane, "redis_url", _ABSENT_REDIS)
    monkeypatch.setattr(settings.data_plane, "redis_admin_password", "")
    directory = tmp_path / "pgbouncer"
    directory.mkdir()
    config = directory / "pgbouncer.ini"
    (directory / "userlist.txt").write_text('"ava" ""\n"ava_pooler_admin" ""\n')
    with throwaway_postgres() as direct:
        backend_port = urlsplit(direct).port
        assert backend_port is not None
        identity, port = _start_private_pooler(binary, directory, backend_port)
        try:
            with psycopg.connect(direct, autocommit=True) as conn:
                conn.execute("CREATE TABLE pooler_stop_receipt (value integer)")
            yield (
                OwnedPooler(identity, port, config),
                f"postgresql://ava@127.0.0.1:{port}/test",
                direct,
            )
        finally:
            _kill(identity)


@pytest.mark.parametrize(
    ("preexisting_drain", "compensation_retry"), [(False, False), (False, True), (True, False)]
)
def test_repeated_normal_stop_preserves_waiting_transaction(
    native_pooler: tuple[OwnedPooler, str, str],
    preexisting_drain: bool,
    compensation_retry: bool,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    custodian, pooled, direct = native_pooler
    with psycopg.connect(pooled) as client:
        client.execute("INSERT INTO pooler_stop_receipt VALUES (42)")
        if preexisting_drain:
            # A prior native stop has no Ava intent record. Closed listeners
            # still forbid a second signal; native custody remains observable.
            os.kill(custodian.identity.pid, signal.SIGINT)
            deadline = time.monotonic() + 5
            while ownership.require_listener(custodian.identity, custodian.port, required=False):
                assert time.monotonic() < deadline, "private pooler did not begin draining"
                time.sleep(0.01)
        for _attempt in range(2):
            failure: RuntimeError | TimeoutError | None = None
            try:
                if _attempt and compensation_retry:
                    pooler.stop_pgbouncer()
                else:
                    maintenance_stop.stop(0.5, producer=operator_pipeline)
            except (RuntimeError, TimeoutError) as exc:
                failure = exc
            assert custodian.identity.live(), "retry must not immediately abort the waiting pooler"
            assert client.execute("SELECT value FROM pooler_stop_receipt").fetchone() == (42,)
            assert failure is not None and "custody retained" in str(failure)
        client.commit()
    assert custodian.stop(deadline=time.monotonic() + 5)
    with psycopg.connect(direct) as conn:
        assert conn.execute("SELECT value FROM pooler_stop_receipt").fetchone() == (42,)
    assert not custodian.identity.live()


@pytest.mark.parametrize(("force", "budget"), [(False, 5.0), (True, -1.0)])
def test_a_drain_that_finishes_during_the_listener_scan_is_stopped(
    native_pooler: tuple[OwnedPooler, str, str],
    monkeypatch: pytest.MonkeyPatch,
    force: bool,
    budget: float,
) -> None:
    """A retained drain can complete while a retry (or the force leg) scans the
    pooler's listeners — a busy host's socket scan outlasts the pooler's exit:
    the birth that exits mid-scan is stopped, never a foreign listener."""
    custodian, pooled, direct = native_pooler
    scan = ownership.strict_listeners_on
    with contextlib.closing(psycopg.connect(pooled)) as client:
        client.execute("INSERT INTO pooler_stop_receipt VALUES (7)")
        # The open transaction retains the drain for the whole budget; the budget
        # only has to outlast one listener scan before the signal (lsof on macOS).
        assert not custodian.stop(deadline=time.monotonic() + 2)

        def drain_finishes_during_scan(port: int) -> list[int]:
            client.commit()
            deadline = time.monotonic() + 5
            while custodian.identity.live():
                assert time.monotonic() < deadline, "private pooler did not finish its drain"
                time.sleep(0.01)
            return scan(port)

        monkeypatch.setattr(ownership, "strict_listeners_on", drain_finishes_during_scan)
        assert custodian.stop(deadline=time.monotonic() + budget, force=force)
    with psycopg.connect(direct) as conn:
        assert conn.execute("SELECT value FROM pooler_stop_receipt").fetchone() == (7,)
    assert not custodian.identity.live()


@pytest.mark.parametrize("force_budget", [0.2, -1.0])
def test_explicit_force_can_finish_a_retained_drain(
    native_pooler: tuple[OwnedPooler, str, str], force_budget: float
) -> None:
    custodian, pooled, direct = native_pooler
    with contextlib.closing(psycopg.connect(pooled)) as client:
        client.execute("INSERT INTO pooler_stop_receipt VALUES (99)")
        # Allow native listener validation and the durable intent write during setup.
        # The transaction retains the drain; the force budget below is under test.
        assert not custodian.stop(deadline=time.monotonic() + 2)
        assert custodian.identity.live()
        assert custodian.stop(deadline=time.monotonic() + force_budget, force=True)
        with pytest.raises(psycopg.OperationalError):
            client.execute("SELECT 1")
    with psycopg.connect(direct) as conn:
        assert conn.execute("SELECT count(*) FROM pooler_stop_receipt").fetchone() == (0,)


class _StopClock:
    """Only the mocked pooler's wait consumes this test's virtual budget."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        self.now += delay


@pytest.fixture
def stop_clock(monkeypatch: pytest.MonkeyPatch) -> _StopClock:
    clock = _StopClock()
    monkeypatch.setattr(_pooler_stop, "time", clock)
    return clock


def test_durable_intent_prevents_resignal_before_listener_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop_clock: _StopClock
) -> None:
    identity = OwnedProcess.capture(psutil.Process(os.getpid()))
    custodian = OwnedPooler(identity, 16433, tmp_path / "pgbouncer.ini")
    process = Mock()
    monkeypatch.setattr(OwnedPooler, "_process", Mock(return_value=process))
    monkeypatch.setattr(ownership, "require_listener", Mock(return_value=frozenset({identity.pid})))
    for _attempt in range(2):
        assert not custodian.stop(deadline=stop_clock.monotonic() + 0.02)
    process.send_signal.assert_called_once_with(signal.SIGINT)
    with pytest.raises(RuntimeError, match="already requested"):
        custodian.require_accepting()


@pytest.mark.parametrize("after_intent_write", [False, True])
def test_expired_stop_budget_never_admits_a_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop_clock: _StopClock,
    after_intent_write: bool,
) -> None:
    identity = OwnedProcess.capture(psutil.Process(os.getpid()))
    custodian = OwnedPooler(identity, 16433, tmp_path / "pgbouncer.ini")
    process = Mock()
    monkeypatch.setattr(OwnedPooler, "_process", Mock(return_value=process))
    monkeypatch.setattr(ownership, "require_listener", Mock(return_value=frozenset({identity.pid})))
    deadline = stop_clock.monotonic()
    if after_intent_write:
        write = _pooler_stop.write_private_bytes

        def expires_after_write(path: Path, data: bytes) -> None:
            write(path, data)
            stop_clock.sleep(0.02)

        monkeypatch.setattr(_pooler_stop, "write_private_bytes", expires_after_write)
        deadline += 0.02
    with pytest.raises(TimeoutError, match="stop deadline expired; custody retained"):
        custodian.stop(deadline=deadline)
    process.send_signal.assert_not_called()
    intent = tmp_path / "stop-intent.json"
    assert intent.exists() is after_intent_write
    if after_intent_write:
        assert json.loads(intent.read_text()) == _native_birth(identity)


def test_native_birth_change_refuses_signal(tmp_path: Path) -> None:
    identity = OwnedProcess.capture(psutil.Process(os.getpid()))
    changed = OwnedProcess(
        identity.pid,
        identity.birth + 1,
        identity.starttime + 1 if identity.starttime is not None else None,
    )
    custodian = OwnedPooler(changed, 16433, tmp_path / "pgbouncer.ini")
    with pytest.raises(RuntimeError, match="native birth changed"):
        custodian._process()


def test_linux_stop_intent_uses_native_tick_after_clock_correction(tmp_path: Path) -> None:
    first_reader = OwnedProcess(1234, 1000.0, 9876)
    next_reader = OwnedProcess(1234, 1001.0, 9876)
    (tmp_path / "stop-intent.json").write_text(json.dumps(_native_birth(first_reader)))
    assert OwnedPooler(next_reader, 16433, tmp_path / "pgbouncer.ini")._stop_requested()
    replacement = OwnedProcess(1234, 1001.0, 9877)
    assert not OwnedPooler(replacement, 16433, tmp_path / "pgbouncer.ini")._stop_requested()


def test_foreign_listener_refuses_before_intent_or_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = OwnedProcess.capture(psutil.Process(os.getpid()))
    custodian = OwnedPooler(identity, 16433, tmp_path / "pgbouncer.ini")
    monkeypatch.setattr(
        ownership, "strict_listeners_on", Mock(return_value=[identity.pid, 99999999])
    )
    with pytest.raises(RuntimeError, match="does not belong"):
        custodian.stop(deadline=time.monotonic() + 1)
    assert not (tmp_path / "stop-intent.json").exists()


def test_corrupt_stop_intent_cannot_authorize_another_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = OwnedProcess.capture(psutil.Process(os.getpid()))
    custodian = OwnedPooler(identity, 16433, tmp_path / "pgbouncer.ini")
    (tmp_path / "stop-intent.json").write_text(json.dumps({"pid": identity.pid}))
    monkeypatch.setattr(ownership, "require_listener", Mock(return_value=frozenset({identity.pid})))
    with pytest.raises(RuntimeError, match="invalid PgBouncer stop intent"):
        custodian.stop(deadline=time.monotonic() + 1)
