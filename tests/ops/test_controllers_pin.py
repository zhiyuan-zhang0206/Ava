"""Pin controller reconcile behavior — ported from the watchdog daemon's
`_check_pin_drift` / `_warn_gateway_off_pin` tests when the gate moved to
`ops.controllers.pin`.

Covers the agent-runner acting half (self-heal to the pin: unknown pins are
fetched first, then judged, then healed; five guards: lock, in-flight local
update, backoff, cooldown, POST-failure) and the gateway warn-only half (never
acts, never gates), plus the PinController role dispatch.

The in-flight-update guard is issue #1074: the lease is blind to a watchdog-spawned
`ava-updater`, and being off-pin is that updater's own mid-flight state, so the pin
heal used to force the checkout back underneath a live one — six alternating resets
in 111 minutes on prod, each updater dying on a schema check against a tree it had
not checked out.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ops.controllers import pin, update_trigger
from ops.controllers.base import BlockScope
from shared.cluster_lock import DeployLease, settle_note

_THIS_HOST = "laptop-host"


def _relation(value: str):
    """A typed prod_source_pin_relation stand-in (the fixture shas are fake, so
    real git ancestry cannot decide them)."""

    def _r(_pin: str, _head: str) -> str:
        return value

    return _r


def _lease(*, note: str | None) -> DeployLease:
    """A live lease as `read_update_lease` returns it. `note=None` is a rollout
    executing right now; a settle note is a stated waiting period with nobody
    executing under it."""
    return DeployLease(
        holder="gateway-host:pid65237", held_for_s=120.0, expires_in_s=900.0, note=note
    )


@pytest.fixture(autouse=True)
def _reset_cooldown() -> None:
    update_trigger.reset_cooldown()


@pytest.fixture
def pin_drift_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str | None]:
    """A happy self-heal context: no update lock, isolated backoff file, a
    trigger_update spy returning success. Returns the spy's captured target_sha list."""
    monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: None)
    monkeypatch.setattr("shared.machine.machine_name", lambda: _THIS_HOST)
    monkeypatch.setattr("ops.cluster.current_orchestration", lambda: None)
    # No recorded update by default: the recent-update guard passes through.
    monkeypatch.setattr("shared.last_update.read_last_update", lambda: None)
    monkeypatch.setattr(pin, "_pin_heal_attempt_path", lambda: tmp_path / "pin_heal_attempt")
    # A strictly-behind HEAD: the heal is a catch-up, not a downgrade. Tests
    # for the ahead / unknown relations override this per-case.
    monkeypatch.setattr(pin, "prod_source_pin_relation", _relation("behind"))
    # The unknown branch fetches before re-judging; default success so no test
    # reaches real git. Tests that care override with their own recorder.
    monkeypatch.setattr(pin, "prod_source_fetch", lambda *_: True)  # pyright: ignore[reportUnknownArgumentType]
    spawned: list[str | None] = []

    def _spy(target_sha: str | None = None) -> bool:
        spawned.append(target_sha)
        return True

    monkeypatch.setattr(pin, "trigger_update", _spy)
    return spawned


def test_self_heals_when_off_pin(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, pin_drift_env: list
) -> None:
    """HEAD != pin → force-update to the pin (target_sha=pin) + block the tick."""
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")
    with caplog.at_level("WARNING"):
        assert pin.check_pin_drift() is True
    assert pin_drift_env == ["abc1234"]  # force-checkout to the pin, not origin/main
    assert any("off-pin" in r.message for r in caplog.records)


def test_defers_while_update_lock_held(
    monkeypatch: pytest.MonkeyPatch, pin_drift_env: list
) -> None:
    """A not-yet-paused host must not self-heal while a cluster update holds the lock."""
    monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: _lease(note=None))
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")
    assert pin.check_pin_drift() is False
    assert pin_drift_env == []


def test_does_not_downgrade_when_head_ahead_of_pin(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, pin_drift_env: list
) -> None:
    """HEAD contains the pin (a failed rollout landed the code but never advanced
    the pin — the 2026-08-25 shape) is converged at-or-above the pin: the heal
    must NOT force-checkout the stale pin underneath the landed gateway."""
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")
    monkeypatch.setattr(pin, "prod_source_pin_relation", _relation("ahead"))
    with caplog.at_level("WARNING"):
        assert pin.check_pin_drift() is False
    assert pin_drift_env == []  # no spawn, no downgrade
    assert any("AHEAD of pin" in r.message for r in caplog.records)


