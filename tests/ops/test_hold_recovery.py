"""Bounded completion of a stranded update hold (task #3142) — the decision matrix.

`maybe_spawn_stranded_recovery` is the whole policy surface of the mechanism:
which verdicts, phases, machine roles and budgets may spend the episode's one
attempt, and what a failed spawn leaves behind. Its two hard limits are locked
here as well: the reservation (a DB compare-and-set) is the only path that
reaches a spawn, and the spawned session runs the same
`python -m cli.commands._hold_recover` argv the updater family uses.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from ops import hold_recovery
from ops.controllers import stranded_pause as sp
from shared import pause_owner
from shared.config import settings
from shared.maintenance_state import MaintenanceHold
from shared.updater_handoff import UpdaterHandoffSnapshot

_AT = datetime(2026, 9, 12, 3, 0, tzinfo=UTC)
_HOLDER = "gateway-host:pid4242"

_VerdictKind = Literal["stranded", "clear", "unknown"]


def _verdict(kind: _VerdictKind = "stranded") -> sp.StrandedHoldVerdict:
    return sp.StrandedHoldVerdict(kind=kind, detail="updater exited rc=1", paused_for=700.0)


def _verdict_reader(
    kind: _VerdictKind,
) -> Callable[[UpdaterHandoffSnapshot | None], sp.StrandedHoldVerdict]:
    """A stand-in for `stranded_hold_verdict` that ignores its handoff argument."""

    def _read(_handoff: UpdaterHandoffSnapshot | None = None) -> sp.StrandedHoldVerdict:
        return _verdict(kind)

    return _read


def _snapshot(phase: str, *, holder: str = _HOLDER, acquired_at: datetime = _AT) -> object:
    return pause_owner.PauseOwnerSnapshot(
        status="paused",
        holder=holder,
        acquired_at=acquired_at,
        maintenance=MaintenanceHold(phase=phase),  # type: ignore[arg-type]
    )


@pytest.fixture
def hold_at_phase(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Point the hold read at a paused unit held at `phase`."""

    def _plant(phase: str) -> None:
        monkeypatch.setattr(sp.pause_owner, "read", lambda: _snapshot(phase))

    return _plant


class _Spawns:
    """Recorded spawn calls + the reservation's answer."""

    def __init__(self, log_path: Path) -> None:
        self.calls: list[tuple[str, datetime]] = []
        self.notes: list[str] = []
        self.reservations: list[dict[str, object]] = []
        self.attempt: int | None = 1
        self.log_path = log_path

    def reserve(self, **kwargs: object) -> int | None:
        self.reservations.append(kwargs)
        return self.attempt

    def finish(self, note: str) -> None:
        self.notes.append(note)


@pytest.fixture
def spawns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Spawns:
    """Fake the reservation write + the session spawn; record both."""
    import shared.host_deploy_state as hds

    rec = _Spawns(tmp_path / "hold-recover.log")
    monkeypatch.setattr(hds, "reserve_stranded_recovery", rec.reserve)
    monkeypatch.setattr(hds, "finish_stranded_recovery", rec.finish)

    def _spawn(*, holder: str, acquired_at: datetime) -> Path:
        rec.calls.append((holder, acquired_at))
        return rec.log_path

    monkeypatch.setattr(hold_recovery, "spawn_hold_recovery", _spawn)
    return rec


def test_spawns_one_attempt_for_a_stranded_post_stop_hold(
    hold_at_phase: Callable[[str], None], spawns: _Spawns
) -> None:
    """The licensed shape: a stranded verdict, a post-stop phase, an unspent budget."""
    hold_at_phase("stopping")
    sp.maybe_spawn_stranded_recovery(_verdict(), role="agent-runner")
    assert spawns.calls == [(_HOLDER, _AT)]
    assert spawns.reservations == [
        {
            "max_attempts": hold_recovery.MAX_ATTEMPTS,
            "cooldown_s": hold_recovery.COOLDOWN_S,
            "note": "attempt reserved (phase=stopping)",
        }
    ]
    assert spawns.notes == []


@pytest.mark.parametrize("phase", ["stopping", "stopped", "starting"])
def test_every_post_stop_phase_may_be_completed(
    phase: str, hold_at_phase: Callable[[str], None], spawns: _Spawns
) -> None:
    hold_at_phase(phase)
    sp.maybe_spawn_stranded_recovery(_verdict(), role="agent-runner")
    assert spawns.calls == [(_HOLDER, _AT)]


@pytest.mark.parametrize("phase", ["preparing", "draining", "drained", "ready"])
def test_pre_stop_phases_are_never_completed_automatically(
    phase: str, hold_at_phase: Callable[[str], None], spawns: _Spawns
) -> None:
    """An incomplete drain is `resume --cancel` work; a `ready` hold is finished."""
    hold_at_phase(phase)
    sp.maybe_spawn_stranded_recovery(_verdict(), role="agent-runner")
    assert spawns.calls == [] and spawns.reservations == []


