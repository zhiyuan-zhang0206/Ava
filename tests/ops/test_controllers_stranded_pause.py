"""Pause controller behavior — ported from the watchdog daemon's stranded-pause
tests when the gate moved to `ops.controllers.stranded_pause`.

Covers stranded-pause recovery and the PauseController gate (paused always blocks the
tick, unpaused proceeds).

Who OWNS a pause is a two-signal question (issue #1074). The lease covers a gateway
orchestration; a live local orchestration session covers the updater this host spawned
for itself, which takes no lease at all. On the lease alone the two could not be told
apart, so the single bound had to cover a self-update that might be quietly working —
and every pause whose owner was already dead (the state a failed updater leaves) paid
that same ten minutes while `ops.manager` refused to revive anything. An owned pause is
still declined outright; only the unowned wait shortened.

Which lease shapes count as an owner is the second question (issue #1116). An
*executing* lease does; a **settle hold naming this host** does not — nothing runs
under it, and the orchestration only wrote it because it had already proved this
host's pause lost its owner."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from ops.controllers import stranded_pause as sp
from ops.controllers.base import BlockScope
from shared.cluster_lock import DeployLease, settle_note

_THIS_HOST = "laptop-host"


@pytest.fixture
def fake_pause(monkeypatch: pytest.MonkeyPatch) -> Callable[[float | None], None]:
    """Plant a paused `host_deploy_state` row aged `seconds`; None = no row.

    The row is the R1 posture truth (Task #1021) the controller reads; the
    age controls `_stranded_pause_seconds` via the row's `updated_at`.
    """

    def _plant(age_s: float | None) -> None:
        if age_s is None:
            monkeypatch.setattr("shared.host_deploy_state.read", lambda machine=None, **_kw: None)  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
            return
        from datetime import UTC, datetime, timedelta

        from shared.host_deploy_state import HostDeployState

        state = HostDeployState(
            machine=_THIS_HOST,
            posture="paused",
            updated_at=datetime.now(UTC) - timedelta(seconds=age_s),
            updater_lease_expires_at=None,
        )
        monkeypatch.setattr("shared.host_deploy_state.read", lambda machine=None, **_kw: state)  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]

    return _plant


def _executing_lease() -> DeployLease:
    """A rollout mutating the cluster right now: `note is None` is what marks it, and
    `acquire_update_lock` NULLs the column for exactly that reason."""
    return DeployLease(holder="cloud:pid123", held_for_s=120.0, expires_in_s=1680.0, note=None)


def _settle_hold(*hosts: str) -> DeployLease:
    """The lease a rollout leaves behind when it exits with hosts still unconverged —
    nothing executing, `note` naming who it waits for."""
    return DeployLease(
        holder="cloud:pid123",
        held_for_s=300.0,
        expires_in_s=600.0,
        note=settle_note(list(hosts)),
    )


@pytest.fixture(autouse=True)
def _no_local_orchestration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test to "nothing is executing here". The tests about the local
    session say so explicitly."""
    monkeypatch.setattr("ops.cluster.current_orchestration", lambda: None)
    monkeypatch.setattr(sp.cluster_session, "live_orchestration_session", lambda: None)
    monkeypatch.setattr(sp.ui_update_state, "force_clear", lambda: False)


@pytest.fixture(autouse=True)
def _this_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whether a hold names THIS host is the whole discrimination, so the tests must
    not depend on the runner's real machine identity (which may not resolve at all)."""
    monkeypatch.setattr(sp, "machine_name", lambda: _THIS_HOST)


def test_self_unpauses_when_old(
    fake_pause: Callable[[float | None], None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Paused longer than the timeout + no update lock → self-unpause."""
    fake_pause(sp.STRANDED_PAUSE_TIMEOUT_S + 60)
    monkeypatch.setattr(sp, "read_update_lease", lambda: None)
    unpaused: list[bool] = []
    marker_clears: list[str] = []
    monkeypatch.setattr(sp, "unpause_local_cluster", lambda: unpaused.append(True))
    monkeypatch.setattr(
        sp.ui_update_state,
        "read",
        lambda: sp.ui_update_state.UiUpdateSnapshot(
            status="updating",
            generation="stale-generation",
            kind="rollout",
        ),
    )
    monkeypatch.setattr(sp.ui_update_state, "clear", marker_clears.append)
    with caplog.at_level("WARNING"):
        assert sp.recover_stranded_pause() is True
    assert unpaused == [True]
    assert marker_clears == ["stale-generation"]
    assert any("nothing is coming back" in r.message for r in caplog.records)


def test_defers_when_update_lock_held(
    fake_pause: Callable[[float | None], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paused past the UNOWNED bound but a live cluster-update lock still owns it → do
    NOT self-unpause (a slow rollout)."""
    fake_pause(sp.STRANDED_PAUSE_TIMEOUT_S + 60)
    monkeypatch.setattr(sp, "read_update_lease", _executing_lease)
    unpaused: list[bool] = []
    monkeypatch.setattr(sp, "unpause_local_cluster", lambda: unpaused.append(True))
    assert sp.recover_stranded_pause() is False
    assert unpaused == []


# ─── who owns the pause is two signals, not one (issue #1074) ────────────────


def test_defers_to_a_live_local_updater_that_holds_no_lease(
    fake_pause: Callable[[float | None], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A watchdog-spawned `ava-updater` takes no lease, and `spawn_update` pauses this
    host BEFORE spawning it. This is what LICENSES the short unowned bound rather than
    a standing bug the bound uncovered: on the lease alone, a pause held by a working
    local self-update and one whose owner is dead read identically, so shortening the
    wait without this signal would unpause a host mid-checkout — reviving the restarter
    the pause exists to keep down while old-code agents could respawn."""
    fake_pause(sp.STRANDED_PAUSE_TIMEOUT_S + 60)
    monkeypatch.setattr(sp, "read_update_lease", lambda: None)
    monkeypatch.setattr("ops.cluster.current_orchestration", lambda: "update")
    unpaused: list[bool] = []
    monkeypatch.setattr(sp, "unpause_local_cluster", lambda: unpaused.append(True))
    assert sp.recover_stranded_pause() is False
    assert unpaused == []


def test_live_detached_session_blocks_unpause_and_marker_clear(
    fake_pause: Callable[[float | None], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_pause(sp.STRANDED_PAUSE_TIMEOUT_S + 60)
    monkeypatch.setattr(sp, "read_update_lease", lambda: None)
    monkeypatch.setattr(sp.cluster_session, "live_orchestration_session", lambda: "ava-rollout")
    unpaused: list[bool] = []
    marker_clears: list[bool] = []
    monkeypatch.setattr(sp, "unpause_local_cluster", lambda: unpaused.append(True))
    monkeypatch.setattr(sp.ui_update_state, "force_clear", lambda: marker_clears.append(True))

    assert sp.recover_stranded_pause() is False
    assert unpaused == []
    assert marker_clears == []


def test_unpause_failure_preserves_marker_for_retry(
    fake_pause: Callable[[float | None], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_pause(sp.STRANDED_PAUSE_TIMEOUT_S + 60)
    monkeypatch.setattr(sp, "read_update_lease", lambda: None)
    marker_clears: list[bool] = []

    def _fail_unpause() -> None:
        raise RuntimeError("still paused")

    monkeypatch.setattr(sp, "unpause_local_cluster", _fail_unpause)
    monkeypatch.setattr(sp.ui_update_state, "force_clear", lambda: marker_clears.append(True))

    with pytest.raises(RuntimeError, match="still paused"):
        sp.recover_stranded_pause()
    assert marker_clears == []


def test_an_owned_pause_is_declined_however_long_it_has_run(
    fake_pause: Callable[[float | None], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An owned pause is declined outright, unchanged from before — the shortened bound
    must not become a way to unpause a host a rollout is still working on. The owner
    states end on their own: a crashed holder stops being a LIVE lease at its TTL, and
    a hung session is killed by the reaper that runs ahead of this controller."""
    fake_pause(sp.STRANDED_PAUSE_TIMEOUT_S * 20)
    monkeypatch.setattr(sp, "read_update_lease", _executing_lease)
    unpaused: list[bool] = []
    monkeypatch.setattr(sp, "unpause_local_cluster", lambda: unpaused.append(True))
    assert sp.recover_stranded_pause() is False
    assert unpaused == []


def test_an_unowned_pause_recovers_at_the_short_bound(
    fake_pause: Callable[[float | None], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state a failed updater leaves: its `ava start` recovery exits before the
    step that unlinks the flag, so the flag outlives its owner and `ops.manager`
    blocks every round on `pause`. Nothing is executing, so the only wait needed is
    the sub-second gap between `pause_local_cluster()` and the session spawn."""
    fake_pause(sp.STRANDED_PAUSE_TIMEOUT_S + 5)
    monkeypatch.setattr(sp, "read_update_lease", lambda: None)
    unpaused: list[bool] = []
    monkeypatch.setattr(sp, "unpause_local_cluster", lambda: unpaused.append(True))
    assert sp.recover_stranded_pause() is True
    assert unpaused == [True]


def test_retained_normal_recovery_blocks_generic_self_unpause(
    fake_pause: Callable[[float | None], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_pause(sp.STRANDED_PAUSE_TIMEOUT_S + 60)
    monkeypatch.setattr(sp, "read_update_lease", lambda: None)

    def refuse(_snapshot: sp.updater_handoff.UpdaterHandoffSnapshot) -> bool:
        return False

    monkeypatch.setattr(sp.updater_handoff, "allows_generic_recovery", refuse)
    unpaused: list[bool] = []
    monkeypatch.setattr(sp, "unpause_local_cluster", lambda: unpaused.append(True))

    assert sp.recover_stranded_pause() is False
    assert unpaused == []


# ─── which lease shapes own a pause (issue #1116) ────────────────────────────


def test_a_settle_hold_naming_this_host_does_not_own_the_pause(
    fake_pause: Callable[[float | None], None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The mutual wait: the hold exists BECAUSE this host has not converged, and this
    host would not converge because the hold read as an owner of its pause.

    The hold is not independent evidence — the `POLL_STALLED` verdict behind it is
    minted from `paused=true` AND `current_orchestration=null`, which is this
    controller's own unowned reading taken remotely and confirmed twice. Deferring to
    it means treating the orchestration's proof that nobody is coming back as proof
    that somebody is."""
    fake_pause(sp.STRANDED_PAUSE_TIMEOUT_S + 60)
    monkeypatch.setattr(sp, "read_update_lease", lambda: _settle_hold(_THIS_HOST))
    unpaused: list[bool] = []
    monkeypatch.setattr(sp, "unpause_local_cluster", lambda: unpaused.append(True))
    with caplog.at_level("WARNING"):
        assert sp.recover_stranded_pause() is True
    assert unpaused == [True]
    assert any("settle hold waiting for THIS host" in r.message for r in caplog.records)


def test_a_settle_hold_naming_a_different_host_still_owns_the_pause(
    fake_pause: Callable[[float | None], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The permission is exactly as narrow as `DeployLease.awaits`: a hold waiting on
    somebody else says nothing about this host's pause, so it still owns it."""
    fake_pause(sp.STRANDED_PAUSE_TIMEOUT_S + 60)
    monkeypatch.setattr(sp, "read_update_lease", lambda: _settle_hold("wsl", "windows-box"))
    unpaused: list[bool] = []
    monkeypatch.setattr(sp, "unpause_local_cluster", lambda: unpaused.append(True))
    assert sp.recover_stranded_pause() is False
    assert unpaused == []


def test_a_settle_hold_naming_this_host_still_defers_to_a_live_local_updater(
    fake_pause: Callable[[float | None], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the LEASE half is narrowed. A watchdog-spawned `ava-updater` takes no lease
    at all, so a settle hold says nothing whatever about it — and this is the blind spot
    (issue #1074) that sank the rejected lease-only discriminator of #1098. The local
    session is still consulted and still decides."""
    fake_pause(sp.STRANDED_PAUSE_TIMEOUT_S + 60)
    monkeypatch.setattr(sp, "read_update_lease", lambda: _settle_hold(_THIS_HOST))
    monkeypatch.setattr("ops.cluster.current_orchestration", lambda: "update")
    unpaused: list[bool] = []
    monkeypatch.setattr(sp, "unpause_local_cluster", lambda: unpaused.append(True))
    assert sp.recover_stranded_pause() is False
    assert unpaused == []


def test_a_settle_hold_naming_this_host_still_serves_the_unowned_bound(
    fake_pause: Callable[[float | None], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An awaited hold changes WHO OWNS the pause, never HOW LONG an unowned one waits.
    `STRANDED_PAUSE_TIMEOUT_S` still covers the gap inside `spawn_update` between
    `pause_local_cluster()` and the session spawn a few statements later — a gap
    this host can enter at any moment, settle hold or not — so treating the hold as a
    licence to unpause immediately would reintroduce that race for one lease shape."""
    fake_pause(sp.STRANDED_PAUSE_TIMEOUT_S - 5)
    monkeypatch.setattr(sp, "read_update_lease", lambda: _settle_hold(_THIS_HOST))
    unpaused: list[bool] = []
    monkeypatch.setattr(sp, "unpause_local_cluster", lambda: unpaused.append(True))
    assert sp.recover_stranded_pause() is False
    assert unpaused == []


def test_a_note_that_does_not_parse_owns_the_pause(
    fake_pause: Callable[[float | None], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`settle_hosts`' rule, inherited: a note we cannot read yields an empty host set
    and therefore a deferral. A reworded note must never widen into permission."""
    fake_pause(sp.STRANDED_PAUSE_TIMEOUT_S + 60)
    reworded = DeployLease(
        holder="cloud:pid123", held_for_s=300.0, expires_in_s=600.0, note="settling for a bit"
    )
    monkeypatch.setattr(sp, "read_update_lease", lambda: reworded)
    unpaused: list[bool] = []
    monkeypatch.setattr(sp, "unpause_local_cluster", lambda: unpaused.append(True))
    assert sp.recover_stranded_pause() is False
    assert unpaused == []


def test_an_unresolvable_machine_name_never_unpauses(
    fake_pause: Callable[[float | None], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whether a hold names this host is unanswerable without knowing which host this
    is, so a machine-name failure is missing evidence like any other read failure — it
    must not fall through to "no owner"."""
    fake_pause(sp.STRANDED_PAUSE_TIMEOUT_S + 60)
    monkeypatch.setattr(sp, "read_update_lease", lambda: _settle_hold(_THIS_HOST))

    def _no_name() -> str:
        raise RuntimeError("machine name file missing")

    monkeypatch.setattr(sp, "machine_name", _no_name)
    unpaused: list[bool] = []
    monkeypatch.setattr(sp, "unpause_local_cluster", lambda: unpaused.append(True))
    assert sp.recover_stranded_pause() is False
    assert unpaused == []


def test_unreadable_signals_never_unpause(
    fake_pause: Callable[[float | None], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unpause taken on missing evidence is the one mistake this must not make —
    it would fight a rollout it simply could not see."""
    fake_pause(sp.STRANDED_PAUSE_TIMEOUT_S + 60)
    unpaused: list[bool] = []
    monkeypatch.setattr(sp, "unpause_local_cluster", lambda: unpaused.append(True))

    def _boom():
        raise RuntimeError("DB unreachable")

    monkeypatch.setattr(sp, "read_update_lease", _boom)
    assert sp.recover_stranded_pause() is False

    monkeypatch.setattr(sp, "read_update_lease", lambda: None)
    monkeypatch.setattr("ops.cluster.current_orchestration", _boom)
    assert sp.recover_stranded_pause() is False
    assert unpaused == []


def test_skips_when_recent(
    fake_pause: Callable[[float | None], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh pause (normal in-progress rollout) is left alone."""
    fake_pause(0)
    unpaused: list[bool] = []
    monkeypatch.setattr(sp, "unpause_local_cluster", lambda: unpaused.append(True))
    assert sp.recover_stranded_pause() is False
    assert unpaused == []


def test_skips_when_no_flag(
    fake_pause: Callable[[float | None], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No posture row → nothing to recover."""
    fake_pause(None)
    unpaused: list[bool] = []
    monkeypatch.setattr(sp, "unpause_local_cluster", lambda: unpaused.append(True))
    assert sp.recover_stranded_pause() is False
    assert unpaused == []


# ─── PauseController gate ────────────────────────────────────────────────────


def test_controller_blocks_and_recovers_when_stranded(
    fake_pause: Callable[[float | None], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stranded pause self-unpauses AND blocks this tick (acted=True)."""
    fake_pause(sp.STRANDED_PAUSE_TIMEOUT_S + 60)
    monkeypatch.setattr(sp, "read_update_lease", lambda: None)
    unpaused: list[bool] = []
    monkeypatch.setattr(sp, "unpause_local_cluster", lambda: unpaused.append(True))
    res = sp.PauseController().reconcile("gateway")
    assert res.dimension == "pause" and res.blocks is BlockScope.ALL and res.acted is True
    assert unpaused == [True]


def test_controller_blocks_without_acting_when_fresh_pause(
    fake_pause: Callable[[float | None], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normal (fresh) pause blocks the whole roster but takes no action: a rollout
    deliberately took every service here down, so none of them may be revived."""
    fake_pause(0)
    monkeypatch.setattr(sp, "unpause_local_cluster", lambda: None)
    res = sp.PauseController().reconcile("gateway")
    assert res.blocks is BlockScope.ALL and res.acted is False


def test_controller_proceeds_when_unpaused(fake_pause: Callable[[float | None], None]) -> None:
    """No posture row → the controller does not block; the tick runs healthchecks."""
    fake_pause(None)
    res = sp.PauseController().reconcile("gateway")
    assert res.blocks is BlockScope.NONE and res.acted is False
