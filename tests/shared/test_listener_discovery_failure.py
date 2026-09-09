"""Inspection failures cannot certify absent collectors or authorize restarts."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import psutil
import pytest

from shared import port_preflight, proc, service_respawn, supervised_listener
from shared.daemon_health import DaemonProbe, ProbeVerdict
from tests.shared.test_supervised_listener import _wire_listener


@pytest.mark.parametrize("failure", ["timeout", "missing", "denied"])
def test_failed_listener_discovery_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    _wire_listener(monkeypatch, holder_matches_binary=True, supervised_pid=1109)
    monkeypatch.setattr(supervised_listener, "listeners_on", port_preflight.strict_listeners_on)

    def denied(*, kind: str) -> None:
        raise psutil.AccessDenied

    def lsof(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if failure == "timeout":
            raise subprocess.TimeoutExpired("lsof", 10)
        if failure == "missing":
            raise FileNotFoundError("lsof")
        return subprocess.CompletedProcess(["lsof"], 1, "", "permission denied")

    monkeypatch.setattr(psutil, "net_connections", denied)
    monkeypatch.setattr(proc, "run_bounded", lsof)
    result = supervised_listener.probe_supervised_listener(
        "otel-collector", ports=(4319, 8889), binary=Path("/collector")
    )
    assert result.probe.verdict is ProbeVerdict.UNAVAILABLE
    assert result.probe.terminal and not result.probe.alive
    assert "listener discovery failed on port 4319" in result.probe.detail
    assert result.stale_pids == ()
    assert port_preflight.listeners_on(4319) == []


def test_unavailable_probe_never_respawns(monkeypatch: pytest.MonkeyPatch) -> None:
    def respawn() -> DaemonProbe:
        pytest.fail("inspection failure must not restart a service")

    with pytest.raises(SystemExit):
        service_respawn.run_keepalive(
            "otel-collector",
            logging.getLogger(__name__),
            probe=lambda: DaemonProbe.unavailable("inspection denied"),
            respawn=respawn,
        )


@pytest.mark.parametrize(
    ("stdout", "code", "expected"), [("p1109\np1109\n", 0, [1109]), ("", 1, [])]
)
def test_successful_lsof_distinguishes_listeners_from_absence(
    monkeypatch: pytest.MonkeyPatch, stdout: str, code: int, expected: list[int]
) -> None:
    def denied(*, kind: str) -> None:
        raise psutil.AccessDenied

    def lsof(
        args: list[str], *, capture_output: bool, text: bool, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["lsof"], code, stdout, "")

    monkeypatch.setattr(psutil, "net_connections", denied)
    monkeypatch.setattr(proc, "run_bounded", lsof)
    assert port_preflight.strict_listeners_on(4319) == expected
