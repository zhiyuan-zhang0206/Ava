"""Actual temporary processes and socket observations, never a fleet completeness proof."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Self
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import psutil
import pytest
from pydantic import SecretStr, ValidationError

from services.agent_ops import bootstrap
from services.agent_ops.bootstrap import ObserverProjection, PreparedObservation
from shared.daemon_http import start_daemon_http
from shared.managed_writer_barrier import RolloutIdentity
from shared.managed_writer_observation import (
    ExpectedLauncher,
    ExpectedProcess,
    ExpectedSession,
    ExpectedUnitWriters,
    LauncherObservation,
    ObservationChallenge,
    UnitObserver,
    observe_launcher,
    observe_process,
    observe_session,
)
from shared.native_job_observation import NativeReadUnavailableError
from shared.platform import IS_LINUX
from shared.session_record import pid_starttime_ticks
from shared.transport_encryption import TransportEncryptionUndeclared


class _StoppedServer:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def serve_forever(self) -> None:
        return None


def _prepared_observation(tmp_path: Path) -> PreparedObservation:
    now = datetime.now(UTC)
    return PreparedObservation(
        expected=ExpectedUnitWriters(
            machine="test",
            home=str(tmp_path.resolve()),
            artifact_digest="a" * 64,
            manifest_digest="b" * 64,
            processes=(),
            sessions=(),
            launchers=(),
        ),
        operation=RolloutIdentity(holder="test", acquired_at=now, target_sha="c" * 40),
        challenge=ObservationChallenge(challenge=uuid4(), valid_until=now + timedelta(minutes=1)),
        schema_digest="d" * 64,
    )


def _projected_observer(mode: str | None) -> ObserverProjection:
    environment = {
        "AVA_DB_URL": "postgresql://projected.invalid/test",
        "AVA_CLUSTER_SECRET": "test-cluster-secret",
        "AVA_OPS_HEALTH_PORT": "18106",
    }
    if mode is not None:
        environment["AVA_TRANSPORT_ENCRYPTION"] = mode
    # This is the Settings-free entry's contract: exercise its raw child
    # projection before ordinary Settings exists rather than mutating Settings.
    with patch.dict("os.environ", environment, clear=True):
        return ObserverProjection.from_environment()


def _skip_validate_entry(_context: PreparedObservation, _projection: ObserverProjection) -> None:
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", (None, "", "none", "wireguard"))
async def test_secret_bootstrap_observer_refuses_undeclared_off_box_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str | None
) -> None:
    projection = _projected_observer(mode)
    start = AsyncMock(return_value=_StoppedServer())
    monkeypatch.setattr(bootstrap, "validate_entry", _skip_validate_entry)
    monkeypatch.setattr(bootstrap, "start_daemon_http", start)

    with pytest.raises(TransportEncryptionUndeclared):
        await bootstrap.serve(_prepared_observation(tmp_path), projection)

    start.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("tls", "mtls", "overlay"))
async def test_secret_bootstrap_observer_accepts_declared_encrypted_off_box_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    projection = _projected_observer(mode)
    start = AsyncMock(return_value=_StoppedServer())
    monkeypatch.setattr(bootstrap, "validate_entry", _skip_validate_entry)
    monkeypatch.setattr(bootstrap, "start_daemon_http", start)

    await bootstrap.serve(_prepared_observation(tmp_path), projection)

    assert projection.transport_encryption == mode
    awaited = start.await_args
    assert awaited is not None
    assert awaited.kwargs["host"] == "0.0.0.0"  # noqa: S104 — asserted test value


@pytest.mark.parametrize("url", ["", "  "])
def test_empty_projected_db_url_cannot_use_ambient_postgres_defaults(url: str) -> None:
    with pytest.raises(ValidationError):
        ObserverProjection(db_url=SecretStr(url), cluster_secret=SecretStr(""), ops_port=8106)


def test_exact_live_exited_and_reused_identity() -> None:
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", "import sys;sys.stdin.read()"], stdin=subprocess.PIPE
    )
    try:
        expected = ExpectedProcess(
            pid=child.pid,
            create_time=psutil.Process(child.pid).create_time(),
            starttime=pid_starttime_ticks(child.pid),
        )
        assert observe_process(expected) == "alive"
        mismatch = expected.model_copy(
            update={"starttime": None, "create_time": expected.create_time + 60}
        )
        assert observe_process(mismatch) == "identity_mismatch"
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=5)
        assert observe_process(expected) == "exited"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.skipif(not IS_LINUX, reason="Linux /proc start-time identity")
def test_observe_process_reads_a_vanished_entry_as_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stop-race window in the managed-writer observation: psutil validated
    the pid, then the raw read found no /proc entry because the process was
    reaped in between — that is the exit itself, not a lost observation."""
    from shared import managed_writer_observation as observation

    child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"])
    pid = child.pid
    expected = ExpectedProcess(
        pid=pid,
        create_time=psutil.Process(pid).create_time(),
        starttime=pid_starttime_ticks(pid),
    )
    assert expected.starttime is not None
    real_read = pid_starttime_ticks

    def reaping_read(reading_pid: int) -> int | None:
        if reading_pid == pid:
            os.kill(pid, signal.SIGKILL)
            child.wait()
        return real_read(reading_pid)

    monkeypatch.setattr(observation, "pid_starttime_ticks", reaping_read)
    try:
        assert observe_process(expected) == "exited"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.skipif(not IS_LINUX, reason="Linux /proc start-time identity")