def test_defers_when_pin_still_unknown_after_fetch(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, pin_drift_env: list
) -> None:
    """unknown → the controller fetches the track ref and re-judges; a pin that
    is STILL unknown after the fetch (not on the track ref, or the fetch failed)
    defers instead of force-checking-out blind (a checkout is the wrong default
    when the pin may be older than HEAD)."""
    fetches: list[tuple[str, ...]] = []
    monkeypatch.setattr(pin, "prod_source_fetch", lambda *refs: fetches.append(refs) or True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")
    monkeypatch.setattr(pin, "prod_source_pin_relation", _relation("unknown"))
    with caplog.at_level("WARNING"):
        assert pin.check_pin_drift() is False
    assert fetches == [("origin", "main")]
    assert pin_drift_env == []
    assert any("ancestry unknown even after fetching" in r.message for r in caplog.records)


def test_fetches_then_heals_when_pin_becomes_resolvable(
    monkeypatch: pytest.MonkeyPatch, pin_drift_env: list
) -> None:
    """unknown → the fetch brings the pin → the re-judged relation is behind →
    the heal runs. This is the excluded-runner convergence path (#621): a host
    that never had the new pin fetched used to defer forever."""
    calls: list[str] = []

    def _r(_pin: str, _head: str) -> str:
        calls.append("judge")
        return "unknown" if len(calls) == 1 else "behind"

    monkeypatch.setattr(pin, "prod_source_pin_relation", _r)
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")
    assert pin.check_pin_drift() is True
    assert calls == ["judge", "judge"]  # judged once, fetched, judged again
    assert pin_drift_env == ["abc1234"]


def test_defers_when_fetch_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, pin_drift_env: list
) -> None:
    """A wedged network (fetch returns False) leaves the relation unknown — the
    deferral stands and nothing spawns; the next tick retries."""
    monkeypatch.setattr(pin, "prod_source_fetch", lambda *_: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")
    monkeypatch.setattr(pin, "prod_source_pin_relation", _relation("unknown"))
    with caplog.at_level("WARNING"):
        assert pin.check_pin_drift() is False
    assert pin_drift_env == []
    assert any("ancestry unknown even after fetching" in r.message for r in caplog.records)


def test_does_not_downgrade_when_fetch_reveals_ahead(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, pin_drift_env: list
) -> None:
    """The pin=floor semantics survive the fetch: a pin that resolves to an
    ancestor of HEAD after the fetch is still never checked-out under the head
    (the 2026-08-25 downgrade shape) — converged at-or-above the pin."""
    calls: list[str] = []

    def _r(_pin: str, _head: str) -> str:
        calls.append("judge")
        return "unknown" if len(calls) == 1 else "ahead"

    monkeypatch.setattr(pin, "prod_source_pin_relation", _r)
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")
    with caplog.at_level("WARNING"):
        assert pin.check_pin_drift() is False
    assert pin_drift_env == []  # no spawn, no downgrade
    assert any("AHEAD of pin" in r.message for r in caplog.records)


def test_heals_when_head_behind_pin(monkeypatch: pytest.MonkeyPatch, pin_drift_env: list) -> None:
    """A HEAD strictly behind the pin (this host missed a rollout) still heals —
    that is a catch-up, not a downgrade."""
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")
    monkeypatch.setattr(pin, "prod_source_pin_relation", _relation("behind"))
    assert pin.check_pin_drift() is True
    assert pin_drift_env == ["abc1234"]


def test_heals_when_head_diverged_from_pin(
    monkeypatch: pytest.MonkeyPatch, pin_drift_env: list
) -> None:
    """A diverged HEAD (rebase / force-push) is not on the pinned line at all —
    force-checkout to the pin is the correct heal."""
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")
    monkeypatch.setattr(pin, "prod_source_pin_relation", _relation("diverged"))
    assert pin.check_pin_drift() is True
    assert pin_drift_env == ["abc1234"]


def test_backoff_blocks_repeat_to_same_pin(
    monkeypatch: pytest.MonkeyPatch, pin_drift_env: list
) -> None:
    """A self-heal to a pin that didn't land is not retried within the backoff window
    (survives the update-forced restart)."""
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")
    assert pin.check_pin_drift() is True
    assert pin_drift_env == ["abc1234"]
    update_trigger.reset_cooldown()  # clear the process cooldown
    assert pin.check_pin_drift() is False  # same pin, still off → backoff blocks
    assert pin_drift_env == ["abc1234"]  # no second spawn


def test_records_failed_attempt_when_spawn_fails(
    monkeypatch: pytest.MonkeyPatch, pin_drift_env: list
) -> None:
    """A failed gateway POST records an ok=False attempt (operator visibility) and
    falls back to a local spawn; when that also fails, the tick is not blocked."""
    monkeypatch.setattr(pin, "trigger_update", lambda **_kw: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")

    def _local_boom(*, target_sha: str | None = None) -> object:
        raise RuntimeError("local spawn failed")

    monkeypatch.setattr("ops.cluster.spawn_update", _local_boom)
    assert pin.check_pin_drift() is False
    rec = json.loads(pin._pin_heal_attempt_path().read_text())
    assert rec["ok"] is False
    # The LOCAL failure is the round's final outcome, so it is what the record keeps —
    # the same shape `_spawn_update_locally` already gives the schema controller, and
    # the reason the bare-Exception branch had to start recording at all (issue #1074:
    # without a record it armed no backoff and retried at the cooldown cadence forever).
    assert rec["last_error"].startswith("local spawn failed:")


def test_skips_self_heal_in_cooldown(monkeypatch: pytest.MonkeyPatch, pin_drift_env: list) -> None:
    """Off-pin but an update was just spawned this process → no second spawn."""
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")
    update_trigger._last_update_spawn = time.monotonic()  # just spawned
    assert pin.check_pin_drift() is False
    assert pin_drift_env == []


def test_noop_when_on_pin(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, pin_drift_env: list
) -> None:
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "abc1234")
    with caplog.at_level("WARNING"):
        assert pin.check_pin_drift() is False
    assert caplog.records == []
    assert pin_drift_env == []


def test_silent_when_no_pin(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, pin_drift_env: list
) -> None:
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: None)
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")
    with caplog.at_level("WARNING"):
        assert pin.check_pin_drift() is False
    assert caplog.records == []


