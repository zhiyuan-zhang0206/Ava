"""Watchdog probe for the launchd-owned macOS permissions helper."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from services.healthchecks import permissions_helper as hc
from services.permissions_helper import client


@pytest.fixture(autouse=True)
def _macos_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hc, "IS_MACOS", True)
    monkeypatch.setattr(hc, "init_gateway_process", lambda *_args, **_kwargs: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "_consecutive_failures", 0)
    monkeypatch.setattr(hc, "_reported_unhealthy", False)
    monkeypatch.setattr(hc, "_repair_attempted", False)


class _FakeSocket:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.timeout: float | None = None
        self.sent = b""
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def sendall(self, payload: bytes) -> None:
        self.sent += payload

    def recv(self, _size: int) -> bytes:
        response, self.response = self.response, b""
        return response

    def close(self) -> None:
        self.closed = True


def _error_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == "services.healthchecks.permissions_helper"
        and record.levelno == logging.ERROR
    ]


def test_ping_uses_short_timeout_and_helper_wire_protocol(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sock = _FakeSocket(
        b'{"id":0,"ok":true,"result":{"pong":true,"preflight_screen":true,"ax_trusted":true}}\n'
    )
    paths: list[str] = []

    def connect(path: str) -> _FakeSocket:
        paths.append(path)
        return sock

    socket_path = tmp_path / "helper.sock"
    monkeypatch.setattr(client, "_connect", connect)
    monkeypatch.setattr(hc, "permissions_helper_socket", lambda: socket_path)

    assert hc._ping()
    assert paths == [str(socket_path)]
    assert sock.timeout == 3.0
    assert json.loads(sock.sent) == {"id": 0, "method": "ping"}
    assert sock.closed


def test_unhealthy_episode_logs_once_and_repairs_once_after_third_round(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    repairs: list[None] = []
    monkeypatch.setattr(
        hc,
        "_ping",
        lambda: (_ for _ in ()).throw(client.PermissionsHelperError("socket unavailable")),
    )

    def fail_repair() -> bool:
        repairs.append(None)
        return False

    monkeypatch.setattr(hc, "repair_unresponsive_helper", fail_repair)

    with caplog.at_level(logging.ERROR, logger="services.healthchecks.permissions_helper"):
        for _ in range(4):
            hc.main()

    assert len(_error_records(caplog)) == 1
    assert repairs == [None]
    assert hc._consecutive_failures == 4
    assert hc._repair_attempted is True


def test_successful_repair_is_verified_and_cleared_on_next_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = iter((False, False, False, True))
    monkeypatch.setattr(hc, "_ping", lambda: next(replies))
    monkeypatch.setattr(hc, "repair_unresponsive_helper", lambda: True)

    hc.main()
    hc.main()
    hc.main()

    assert hc._consecutive_failures == 3
    assert hc._reported_unhealthy is True
    assert hc._repair_attempted is True

    hc.main()

    assert hc._consecutive_failures == 0
    assert hc._reported_unhealthy is False
    assert hc._repair_attempted is False


def test_non_macos_returns_without_ping_or_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hc, "IS_MACOS", False)
    monkeypatch.setattr(hc, "_ping", lambda: pytest.fail("non-macOS must not ping the helper"))
    monkeypatch.setattr(
        hc,
        "repair_unresponsive_helper",
        lambda: pytest.fail("non-macOS must not repair launchd"),
    )

    hc.main()
