"""Inspection failures cannot certify absent collectors or authorize restarts."""

from __future__ import annotations

import logging
import os
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

    def _resolve(*args: str) -> list[str]:
        return ["lsof", *args]

    monkeypatch.setattr(psutil, "net_connections", denied)
    monkeypatch.setattr(proc, "run_bounded", lsof)
    # Resolution is deterministic here: the fixture host may or may not have
    # lsof installed, and neither case should change what is asserted.
    monkeypatch.setattr(port_preflight, "_lsof_argv", _resolve)
    assert port_preflight.strict_listeners_on(4319) == expected


def test_probe_resolves_lsof_through_candidates_in_restricted_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The company-air regression: a context whose PATH hides lsof (macOS
    keeps it in /usr/sbin) must still resolve the supervised listener and
    report it alive instead of "nothing listening"."""
    _wire_listener(monkeypatch, holder_matches_binary=True, supervised_pid=1109)
    monkeypatch.setattr(supervised_listener, "listeners_on", port_preflight.strict_listeners_on)

    def denied(*, kind: str) -> None:
        raise psutil.AccessDenied

    seen: list[list[str]] = []

    def lsof(
        argv: list[str], *, capture_output: bool, text: bool, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="p1109\n", stderr="")

    def _which(name: str) -> str | None:
        assert name == "lsof"
        return None

    def _access(path: str, mode: int) -> bool:
        assert mode == os.X_OK
        return path == "/usr/sbin/lsof"

    monkeypatch.setattr(psutil, "net_connections", denied)
    monkeypatch.setattr(proc, "run_bounded", lsof)
    monkeypatch.setattr(port_preflight.shutil, "which", _which)
    monkeypatch.setattr(port_preflight.os, "access", _access)

    result = supervised_listener.probe_supervised_listener(
        "otel-collector", ports=(4319, 8889), binary=Path("/collector")
    )
    assert result.probe.alive and result.probe.verdict is ProbeVerdict.ALIVE
    assert result.probe.detail == "otel-collector listener pid 1109 is supervised"
    assert result.stale_pids == ()
    assert seen and seen[0][0] == "/usr/sbin/lsof"
