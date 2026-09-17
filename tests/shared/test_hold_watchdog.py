"""shared.hold_watchdog — the orphan-hold completion verdict and its budget (task #3887).

The condition table is the contract: every gate in the module docstring gets a
passing and a refusing case, and the CAS semantics mirror #3142's budget
(one attempt per generation, cooldown, never refunded). The tests drive the
module's own seams — every probe is monkeypatched at the boundary the
production code reads through, so a rename breaks them rather than letting a
stand-in drift.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from shared import hold_watchdog as hw
from shared import pause_owner
from shared.maintenance_state import MaintenanceHold
from shared.os_watchdog_probe import HeldStopState

_AT = datetime(2026, 9, 17, 22, 27, 46, tzinfo=UTC)
_OLD_ENOUGH = _AT.timestamp() + 3600.0


def _snapshot(
    phase: str = "stopped",
    *,
    failures: dict[int, str] | None = None,
    holder: str = "wsl:pid1",
    acquired_at: datetime = _AT,
) -> pause_owner.PauseOwnerSnapshot:
    return pause_owner.PauseOwnerSnapshot(
        status="paused",
        holder=holder,
        acquired_at=acquired_at,
        maintenance=MaintenanceHold(phase, failures=dict(failures or {})),  # type: ignore[arg-type]
    )


@pytest.fixture(autouse=True)
def _clean_attempt_state() -> Iterator[None]:
    """The root conftest shares one tmp $AVA_HOME across the session; this
    module owns the attempt CAS file inside it.

    Paths resolve at setup, not teardown: the unreadable-config test
    deliberately breaks settings reads, and a teardown that resolved through
    settings would fail after it (and mask the test's real result).
    """
    paths = (hw.attempt_path(), hw.attempt_lock_path())
    for path in paths:
        path.unlink(missing_ok=True)
    yield
    for path in paths:
        path.unlink(missing_ok=True)


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every gate passing: a stopped hold, dead dead-driver, nothing executing."""
    monkeypatch.setattr(pause_owner, "read", _snapshot)

    def _dead(_driver: object) -> str:
        return "dead"

    monkeypatch.setattr("shared.hold_driver.liveness", _dead)
    monkeypatch.setattr(hw, "_executing_block", lambda: None)
    monkeypatch.setattr(hw, "_lifecycle_busy", lambda: False)
    monkeypatch.setattr(hw, "_intended_expiry", lambda: None)
    monkeypatch.setattr("shared.os_watchdog_probe.held_stop_state", lambda: HeldStopState.ABSENT)


# --- the hold gate ----------------------------------------------------------


def test_no_journal_reads_as_no_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pause_owner, "read", lambda: pause_owner.PauseOwnerSnapshot(status="inactive")
    )
    verdict = hw.evaluate()
    assert verdict.kind is hw.VerdictKind.NO_HOLD
    assert verdict.code == "no-hold"


def test_a_resumed_journal_reads_as_no_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pause_owner, "read", lambda: pause_owner.PauseOwnerSnapshot(status="resumed")
    )
    assert hw.evaluate().kind is hw.VerdictKind.NO_HOLD