def test_warns_when_head_unreadable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, pin_drift_env: list
) -> None:
    """Pinned but HEAD unreadable → could be off-pin and can't tell, a WARNING."""
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: None)
    with caplog.at_level("WARNING"):
        assert pin.check_pin_drift() is False
    assert any("HEAD unreadable" in r.message for r in caplog.records)


def test_silent_when_db_unreachable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, pin_drift_env: list
) -> None:
    """A transient OperationalError degrades to no signal, never an error."""
    import psycopg

    def _boom() -> str | None:
        raise psycopg.OperationalError("db down")

    monkeypatch.setattr(pin, "get_cluster_target_sha", _boom)
    with caplog.at_level("WARNING"):
        assert pin.check_pin_drift() is False
    assert caplog.records == []


def test_logs_on_non_operational_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, pin_drift_env: list
) -> None:
    """A real bug (missing pin row / schema error) is surfaced loudly, never
    swallowed as 'no pin', and never crashes the tick."""

    def _boom() -> str | None:
        raise RuntimeError("cluster_pin singleton row missing")

    monkeypatch.setattr(pin, "get_cluster_target_sha", _boom)
    with caplog.at_level("ERROR"):
        assert pin.check_pin_drift() is False
    assert any("reading cluster pin failed" in r.message for r in caplog.records)


# ─── gateway warn-only half ──────────────────────────────────────────────────


