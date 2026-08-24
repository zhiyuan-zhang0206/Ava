"""Identity-verified takeover for a supervised listener."""

from __future__ import annotations

from pathlib import Path

import pytest

from shared import daemon_health


class _Record:
    def __init__(self, pid: int) -> None:
        self.pid = pid


class _Backend:
    def __init__(self, supervised: bool) -> None:
        self._supervised = supervised

    def has_session(self, _name: str) -> bool:
        return self._supervised


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

    monkeypatch.setattr(daemon_health, "listeners_on", lambda _port: [1109])  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(
        daemon_health,
        "_listener_matches_binary",
        _matches,
    )
    monkeypatch.setattr(daemon_health.SessionRecord, "read", lambda _path: record)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(
        daemon_health,
        "get_backend",
        lambda: _Backend(supervised_pid is not None),
    )


def test_probe_marks_same_binary_without_a_live_session_record_reclaimable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PPID=1 collector is ours only when its supervisor record names its PID."""
    _wire_listener(monkeypatch, holder_matches_binary=True, supervised_pid=None)

    result = daemon_health.probe_supervised_listener(
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

    result = daemon_health.probe_supervised_listener(
        "otel-collector", ports=(4318, 8888), binary=Path("/home/u/.ava/otelcol-contrib")
    )

    assert result.probe.alive is True
    assert result.stale_pids == ()


def test_takeover_kills_only_the_verified_stale_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """A collector that lost its record is reclaimed before the replacement binds."""
    _wire_listener(monkeypatch, holder_matches_binary=True, supervised_pid=None)
    killed: list[int] = []
    monkeypatch.setattr(daemon_health, "force_kill", killed.append)

    daemon_health.reclaim_stale_supervised_listener(
        "otel-collector", ports=(4318, 8888), binary=Path("/home/u/.ava/otelcol-contrib")
    )

    assert killed == [1109]


def test_takeover_refuses_a_different_process_on_the_collector_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Port number alone never authorizes a kill on a co-located unit."""
    _wire_listener(monkeypatch, holder_matches_binary=False, supervised_pid=None)
    killed: list[int] = []
    monkeypatch.setattr(daemon_health, "force_kill", killed.append)

    result = daemon_health.reclaim_stale_supervised_listener(
        "otel-collector", ports=(4318, 8888), binary=Path("/home/u/.ava/otelcol-contrib")
    )

    assert result.probe.terminal is True
    assert killed == []