def test_an_unreadable_journal_backs_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing evidence is never a completion license: an invalid journal
    could be hiding a real hold."""
    monkeypatch.setattr(
        pause_owner, "read", lambda: pause_owner.PauseOwnerSnapshot(status="invalid")
    )
    verdict = hw.evaluate()
    assert verdict.kind is hw.VerdictKind.BACK_OFF
    assert verdict.code == "hold-unreadable"


@pytest.mark.parametrize("phase", ["preparing", "draining", "drained", "ready"])
def test_other_phases_are_refused(phase: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pause_owner, "read", lambda: _snapshot(phase))
    verdict = hw.evaluate()
    assert verdict.kind is hw.VerdictKind.BACK_OFF
    assert verdict.code == "phase"


def test_failed_receipts_defer_to_repair(armed: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pause_owner, "read", lambda: _snapshot(failures={228: "flush"}))
    verdict = hw.evaluate()
    assert verdict.kind is hw.VerdictKind.BACK_OFF
    assert verdict.code == "failures"


def test_the_switch_off_disables_the_mechanism(
    armed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.config import settings

    monkeypatch.setattr(settings.gateway, "stranded_hold_recovery", False)
    verdict = hw.evaluate()
    assert verdict.kind is hw.VerdictKind.BACK_OFF
    assert verdict.code == "disabled"


def test_the_switch_read_failure_defaults_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """A full-stop shape must never silently restore the blackout because a
    config read failed (the field default is ON; user ruling on #3887)."""

    class _Unreadable:
        def __getattr__(self, name: str) -> object:
            raise RuntimeError(f"config unreadable ({name})")

    monkeypatch.setattr("shared.config.settings", _Unreadable())
    assert hw.enabled() is True
    assert hw.min_age_seconds() == hw.DEFAULT_MIN_AGE_SECONDS
    assert hw.cooldown_seconds() == hw.DEFAULT_COOLDOWN_SECONDS


# --- the driver gate --------------------------------------------------------


@pytest.mark.parametrize("reading", ["alive", "missing", "unreadable"])
def test_non_dead_driver_identity_never_acts(
    reading: str, armed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a birth-checked DEAD is a license; a live shepherd is work in
    progress and missing evidence is missing evidence (task #3270)."""

    def _reading(_driver: object) -> str:
        return reading

    monkeypatch.setattr("shared.hold_driver.liveness", _reading)
    verdict = hw.evaluate()
    assert verdict.kind is hw.VerdictKind.BACK_OFF
    assert verdict.code == f"driver-{reading}"


# --- the executing gate -----------------------------------------------------


def test_a_live_executing_signal_defers(armed: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hw, "_executing_block", lambda: ("orchestration-session", "ava-updater"))
    verdict = hw.evaluate()
    assert verdict.kind is hw.VerdictKind.BACK_OFF
    assert verdict.code == "orchestration-session"


def test_handoff_pending_defers(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared import updater_handoff

    snapshot = updater_handoff.UpdaterHandoffSnapshot(
        status="pending", generation="g1", expired=False
    )

    def _read(**_kw: object) -> updater_handoff.UpdaterHandoffSnapshot:
        return snapshot

    monkeypatch.setattr(updater_handoff, "read", _read)
    assert hw._executing_block() == ("handoff-pending", "updater handoff g1 is pending")


def test_handoff_invalid_defers(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared import updater_handoff

    def _invalid_read(**_kw: object) -> updater_handoff.UpdaterHandoffSnapshot:
        return updater_handoff.UpdaterHandoffSnapshot(status="invalid")

    monkeypatch.setattr(updater_handoff, "read", _invalid_read)
    assert hw._executing_block() == (
        "handoff-unreadable",
        "the updater handoff journal is unreadable",
    )


def test_handoff_running_with_dead_owner_does_not_defer(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared import updater_handoff

    snapshot = updater_handoff.UpdaterHandoffSnapshot(
        status="running", generation="g2", expired=True
    )

    def _read(**_kw: object) -> updater_handoff.UpdaterHandoffSnapshot:
        return snapshot

    monkeypatch.setattr(updater_handoff, "read", _read)

    def _not_live(_snapshot: object) -> bool:
        return False

    monkeypatch.setattr(updater_handoff, "owner_is_live", _not_live)
    monkeypatch.setattr(hw, "_live_orchestration_session", lambda: None)
    monkeypatch.setattr(hw, "_updater_lock_held", lambda: False)
    assert hw._executing_block() is None


def test_an_expired_pending_handoff_does_not_defer(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared import updater_handoff

    snapshot = updater_handoff.UpdaterHandoffSnapshot(
        status="pending", generation="g3", expired=True
    )

    def _read(**_kw: object) -> updater_handoff.UpdaterHandoffSnapshot:
        return snapshot

    monkeypatch.setattr(updater_handoff, "read", _read)
    monkeypatch.setattr(hw, "_live_orchestration_session", lambda: None)
    monkeypatch.setattr(hw, "_updater_lock_held", lambda: False)
    assert hw._executing_block() is None


def test_a_live_orchestration_session_defers_including_hold_recover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ava-hold-recover` is #3142's completion session: this mechanism must
    stand down while the sibling completion runs."""
    from shared import updater_handoff

    def _inactive_read(**_kw: object) -> updater_handoff.UpdaterHandoffSnapshot:
        return updater_handoff.UpdaterHandoffSnapshot(status="inactive")

    monkeypatch.setattr(updater_handoff, "read", _inactive_read)
    seen: list[str] = []

    class _Backend:
        def has_session(self, name: str) -> bool:
            seen.append(name)
            return name == "ava-hold-recover"

    monkeypatch.setattr("shared.session_backend.get_backend", _Backend)
    assert hw._executing_block() == (
        "orchestration-session",
        "orchestration session ava-hold-recover is in flight",
    )
    assert "ava-updater" in seen  # the whole family is asked, not just the hit


def test_an_unreadable_session_probe_defers(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared import updater_handoff

    def _inactive_read(**_kw: object) -> updater_handoff.UpdaterHandoffSnapshot:
        return updater_handoff.UpdaterHandoffSnapshot(status="inactive")

    monkeypatch.setattr(updater_handoff, "read", _inactive_read)

    def _explode() -> None:
        raise RuntimeError("no backend")

    monkeypatch.setattr("shared.session_backend.get_backend", _explode)
    assert hw._executing_block() == (
        "orchestration-session",
        "orchestration session liveness is unreadable",
    )


def test_the_updater_lock_probe_does_not_create_a_missing_lock_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared import host_deploy_state

    missing = tmp_path / "updater.lock"
    monkeypatch.setattr(host_deploy_state, "_updater_lock_path", lambda: missing)
    assert hw._updater_lock_held() is False
    assert not missing.exists()


def test_the_updater_lock_probe_reads_a_held_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held updater mutex is the authoritative "an updater is alive" read."""
    from shared import host_deploy_state

    path = tmp_path / "updater.lock"
    path.write_text("0")

    def _held() -> bool:
        return False

    monkeypatch.setattr(host_deploy_state, "_updater_lock_path", lambda: path)
    monkeypatch.setattr(host_deploy_state, "try_acquire_updater_lock", _held)
    assert hw._updater_lock_held() is True


def test_the_updater_lock_probe_releases_what_it_took(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared import host_deploy_state

    path = tmp_path / "updater.lock"
    path.write_text("0")
    released: list[bool] = []
    monkeypatch.setattr(host_deploy_state, "_updater_lock_path", lambda: path)
    monkeypatch.setattr(host_deploy_state, "try_acquire_updater_lock", lambda: True)
    monkeypatch.setattr(host_deploy_state, "release_updater_lock", lambda: released.append(True))
    assert hw._updater_lock_held() is False
    assert released == [True]


# --- the held-stop and lifecycle gates --------------------------------------


@pytest.mark.parametrize(
    "state", [HeldStopState.STALE, HeldStopState.ABSENT, HeldStopState.UNREADABLE]
)
def test_non_fresh_markers_evaluate(
    state: HeldStopState, armed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shared.os_watchdog_probe.held_stop_state", lambda: state)
    assert hw.evaluate(now=_OLD_ENOUGH).kind is hw.VerdictKind.ELIGIBLE


def test_a_fresh_marker_defers(armed: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stop is mid-flight; it owns its window and clears its own marker."""
    monkeypatch.setattr("shared.os_watchdog_probe.held_stop_state", lambda: HeldStopState.FRESH)
    verdict = hw.evaluate()
    assert verdict.kind is hw.VerdictKind.BACK_OFF
    assert verdict.code == "held-stop"


def test_the_real_lifecycle_probe_sees_a_held_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe is a real acquire-and-release against the real lock path:
    while a start/stop holds it, the watchdog must stand down (boot mutual
    exclusion — never double-master)."""
    from shared.platform import file_lock
    from shared.ui_update_state import lifecycle_lock_path

    with file_lock(lifecycle_lock_path(), timeout_s=1.0):
        assert hw._lifecycle_busy() is True
    assert hw._lifecycle_busy() is False


# --- the bound ---------------------------------------------------------------


def test_a_young_hold_defers(armed: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pause_owner, "read", lambda: _snapshot(acquired_at=_AT + timedelta(seconds=100))
    )
    verdict = hw.evaluate(now=_AT.timestamp() + 900.0)
    assert verdict.kind is hw.VerdictKind.BACK_OFF
    assert verdict.code == "young"
    assert verdict.due_in_s is not None and verdict.due_in_s > 0


def test_the_age_floor_is_read_from_config(armed: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.config import settings

    monkeypatch.setattr(settings.gateway, "hold_watchdog_min_age_seconds", 60.0)
    monkeypatch.setattr(
        pause_owner, "read", lambda: _snapshot(acquired_at=_AT + timedelta(seconds=100))
    )
    assert hw.evaluate(now=_AT.timestamp() + 200.0).kind is hw.VerdictKind.ELIGIBLE


def test_the_configured_floor_reaches_evaluate_unextended(
    armed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The floor is exactly the configured value: at T-1s the verdict waits,
    at T it acts."""
    assert hw.evaluate(now=_AT.timestamp() + hw.min_age_seconds() - 1).code == "young"
    assert hw.evaluate(now=_AT.timestamp() + hw.min_age_seconds()).kind is hw.VerdictKind.ELIGIBLE


def test_an_intended_expiry_leads_the_floor(armed: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Forward-compat for #3724 §2.1: a declared intended lifetime IS the
    completion moment, even when it lands before the default floor."""
    monkeypatch.setattr(hw, "_intended_expiry", lambda: _AT.timestamp() + 60.0)
    verdict = hw.evaluate(now=_AT.timestamp() + 61.0)
    assert verdict.kind is hw.VerdictKind.ELIGIBLE
    verdict = hw.evaluate(now=_AT.timestamp() + 59.0)
    assert verdict.kind is hw.VerdictKind.BACK_OFF
    assert "intended lifetime" in verdict.detail


def test_the_intended_expiry_reader_is_tolerant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Absent / malformed reads as absent — it is a bound refinement, never a
    safety signal."""
    import json

    state = pause_owner.state_path()
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"maintenance": {"intended_expires_at": "not-a-date"}}))
    assert hw._intended_expiry() is None
    state.write_text(
        json.dumps({"maintenance": {"intended_expires_at": "2026-09-17T23:00:00+00:00"}})
    )
    assert hw._intended_expiry() == datetime(2026, 9, 17, 23, 0, tzinfo=UTC).timestamp()
    state.write_text("{}")
    assert hw._intended_expiry() is None
    state.unlink(missing_ok=True)


# --- the verdict's identity --------------------------------------------------


def test_an_eligible_verdict_carries_the_episode(armed: None) -> None:
    verdict = hw.evaluate(now=_OLD_ENOUGH)
    assert verdict.kind is hw.VerdictKind.ELIGIBLE
    assert verdict.episode == f"wsl:pid1|{_AT.isoformat()}"


# --- the ladder --------------------------------------------------------------


def test_the_ladder_mirrors_the_recovery_recipe() -> None:
    assert hw.ladder_for_phase("stopping") == ("stop", "start", "resume")
    assert hw.ladder_for_phase("stopped") == ("start", "resume")
    assert hw.ladder_for_phase("starting") == ("start", "resume")
    with pytest.raises(ValueError, match="no completion ladder"):
        hw.ladder_for_phase("ready")


def test_the_phase_set_matches_the_sibling_mechanism() -> None:
    """#3142 and this mechanism must agree on which phases are completable —
    two answers to that question is how one completes what the other refuses."""
    from ops import hold_recovery

    assert hw.RECOVERABLE_PHASES == hold_recovery.RECOVERABLE_PHASES


def test_the_compiled_defaults_match_the_settings_fields() -> None:
    """The unreadable-config fallbacks are the same values the fields default
    to; drift between them silently changes behavior in a full-stop shape."""
    from shared.config import settings

    assert settings.gateway.hold_watchdog_min_age_seconds == hw.DEFAULT_MIN_AGE_SECONDS
    assert settings.gateway.hold_watchdog_cooldown_seconds == hw.DEFAULT_COOLDOWN_SECONDS


# --- the attempt CAS ---------------------------------------------------------


def _episode(suffix: str = "a") -> str:
    return f"wsl:pid1|2026-09-17T22:27:46+00:00|{suffix}"


def test_reserve_records_the_first_attempt() -> None:
    assert hw.reserve_attempt(_episode(), max_attempts=1, cooldown_s=900.0, now=1000.0) == 1
    state = hw.read_attempt()
    assert state is not None
    assert state.episode == _episode()
    assert state.attempts == 1
    assert state.attempted_at == 1000.0


def test_a_spent_budget_is_not_refunded() -> None:
    assert hw.reserve_attempt(_episode(), max_attempts=1, cooldown_s=900.0, now=1000.0) == 1
    assert hw.reserve_attempt(_episode(), max_attempts=1, cooldown_s=900.0, now=99999.0) is None


def test_a_new_episode_resets_the_budget() -> None:
    assert hw.reserve_attempt(_episode("a"), max_attempts=1, cooldown_s=900.0, now=1000.0) == 1
    assert hw.reserve_attempt(_episode("b"), max_attempts=1, cooldown_s=900.0, now=1001.0) == 1
    state = hw.read_attempt()
    assert state is not None and state.episode == _episode("b")


def test_the_cooldown_gates_a_second_attempt_within_the_generation() -> None:
    assert hw.reserve_attempt(_episode(), max_attempts=2, cooldown_s=900.0, now=1000.0) == 1
    assert hw.reserve_attempt(_episode(), max_attempts=2, cooldown_s=900.0, now=1500.0) is None
    assert hw.reserve_attempt(_episode(), max_attempts=2, cooldown_s=900.0, now=1900.0) == 2


def test_finish_records_the_outcome_on_the_live_generation() -> None:
    hw.reserve_attempt(_episode(), max_attempts=1, cooldown_s=900.0, now=1000.0)
    assert hw.finish_attempt(_episode(), "completed", now=1005.0) is True
    state = hw.read_attempt()
    assert state is not None and state.note == "completed" and state.attempts == 1


def test_finish_on_a_replaced_generation_is_a_no_op() -> None:
    hw.reserve_attempt(_episode("a"), max_attempts=1, cooldown_s=900.0, now=1000.0)
    assert hw.finish_attempt(_episode("b"), "moot", now=1005.0) is False
    state = hw.read_attempt()
    assert state is not None and state.note is None


def test_a_corrupt_cas_file_never_fails_open() -> None:
    """The one-attempt bound must not evaporate because the CAS file is
    unreadable: the mechanism stands down instead."""
    hw.attempt_path().parent.mkdir(parents=True, exist_ok=True)
    hw.attempt_path().write_text("{not json")
    assert hw.reserve_attempt(_episode(), max_attempts=1, cooldown_s=900.0, now=1000.0) is None
    with pytest.raises(hw.AttemptStateUnreadableError):
        hw.read_attempt()