def test_warn_gateway_off_pin_warns_but_never_acts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The gateway half warns on drift and never self-heals; always returns False."""
    spawned: list[object] = []
    monkeypatch.setattr(pin, "trigger_update", lambda *a, **kw: spawned.append((a, kw)) or True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")
    with caplog.at_level("WARNING"):
        assert pin.warn_gateway_off_pin() is False
    assert spawned == []
    assert any("gateway off-pin" in r.message for r in caplog.records)


def test_warn_gateway_off_pin_silent_when_on_pin(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "abc1234")
    with caplog.at_level("WARNING"):
        assert pin.warn_gateway_off_pin() is False
    assert caplog.records == []


# ─── PinController role dispatch ──────────────────────────────────────────────


def test_controller_agent_runner_acts(monkeypatch: pytest.MonkeyPatch) -> None:
    """agent-runner role runs the acting half; a spawned force-update blocks the WHOLE
    roster (it rewrites the checkout and restarts every process), so BlockScope.ALL."""
    monkeypatch.setattr(pin, "check_pin_drift", lambda: True)
    res = pin.PinController().reconcile("agent-runner")
    assert res.dimension == "pin" and res.blocks is BlockScope.ALL and res.acted is True

    monkeypatch.setattr(pin, "check_pin_drift", lambda: False)
    res = pin.PinController().reconcile("agent-runner")
    assert res.blocks is BlockScope.NONE and res.acted is False


def test_controller_gateway_warns_never_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """gateway role runs the warn-only half; never blocks, never claims to act."""
    called: list[bool] = []
    acting: list[bool] = []
    monkeypatch.setattr(pin, "warn_gateway_off_pin", lambda: called.append(True) or False)
    monkeypatch.setattr(pin, "check_pin_drift", lambda: acting.append(True) or True)
    res = pin.PinController().reconcile("gateway")
    assert res.blocks is BlockScope.NONE and res.acted is False
    assert called == [True]
    assert acting == []  # the acting half is agent-runner-only


# ─── the pin does not fight an update that is already running (issue #1074) ──


def test_defers_while_a_local_update_is_in_flight(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, pin_drift_env: list
) -> None:
    """A watchdog-spawned `ava-updater` takes NO lease, so the lock check above reads
    "free" while an update is mid-checkout — and off-pin is precisely what that update
    looks like on its way to the target. Forcing the checkout back under it is what
    made every `origin/main` leg die on a `Schema ahead of code` it had just fixed."""
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")
    monkeypatch.setattr("ops.cluster.current_orchestration", lambda: "update")
    with caplog.at_level("INFO"):
        assert pin.check_pin_drift() is False
    assert pin_drift_env == [], "no force-update while one is already running"
    assert any("mid-move" in r.message for r in caplog.records)


def test_defers_when_the_orchestration_session_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, pin_drift_env: list
) -> None:
    """Unreadable evidence is not evidence of absence. A force-checkout is the most
    destructive thing this controller does, so it declines rather than guesses."""
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")

    def _boom() -> str | None:
        raise RuntimeError("session spawn not available")

    monkeypatch.setattr("ops.cluster.current_orchestration", _boom)
    assert pin.check_pin_drift() is False
    assert pin_drift_env == []


# ─── the settle hold that names this host (issue #1020) ─────────────────────


def test_heals_under_a_settle_hold_that_names_this_host(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, pin_drift_env: list
) -> None:
    """The checkout half of the mutual wait. A settle hold names the hosts whose
    convergence it is waiting for and nobody executes under it, so an off-pin host it
    names must land the checkout the hold is waiting on rather than idle out the TTL —
    `settle_hosts_converged` requires `head_sha == pin` before it will release."""
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")
    monkeypatch.setattr(
        "shared.cluster_lock.read_update_lease",
        lambda: _lease(note=settle_note([_THIS_HOST, "other-box"])),
    )
    with caplog.at_level("WARNING"):
        assert pin.check_pin_drift() is True
    assert pin_drift_env == ["abc1234"]
    assert any("settle hold waiting for THIS host" in r.message for r in caplog.records)


def test_defers_under_a_settle_hold_that_names_another_host(
    monkeypatch: pytest.MonkeyPatch, pin_drift_env: list
) -> None:
    """The permission is scoped to the named host, exactly as in the code controller."""
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")
    monkeypatch.setattr(
        "shared.cluster_lock.read_update_lease", lambda: _lease(note=settle_note(["other-box"]))
    )
    assert pin.check_pin_drift() is False
    assert pin_drift_env == []


def test_defers_under_a_hold_whose_note_cannot_be_parsed(
    monkeypatch: pytest.MonkeyPatch, pin_drift_env: list
) -> None:
    """An unreadable note yields an empty host set and therefore a deferral — the same
    direction `settle_hosts` already takes on the release path."""
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")
    monkeypatch.setattr(
        "shared.cluster_lock.read_update_lease", lambda: _lease(note="paused for maintenance")
    )
    assert pin.check_pin_drift() is False
    assert pin_drift_env == []


def test_a_settle_hold_naming_this_host_does_not_bypass_the_in_flight_guard(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, pin_drift_env: list
) -> None:
    """Where the two guards meet. The settle-hold exception is scoped to the LEASE
    guard, and the in-flight-update guard watches a signal a lease cannot carry — a
    watchdog-spawned updater takes none at all. So a hold naming this host says
    nothing about whether an updater is mid-checkout here, and a compose that let the
    exception carry past the second guard would force the checkout back underneath a
    live updater: #1074's flap, re-opened in the one window #1020 widens."""
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")
    monkeypatch.setattr(
        "shared.cluster_lock.read_update_lease", lambda: _lease(note=settle_note([_THIS_HOST]))
    )
    monkeypatch.setattr("ops.cluster.current_orchestration", lambda: "update")
    with caplog.at_level("INFO"):
        assert pin.check_pin_drift() is False
    assert pin_drift_env == [], "a settle hold must not license a checkout under a live updater"
    assert any("mid-move" in r.message for r in caplog.records)


