"""Owned PgBouncer shutdown remains non-escalating across retries and callers."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlsplit

import psutil
import psycopg
import pytest

from cli.commands import _maintenance_data_plane as maintenance
from cli.commands import _pgbouncer as pooler
from cli.commands._pooler_stop import OwnedPooler, _native_birth
from shared.cluster import ownership
from shared.config import settings
from shared.native_process.ownership import OwnedProcess
from shared.pg_tools import throwaway_postgres
from tests._containers import _free_port, _wait_port

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="native POSIX pooler")


@pytest.fixture
def native_pooler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[OwnedPooler, str, str]]:
    binary = pooler.pgbouncer_bin()
    if not (Path(binary).exists() or shutil.which(binary)):
        pytest.skip("native PgBouncer is not installed")
    monkeypatch.setattr(settings.general, "ava_home", str(tmp_path))
    monkeypatch.setattr(pooler, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(settings.data_plane, "db_url", "postgresql://ava@127.0.0.1:12345/test")
    monkeypatch.setattr(settings.data_plane, "redis_url", f"redis://127.0.0.1:{_free_port()}")
    monkeypatch.setattr(settings.data_plane, "redis_admin_password", "")
    port = _free_port()
    directory = tmp_path / "pgbouncer"
    directory.mkdir()
    config = directory / "pgbouncer.ini"
    record = directory / "pgbouncer.pid"
    (directory / "userlist.txt").write_text('"ava" ""\n')
    with throwaway_postgres() as direct:
        backend = urlsplit(direct)
        config.write_text(
            "[databases]\n"
            f"test = host=127.0.0.1 port={backend.port} dbname=ava_citest user=ava\n"
            "[pgbouncer]\nlisten_addr=127.0.0.1\n"
            f"listen_port={port}\nauth_type=trust\nauth_file={directory / 'userlist.txt'}\n"
            f"pidfile={record}\nlogfile={directory / 'pgbouncer.log'}\n"
            "pool_mode=transaction\nunix_socket_dir=\nadmin_users=ava\n"
        )
        subprocess.run(  # noqa: S603 — private fixture configuration and captured native process
            [binary, "-d", str(config)], check=True, capture_output=True, timeout=5
        )
        identity = None
        try:
            _wait_port(port, timeout=5)
            deadline = time.monotonic() + 5
            while not record.exists():
                assert time.monotonic() < deadline, "private pooler did not publish its PID"
                time.sleep(0.01)
            identity = ownership.pooler(config, record)
            assert identity is not None
            ownership.require_listener(identity, port)
            with psycopg.connect(direct, autocommit=True) as conn:
                conn.execute("CREATE TABLE pooler_stop_receipt (value integer)")
            yield (
                OwnedPooler(identity, port, config),
                f"postgresql://ava@127.0.0.1:{port}/test",
                direct,
            )
        finally:
            if identity is None and record.exists():
                identity = ownership.pooler(config, record)
            if identity is not None and identity.live():
                process = psutil.Process(identity.pid)
                assert OwnedProcess.capture(process) == identity
                process.kill()
                with contextlib.suppress(psutil.NoSuchProcess):
                    process.wait(timeout=5)


@pytest.mark.parametrize(
    ("preexisting_drain", "compensation_retry"), [(False, False), (False, True), (True, False)]
)
def test_repeated_normal_stop_preserves_waiting_transaction(
    native_pooler: tuple[OwnedPooler, str, str], preexisting_drain: bool, compensation_retry: bool
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
                    maintenance.stop(0.5)
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


@pytest.mark.parametrize("force_budget", [0.2, -1.0])
def test_explicit_force_can_finish_a_retained_drain(
    native_pooler: tuple[OwnedPooler, str, str], force_budget: float
) -> None:
    custodian, pooled, direct = native_pooler
    with contextlib.closing(psycopg.connect(pooled)) as client:
        client.execute("INSERT INTO pooler_stop_receipt VALUES (99)")
        assert not custodian.stop(deadline=time.monotonic() + 0.2)
        assert custodian.identity.live()
        assert custodian.stop(deadline=time.monotonic() + force_budget, force=True)
        with pytest.raises(psycopg.OperationalError):
            client.execute("SELECT 1")
    with psycopg.connect(direct) as conn:
        assert conn.execute("SELECT count(*) FROM pooler_stop_receipt").fetchone() == (0,)


def test_durable_intent_prevents_resignal_before_listener_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = OwnedProcess.capture(psutil.Process(os.getpid()))
    custodian = OwnedPooler(identity, 16433, tmp_path / "pgbouncer.ini")
    process = Mock()
    monkeypatch.setattr(OwnedPooler, "_process", Mock(return_value=process))
    monkeypatch.setattr(ownership, "require_listener", Mock(return_value=frozenset({identity.pid})))
    for _attempt in range(2):
        assert not custodian.stop(deadline=time.monotonic() + 0.02)
    process.send_signal.assert_called_once_with(signal.SIGINT)
    with pytest.raises(RuntimeError, match="already requested"):
        custodian.require_accepting()


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
