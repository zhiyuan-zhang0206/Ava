"""Bring-up roster reconcile — `services/agent_host/daemon.py`.

2026-09-02: two rollouts restarted the restarter on hosted boxes where the
roster disables it (pids 10113 / 60380); for minutes each reaped healthy
hosted-agent rows every 30s before the watchdog's own round stopped it. The
host now stops roster-gated process services itself at bring-up. These tests
pin the stop-set derivation and the never-raises contract.
"""

from __future__ import annotations

import os
import signal
import sys
import types
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from services.agent_host import daemon


def _spec(session: str, cmd: str, pidfile: Path) -> types.SimpleNamespace:
    return types.SimpleNamespace(session=session, cmd=cmd, pidfile=pidfile)


def _stub_roster(
    monkeypatch: pytest.MonkeyPatch,
    gate_reasons: dict[str, str | None],
    specs: list[types.SimpleNamespace],
) -> None:
    """Point the function's lazy `from ops.spec import ...` at canned data.

    ``monkeypatch.setitem`` (not a bare ``sys.modules`` assignment) so the real
    module is restored when the test ends — a leaked stub would poison later
    files in the same shard whose lazy ``from ops.spec import ...`` resolves
    against the residue (watchdog daemon import, #1460 CI att1/att2).
    """
    mod = types.ModuleType("ops.spec")

    def _gate_reason(spec: types.SimpleNamespace) -> str | None:
        return gate_reasons.get(spec.session)

    def _build_services() -> list[types.SimpleNamespace]:
        return specs

    mod._gate_reason = _gate_reason  # type: ignore[attr-defined]
    mod.build_services = _build_services  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ops.spec", mod)


def _record_kill(killed: list[tuple[int, int]]) -> Callable[[int, int], None]:
    def kill(pid: int, sig: int) -> None:
        killed.append((pid, sig))

    return kill


def _noop_sleep(s: float) -> None:
    return None


def _holds_when(alive: Iterator[bool]) -> Callable[[Path, str], bool]:
    def holds(pidfile: Path, module: str) -> bool:
        return next(alive)

    return holds


_HOSTED_RESTARTER = "disabled (AVA_RUNNER_MODE is hosted — per-agent process supervision retired)"


class TestStopStrayModeGatedServices:
    def test_stops_a_running_restarter_on_hosted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pidfile = tmp_path / "restarter.pid"
        pidfile.write_text("4242\n")
        spec = _spec("restarter", ".venv/bin/python -m services.restarter.daemon", pidfile)
        _stub_roster(monkeypatch, {"restarter": _HOSTED_RESTARTER}, [spec])
        monkeypatch.setattr(daemon, "_runner_mode", lambda: "hosted")
        # running before the kill, gone after — the grace loop exits immediately
        monkeypatch.setattr(daemon, "pidfile_holds_daemon", _holds_when(iter([True, False])))
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", _record_kill(killed))
        daemon._stop_stray_mode_gated_services()
        assert killed == [(4242, signal.SIGTERM)]

    def test_does_not_stop_an_absent_restarter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pidfile = tmp_path / "restarter.pid"
        pidfile.write_text("4242\n")
        spec = _spec("restarter", ".venv/bin/python -m services.restarter.daemon", pidfile)
        _stub_roster(monkeypatch, {"restarter": _HOSTED_RESTARTER}, [spec])
        monkeypatch.setattr(daemon, "_runner_mode", lambda: "hosted")
        monkeypatch.setattr(daemon, "pidfile_holds_daemon", _holds_when(iter([False])))
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", _record_kill(killed))
        daemon._stop_stray_mode_gated_services()
        assert killed == []

    def test_leaves_config_toggle_gates_alone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A service off for operator reasons (AVA_*_ENABLED off) is not a
        mode exclusion — the bring-up sweep must not fight the operator."""
        pidfile = tmp_path / "heartbeat.pid"
        pidfile.write_text("4242\n")
        spec = _spec("heartbeat", ".venv/bin/python -m services.heartbeat.daemon", pidfile)
        _stub_roster(monkeypatch, {"heartbeat": "disabled (AVA_HEARTBEAT_ENABLED off)"}, [spec])
        monkeypatch.setattr(daemon, "_runner_mode", lambda: "hosted")
        monkeypatch.setattr(daemon, "pidfile_holds_daemon", _holds_when(iter([True])))
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", _record_kill(killed))
        daemon._stop_stray_mode_gated_services()
        assert killed == []

    def test_leaves_the_other_modes_gate_alone(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """agent-host's own gate (disabled in process mode) must not be acted
        on by a hosted host — only exclusions FOR the current mode apply."""
        pidfile = tmp_path / "agent-host.pid"
        pidfile.write_text("4242\n")
        spec = _spec("agent-host", ".venv/bin/python -m services.agent_host.daemon", pidfile)
        _stub_roster(monkeypatch, {"agent-host": "disabled (AVA_RUNNER_MODE is process)"}, [spec])
        monkeypatch.setattr(daemon, "_runner_mode", lambda: "hosted")
        monkeypatch.setattr(daemon, "pidfile_holds_daemon", _holds_when(iter([True])))
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", _record_kill(killed))
        daemon._stop_stray_mode_gated_services()
        assert killed == []

    def test_grace_timeout_logs_and_does_not_block(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A service that ignores SIGTERM must not block bring-up forever —
        warn and leave it to the watchdog round."""
        pidfile = tmp_path / "restarter.pid"
        pidfile.write_text("4242\n")
        spec = _spec("restarter", ".venv/bin/python -m services.restarter.daemon", pidfile)
        _stub_roster(monkeypatch, {"restarter": _HOSTED_RESTARTER}, [spec])
        monkeypatch.setattr(daemon, "_runner_mode", lambda: "hosted")
        monkeypatch.setattr(daemon, "pidfile_holds_daemon", _holds_when(iter([True])))
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", _record_kill(killed))
        # deadline read lands at t=0; the first loop-condition read is already
        # past the 20s grace, so the wait loop exits without sleeping.
        clock = iter([0.0, 21.0])
        monkeypatch.setattr(daemon.time, "monotonic", lambda: next(clock))
        monkeypatch.setattr(daemon.time, "sleep", _noop_sleep)
        with caplog.at_level("WARNING", logger="services.agent_host.daemon"):
            daemon._stop_stray_mode_gated_services()
        assert killed == [(4242, signal.SIGTERM)]
        assert "still up after" in caplog.text

    def test_never_raises_when_the_roster_is_unreadable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A roster read failure must not block the host from serving agents."""
        mod = types.ModuleType("ops.spec")  # no _gate_reason / build_services
        monkeypatch.setitem(sys.modules, "ops.spec", mod)
        monkeypatch.setattr(daemon, "_runner_mode", lambda: "hosted")
        daemon._stop_stray_mode_gated_services()  # must not raise