# ───────────── recent-update guard (2026-08-02 mixed-tree incident) ─────────────


def _last_update(
    outcome: str,
    *,
    minutes_ago: float,
    failing_step: str | None = None,
) -> object:
    """A `LastUpdate` record ended `minutes_ago` (or None when RUNNING)."""
    from datetime import UTC, datetime, timedelta

    from shared.last_update import LastUpdate, UpdateOutcome

    started = datetime.now(UTC) - timedelta(minutes=minutes_ago + 5)
    ended = None if outcome == "running" else datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return LastUpdate(
        outcome=UpdateOutcome(outcome),
        failed=outcome != "clean",
        started_at=started,
        ended_at=ended,
        failing_step=failing_step,
    )


def test_defers_while_last_update_running(
    monkeypatch: pytest.MonkeyPatch, pin_drift_env: list
) -> None:
    """An update mid-flight (the lease may be released in its local-leg window, so
    the lease guard alone is not enough) blocks the self-heal: its checkout is
    exactly the concurrent git op that races the rollout's."""
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")
    monkeypatch.setattr(
        "shared.last_update.read_last_update",
        lambda: _last_update("running", minutes_ago=0),
    )
    assert pin.check_pin_drift() is False
    assert pin_drift_env == []


@pytest.mark.parametrize("outcome", ["recovered", "aborted", "orphaned"])
def test_defers_shortly_after_failed_outcome(
    monkeypatch: pytest.MonkeyPatch,
    pin_drift_env: list,
    caplog: pytest.LogCaptureFixture,
    outcome: str,
) -> None:
    """A rollout that just failed/recovered leaves HEAD possibly on the failed
    target while the operator retries; a self-heal checkout in that window is the
    2026-08-02 race. Defer within the recovery window regardless of outcome."""
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")
    monkeypatch.setattr(
        "shared.last_update.read_last_update",
        lambda: _last_update(outcome, minutes_ago=2, failing_step="gateway local update"),
    )
    with caplog.at_level("INFO"):
        assert pin.check_pin_drift() is False
    assert pin_drift_env == []
    assert any("deferring self-heal" in r.message for r in caplog.records)


def test_proceeds_after_incomplete_outcome(
    monkeypatch: pytest.MonkeyPatch, pin_drift_env: list
) -> None:
    """INCOMPLETE = the gateway landed and the pin advanced; a host still behind
    it is SUPPOSED to converge via this self-heal (the settle-hold path, #1020).
    Deferring would re-open the mutual wait."""
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")
    monkeypatch.setattr(
        "shared.last_update.read_last_update",
        lambda: _last_update("incomplete", minutes_ago=1),
    )
    assert pin.check_pin_drift() is True
    assert pin_drift_env == ["abc1234"]


def test_proceeds_after_clean_outcome(monkeypatch: pytest.MonkeyPatch, pin_drift_env: list) -> None:
    """CLEAN never co-occurs with off-pin in practice (HEAD is the pin), but if
    it does the heal is the right response — nothing is mid-move."""
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")
    monkeypatch.setattr(
        "shared.last_update.read_last_update",
        lambda: _last_update("clean", minutes_ago=1),
    )
    assert pin.check_pin_drift() is True
    assert pin_drift_env == ["abc1234"]


def test_proceeds_after_failed_outcome_outside_window(
    monkeypatch: pytest.MonkeyPatch, pin_drift_env: list
) -> None:
    """A failure older than the recovery window is history, not a race partner —
    the host really is drifted and the heal should run."""
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")
    monkeypatch.setattr(
        "shared.last_update.read_last_update",
        lambda: _last_update("recovered", minutes_ago=30),
    )
    assert pin.check_pin_drift() is True
    assert pin_drift_env == ["abc1234"]


def test_defers_when_last_update_unreadable(
    monkeypatch: pytest.MonkeyPatch, pin_drift_env: list
) -> None:
    """An unreadable record defers like every other unreadable guard input."""
    monkeypatch.setattr(pin, "get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr(pin, "prod_source_head_sha", lambda: "def5678")

    def _boom() -> None:
        raise RuntimeError("cluster_last_update singleton row missing")

    monkeypatch.setattr("shared.last_update.read_last_update", _boom)
    assert pin.check_pin_drift() is False
    assert pin_drift_env == []
