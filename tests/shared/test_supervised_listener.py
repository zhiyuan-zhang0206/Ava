"""Identity-verified takeover for a supervised listener."""

from __future__ import annotations

from pathlib import Path

import pytest

from shared import session_backend, session_record, supervised_listener


class _Record:
    def __init__(self, pid: int) -> None:
        self.pid = pid


class _Backend:
    def __init__(self, supervised: bool) -> None:
        self._supervised = supervised

    def has_session(self, _name: str) -> bool:
        return self._supervised


class _Clock:
    """Deterministic monotonic clock for bounded-stop unit tests."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _wire_listener(
    monkeypatch: pytest.MonkeyPatch,
    *,
    holder_matches_binary: bool,
    supervised_pid: int | None,
) -> None:
    """Make both collector ports resolve to one deterministic mock holder."""
    record = _Record(supervised_pid) if supervised_pid is not None else None

    def _matches(_pid: int, _binary: Path) -> bool:
        return holder_matches_binary

    monkeypatch.setattr(supervised_listener, "listeners_on", lambda _port: [1109])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        supervised_listener,
        "_listener_matches_binary",
        _matches,
    )
    # Method-local imports inside probe_supervised_listener read these at call
    # time, so the patches target the owning modules (test_update_import_timing
    # forbids top-level imports of the kill chain anywhere in the updater's
    # import closure).
    monkeypatch.setattr(session_record.SessionRecord, "read", lambda _path: record)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        session_backend,
        "get_backend",
        lambda: _Backend(supervised_pid is not None),
    )


def test_probe_marks_same_binary_without_a_live_session_record_reclaimable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PPID=1 collector is ours only when its supervisor record names its PID."""
    _wire_listener(monkeypatch, holder_matches_binary=True, supervised_pid=None)

    result = supervised_listener.probe_supervised_listener(
        "otel-collector", ports=(4318, 8888), binary=Path("/home/u/.ava/otelcol-contrib")
    )

    assert result.probe.alive is False
    assert result.probe.terminal is False
    assert result.stale_pids == (1109,)


def test_probe_keeps_the_listener_named_by_the_live_session_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detached native service stays alive when the record identifies its PID."""
    _wire_listener(monkeypatch, holder_matches_binary=True, supervised_pid=1109)

    result = supervised_listener.probe_supervised_listener(
        "otel-collector", ports=(4318, 8888), binary=Path("/home/u/.ava/otelcol-contrib")
    )

    assert result.probe.alive is True
    assert result.stale_pids == ()


def test_stale_listener_that_ignores_sigterm_is_force_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale listener gets only its bounded grace window before SIGKILL."""
    clock = _Clock()
    alive = {"value": True}
    signals: list[str] = []
    monkeypatch.setattr(supervised_listener, "time", clock)

    def _is_alive(_pid: int) -> bool:
        return alive["value"]

    def _request_stop(_pid: int) -> None:
        signals.append("term")

    monkeypatch.setattr(supervised_listener, "process_alive", _is_alive)
    monkeypatch.setattr(supervised_listener, "request_stop", _request_stop)

    def _force_kill(_pid: int) -> None:
        signals.append("kill")
        alive["value"] = False

    monkeypatch.setattr(supervised_listener, "force_kill", _force_kill)

    assert supervised_listener.terminate_stale_listener(1109, grace_s=0.2) == "forced"
    assert signals == ["term", "kill"]


def test_stale_listener_that_stops_on_sigterm_is_graceful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cooperative listener exits during the grace window without SIGKILL."""
    alive = {"value": True}
    signals: list[str] = []

    def _is_alive(_pid: int) -> bool:
        return alive["value"]

    def _request_stop(_pid: int) -> None:
        signals.append("term")
        alive["value"] = False

    monkeypatch.setattr(supervised_listener, "request_stop", _request_stop)

    def _force_kill(_pid: int) -> None:
        signals.append("kill")

    monkeypatch.setattr(supervised_listener, "process_alive", _is_alive)
    monkeypatch.setattr(supervised_listener, "force_kill", _force_kill)

    assert supervised_listener.terminate_stale_listener(1109, grace_s=0.2) == "graceful"
    assert signals == ["term"]


def test_stale_listener_that_is_already_gone_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PID that vanished before signalling is not reported as a clean stop."""
    signals: list[str] = []

    def _is_dead(_pid: int) -> bool:
        return False

    def _request_stop(_pid: int) -> None:
        signals.append("term")

    def _force_kill(_pid: int) -> None:
        signals.append("kill")

    monkeypatch.setattr(supervised_listener, "process_alive", _is_dead)
    monkeypatch.setattr(supervised_listener, "request_stop", _request_stop)
    monkeypatch.setattr(supervised_listener, "force_kill", _force_kill)

    assert supervised_listener.terminate_stale_listener(1109) == "noop"
    assert signals == []


def test_takeover_reports_a_stale_listener_that_survives_sigkill(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed SIGKILL stays visible to the caller instead of becoming a false recovery."""
    _wire_listener(monkeypatch, holder_matches_binary=True, supervised_pid=None)
    clock = _Clock()
    signals: list[str] = []

    def _is_alive(_pid: int) -> bool:
        return True

    def _request_stop(_pid: int) -> None:
        signals.append("term")

    def _force_kill(_pid: int) -> None:
        signals.append("kill")

    monkeypatch.setattr(supervised_listener, "time", clock)
    monkeypatch.setattr(supervised_listener, "process_alive", _is_alive)
    monkeypatch.setattr(supervised_listener, "request_stop", _request_stop)
    monkeypatch.setattr(supervised_listener, "force_kill", _force_kill)

    result = supervised_listener.reclaim_stale_supervised_listener(
        "otel-collector", ports=(4318, 8888), binary=Path("/home/u/.ava/otelcol-contrib"), grace_s=0
    )

    assert result.stale_pids == (1109,)
    assert signals == ["term", "kill"]
    assert "survived forced stop" in caplog.text


def test_takeover_refuses_a_different_process_on_the_collector_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Port number alone never authorizes a kill on a co-located unit."""
    _wire_listener(monkeypatch, holder_matches_binary=False, supervised_pid=None)
    killed: list[int] = []
    monkeypatch.setattr(supervised_listener, "force_kill", killed.append)

    result = supervised_listener.reclaim_stale_supervised_listener(
        "otel-collector", ports=(4318, 8888), binary=Path("/home/u/.ava/otelcol-contrib")
    )

    assert result.probe.terminal is True
    assert killed == []