@pytest.mark.parametrize("kind", ["clear", "unknown"])
def test_only_a_stranded_verdict_licenses_the_attempt(
    kind: _VerdictKind, hold_at_phase: Callable[[str], None], spawns: _Spawns
) -> None:
    hold_at_phase("stopping")
    sp.maybe_spawn_stranded_recovery(_verdict(kind), role="agent-runner")
    assert spawns.calls == [] and spawns.reservations == []


def test_no_verdict_means_no_action(spawns: _Spawns) -> None:
    sp.maybe_spawn_stranded_recovery(None, role="agent-runner")
    assert spawns.calls == [] and spawns.reservations == []


def test_no_hold_means_no_action(monkeypatch: pytest.MonkeyPatch, spawns: _Spawns) -> None:
    monkeypatch.setattr(
        sp.pause_owner, "read", lambda: pause_owner.PauseOwnerSnapshot(status="inactive")
    )
    sp.maybe_spawn_stranded_recovery(_verdict(), role="agent-runner")
    assert spawns.calls == [] and spawns.reservations == []


def test_the_gateway_capabilitys_round_never_starts_a_completion(
    hold_at_phase: Callable[[str], None], spawns: _Spawns
) -> None:
    """`role` is the ROUND's capability, not the machine's: the gateway watchdog
    never initiates a completion, while a unit that also serves `agent-runner`
    completes the same hold in that capability's round."""
    hold_at_phase("stopping")
    sp.maybe_spawn_stranded_recovery(_verdict(), role="gateway")
    assert spawns.calls == [] and spawns.reservations == []
    sp.maybe_spawn_stranded_recovery(_verdict(), role="agent-runner")
    assert spawns.calls == [(_HOLDER, _AT)]


def test_kill_switch_off_spends_nothing(
    hold_at_phase: Callable[[str], None], spawns: _Spawns, monkeypatch: pytest.MonkeyPatch
) -> None:
    hold_at_phase("stopping")
    monkeypatch.setattr(settings.gateway, "stranded_hold_recovery", False)
    sp.maybe_spawn_stranded_recovery(_verdict(), role="agent-runner")
    assert spawns.calls == [] and spawns.reservations == []


def test_a_spent_budget_never_spawns(hold_at_phase: Callable[[str], None], spawns: _Spawns) -> None:
    """The compare-and-set declines: budget spent, inside cooldown, or cleared."""
    hold_at_phase("stopping")
    spawns.attempt = None
    sp.maybe_spawn_stranded_recovery(_verdict(), role="agent-runner")
    assert spawns.calls == []


def test_a_failed_spawn_still_spends_the_attempt(
    hold_at_phase: Callable[[str], None], spawns: _Spawns, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The episode's budget is the bound, not the backend's success."""
    hold_at_phase("stopping")

    def _boom(**_kwargs: object) -> Path:
        raise RuntimeError("session backend declined")

    monkeypatch.setattr(hold_recovery, "spawn_hold_recovery", _boom)
    sp.maybe_spawn_stranded_recovery(_verdict(), role="agent-runner")
    assert spawns.reservations and len(spawns.notes) == 1
    assert spawns.notes[0].startswith("spawn failed: RuntimeError")


def test_the_controller_calls_the_completion_for_a_stranded_pause(
    monkeypatch: pytest.MonkeyPatch, spawns: _Spawns
) -> None:
    """Wiring: the paused branch hands this round's verdict to the completion gate."""
    monkeypatch.setattr(sp, "is_paused", lambda: True)
    monkeypatch.setattr(sp, "recover_stranded_pause", lambda: False)
    monkeypatch.setattr(sp, "stranded_hold_verdict", _verdict_reader("stranded"))
    monkeypatch.setattr(sp, "sync_stranded_hold_record", _verdict_reader("stranded"))
    monkeypatch.setattr(sp.pause_owner, "read", lambda: _snapshot("stopped"))
    sp.PauseController().reconcile("agent-runner")
    assert spawns.calls == [(_HOLDER, _AT)]


def test_spawn_command_shape(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The detached session is `ava-hold-recover` running the module entry."""
    captured: dict[str, str] = {}

    def _spawn(session: str, *, shell_cmd: str, native_cmd: str) -> None:
        captured["session"] = session
        captured["shell"] = shell_cmd
        captured["native"] = native_cmd

    def _log(prefix: str) -> Path:
        return tmp_path / f"{prefix}-1.log"

    monkeypatch.setattr("ops.cluster_session._spawn_detached_session", _spawn)
    monkeypatch.setattr("ops.cluster_deploy._new_update_log", _log)
    log_path = hold_recovery.spawn_hold_recovery(holder=_HOLDER, acquired_at=_AT)
    assert captured["session"] == "ava-hold-recover"
    assert hold_recovery.recovery_session() == captured["session"]
    assert "python -m cli.commands._hold_recover --operation" in captured["shell"]
    assert f"--acquired-at {_AT.isoformat()}" in captured["shell"]
    assert "[session-exit] rc=$?" in captured["shell"]
    assert str(log_path) in captured["shell"]
    assert "python -m cli.commands._hold_recover" in captured["native"]
