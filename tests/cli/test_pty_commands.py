"""Operator CLI contract for host-wide PTY allocation freeze."""

from __future__ import annotations

import urllib.request
from pathlib import Path
from typing import Never

import psycopg
import pytest
import redis

from cli import main as cli_main
from shared.config import settings
from shared.pty_sessions import allocation_freeze


@pytest.fixture(autouse=True)
def _isolated_host_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.general, "cluster_registry", tmp_path / "host" / "clusters.json")


def test_operator_round_trip_needs_no_gateway_or_data_plane(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The recovery command stays local even when every network client fails."""

    def _offline(*_args: object, **_kwargs: object) -> Never:
        raise AssertionError("PTY allocation commands must not use the gateway or data plane")

    monkeypatch.setattr(psycopg, "connect", _offline)
    monkeypatch.setattr(redis.Redis, "execute_command", _offline)
    monkeypatch.setattr(urllib.request, "urlopen", _offline)
    monkeypatch.delenv("AVA_CONFIG_FETCH", raising=False)

    assert (
        cli_main.main(["pty", "freeze", "--holder", "operator-1818", "--reason", "offline cleanup"])
        == 0
    )
    frozen = allocation_freeze.read()
    assert frozen.generation is not None
    assert cli_main.main(["pty", "status"]) == 0
    assert cli_main.main(["pty", "resume", frozen.generation]) == 0
    assert allocation_freeze.read().status == "inactive"
    assert "status=frozen" in capsys.readouterr().out


def test_cli_refuses_stale_generation_and_preserves_current_owner(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli_main.main(["pty", "freeze", "--holder", "current", "--reason", "cleanup"]) == 0
    current = allocation_freeze.read()

    assert cli_main.main(["pty", "resume", "stale-generation"]) == 1
    assert allocation_freeze.read() == current
    assert "does not own the freeze" in capsys.readouterr().err


def test_cli_status_surfaces_corrupt_marker_as_fail_closed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = allocation_freeze.state_path()
    path.parent.mkdir(parents=True)
    path.write_text("[]")

    assert cli_main.main(["pty", "status"]) == 1
    output = capsys.readouterr().out
    assert "status=invalid" in output
    assert f"marker={path}" in output