def test_observe_process_keeps_unknown_when_a_present_process_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pid that still exists while its start time cannot be read stays a
    full unknown — never collapsed into the exit it does not prove."""
    from shared import managed_writer_observation as observation

    expected = ExpectedProcess(
        pid=os.getpid(), create_time=psutil.Process().create_time(), starttime=1
    )

    def unreadable_read(_pid: int) -> int | None:
        return None

    monkeypatch.setattr(observation, "pid_starttime_ticks", unreadable_read)
    assert observe_process(expected) == "unknown"


def test_session_malformed_or_changed_is_not_absent(tmp_path: Path) -> None:
    expected = ExpectedSession(
        name="ava-test", process=ExpectedProcess(pid=1, create_time=1.0), generation="original"
    )
    assert observe_session(tmp_path, expected) == "absent"
    directory = tmp_path / "run" / "sessions"
    directory.mkdir(parents=True)
    record = directory / "ava-test.json"
    record.write_text("{broken")
    assert observe_session(tmp_path, expected) == "unknown"
    record.write_text(json.dumps({"pid": 1, "create_time": 1.0, "generation": "replacement"}))
    assert observe_session(tmp_path, expected) == "identity_mismatch"
    record.write_text(json.dumps({"pid": 1, "create_time": 1.0, "generation": "original"}))
    assert observe_session(tmp_path, expected) == "record_present"


@pytest.mark.asyncio
async def test_actual_observation_socket_requires_fresh_authenticated_challenge(
    tmp_path: Path,
) -> None:
    challenge = ObservationChallenge(
        challenge=uuid4(), valid_until=datetime.now(UTC) + timedelta(minutes=1)
    )
    observer = UnitObserver(
        ExpectedUnitWriters(
            machine="test",
            home=str(tmp_path.resolve()),
            artifact_digest="a" * 64,
            manifest_digest="b" * 64,
            processes=(),
            sessions=(),
            launchers=(),
        ),
        challenge,
    )
    token = uuid4().hex
    server = await start_daemon_http(
        host="127.0.0.1",
        port=0,
        auth_token=token,
        health_response=lambda: (503, b'{"mode":"bootstrap_observation","full_ready":false}'),
        extra_routes={("POST", "/ops/bootstrap-observation"): observer.respond},
    )

    async def request(path: str, nonce: str, bearer: str) -> tuple[int, dict[str, object]]:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", server.sockets[0].getsockname()[1]
        )
        body = json.dumps({"challenge": nonce}).encode()
        writer.write(
            f"POST {path} HTTP/1.1\r\nAuthorization: Bearer {bearer}\r\nContent-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        raw = await reader.read()
        writer.close()
        await writer.wait_closed()
        header, _, payload = raw.partition(b"\r\n\r\n")
        return int(header.split(b" ")[1]), json.loads(payload) if payload else {}

    try:
        endpoint = "/ops/bootstrap-observation"
        assert (await request(endpoint, str(challenge.challenge), "wrong"))[0] == 401
        assert (await request("/ops", str(challenge.challenge), token))[0] == 404
        assert (await request(endpoint, str(uuid4()), token))[0] == 409
        status, result = await request(endpoint, str(challenge.challenge), token)
        assert status == 200 and result["challenge"] == str(challenge.challenge)
        assert result["full_ready"] is False
        assert result["closure"] == "unknown"  # empty test inventory is never fleet proof
        observer.challenge = challenge.model_copy(update={"valid_until": datetime.now(UTC)})
        assert (await request(endpoint, str(challenge.challenge), token))[0] == 409
    finally:
        server.close()
        await server.wait_closed()


def test_whole_second_create_time_drift_is_still_the_expected_process() -> None:
    """A create_time re-read within the tolerance is the expected process, not a reuse."""
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", "import sys;sys.stdin.read()"], stdin=subprocess.PIPE
    )
    try:
        live = psutil.Process(child.pid).create_time()
        for offset in (-1.0, 1.0):
            expected = ExpectedProcess(pid=child.pid, create_time=live + offset, starttime=None)
            assert observe_process(expected) == "alive"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_observe_launcher_passes_absent_through_and_unknowns_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absent definition is a fenced-removal fact, not a lost observation."""
    from shared import managed_writer_observation as observation

    expected = ExpectedLauncher(kind="launchd", name="com.ava.test", definition_digest="a" * 64)
    unit = ExpectedUnitWriters(
        machine="macmini",
        home="/Users/example/.ava",
        artifact_digest="b" * 64,
        manifest_digest="c" * 64,
        processes=(),
        sessions=(),
        launchers=(),
    )
    until = datetime.now(UTC) + timedelta(seconds=10)

    absent = LauncherObservation(definition="absent", loaded=False)

    def absent_launchd(*_args: object, **_kwargs: object) -> LauncherObservation:
        return absent

    monkeypatch.setattr(observation, "observe_launchd", absent_launchd)
    assert observe_launcher(expected, unit, until) == absent

    def unreadable(*_args: object, **_kwargs: object) -> LauncherObservation:
        raise NativeReadUnavailableError("launchctl enumeration unreadable")

    monkeypatch.setattr(observation, "observe_launchd", unreadable)
    assert observe_launcher(expected, unit, until) == LauncherObservation()
