"""Actual temporary processes and socket observations, never a fleet completeness proof."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from shared.daemon_http import start_daemon_http
from shared.managed_writer_observation import (
    ExpectedLauncher,
    ExpectedSession,
    ExpectedUnitWriters,
    LauncherObservation,
    ObservationChallenge,
    UnitObserver,
    observe_launcher,
    observe_session,
)
from shared.native_job_observation import NativeReadUnavailableError
from shared.process_evidence import ExpectedProcess


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

    absent = LauncherObservation(kind="launchd", definition="absent", loaded=False)

    def absent_launchd(*_args: object, **_kwargs: object) -> LauncherObservation:
        return absent

    monkeypatch.setattr(observation, "observe_launchd", absent_launchd)
    assert observe_launcher(expected, unit, until) == absent

    def unreadable(*_args: object, **_kwargs: object) -> LauncherObservation:
        raise NativeReadUnavailableError("launchctl enumeration unreadable")

    monkeypatch.setattr(observation, "observe_launchd", unreadable)
    assert observe_launcher(expected, unit, until) == LauncherObservation(kind="launchd")

    # The crontab arm rides the same fallback: a pass-through absent fact, and
    # the unknown kind default when the table cannot be read (i1 QA N1).
    crontab = ExpectedLauncher(kind="crontab", name="bootstrap", definition_digest="d" * 64)
    absent_crontab = LauncherObservation(
        kind="crontab", definition="absent", loaded=None, enabled=False
    )

    def absent_crontab_read(*_args: object, **_kwargs: object) -> LauncherObservation:
        return absent_crontab

    monkeypatch.setattr(observation, "observe_crontab", absent_crontab_read)
    assert observe_launcher(crontab, unit, until) == absent_crontab

    def unreadable_crontab(*_args: object, **_kwargs: object) -> LauncherObservation:
        raise NativeReadUnavailableError("crontab table unreadable")

    monkeypatch.setattr(observation, "observe_crontab", unreadable_crontab)
    assert observe_launcher(crontab, unit, until) == LauncherObservation(kind="crontab")
