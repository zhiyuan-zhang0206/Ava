"""`cluster_recover_op` — the holder pid-probe gates the lease refusal.

2026-08-12: a rollout hard-killed by its own stop leg left a live-looking deploy
lease behind, and recovery refused on the lease's kind without ever probing the
dead holder — staying refused for the rest of the lease TTL, the exact wait the
op's docstring promises the pid-probe avoids. These tests pin the refusal to
*process liveness*, never lease liveness alone.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Literal

import pytest

import ops.ops_cluster as _ops
from ops.cluster import ClusterUpdateInProgress
from shared.cluster_lock import DeployLease, RecoveryClaim

_Kind = Literal["rollout", "restart", "update"]


def _lease(
    holder: str,
    *,
    kind: _Kind | None = "rollout",
    note: str | None = None,
    held_for_s: float = 0.0,
) -> DeployLease:
    # held_for_s=0.0 ("acquired just now") keeps the pid-recycling bound inert for
    # tests that pin the liveness verdict alone: every live process predates
    # now + slack. The recycling tests pass a large age explicitly.
    return DeployLease(
        holder=holder, held_for_s=held_for_s, expires_in_s=600.0, note=note, kind=kind
    )


@pytest.fixture
def recover_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """Stub the collaborators; record whether the clear + unpause writes ran."""
    calls = {"released": False, "unpaused": False}
    monkeypatch.setattr(_ops, "machine_name", lambda: "m1")
    monkeypatch.setattr(_ops, "updater_lease_live", lambda: False)
    monkeypatch.setattr(_ops.cluster_session, "live_orchestration_session", lambda: None)

    def _claim(_holder: str, observed: DeployLease | None) -> RecoveryClaim:
        return RecoveryClaim(
            acquired=True,
            previous_holder=observed.holder if observed is not None else None,
        )

    def _release(_holder: str) -> None:
        calls["released"] = True

    monkeypatch.setattr(_ops, "claim_recovery_lock", _claim)
    monkeypatch.setattr(_ops, "release_update_lock", _release)
    monkeypatch.setattr(_ops, "unpause_local_cluster", lambda: calls.update(unpaused=True))
    monkeypatch.setattr(
        _ops.ui_update_state,
        "read",
        lambda: _ops.ui_update_state.UiUpdateSnapshot(status="inactive"),
    )
    return calls


def test_recover_clears_the_stranded_ui_marker_after_liveness_refusal_passes(
    monkeypatch: pytest.MonkeyPatch, recover_calls: dict[str, bool]
) -> None:
    _set_lease(monkeypatch, None)
    cleared: list[str] = []
    monkeypatch.setattr(
        _ops.ui_update_state,
        "read",
        lambda: _ops.ui_update_state.UiUpdateSnapshot(
            status="updating", generation="stranded", kind="rollout"
        ),
    )
    monkeypatch.setattr(_ops.ui_update_state, "clear", cleared.append)

    _ops.cluster_recover_op()

    assert cleared == ["stranded"]


@pytest.mark.parametrize("session", ["ava-rollout", "ava-cluster-restart"])
def test_recover_refuses_a_spawned_session_before_its_db_lease_exists(
    session: str,
    monkeypatch: pytest.MonkeyPatch,
    recover_calls: dict[str, bool],
) -> None:
    """begin -> detached spawn -> child lock acquisition is a real live-owner
    window even though neither DB lease exists yet."""
    _set_lease(monkeypatch, None)
    monkeypatch.setattr(_ops.cluster_session, "live_orchestration_session", lambda: session)
    marker_clears: list[bool] = []
    monkeypatch.setattr(_ops.ui_update_state, "force_clear", lambda: marker_clears.append(True))

    with pytest.raises(ClusterUpdateInProgress, match=session):
        _ops.cluster_recover_op()

    assert recover_calls == {"released": False, "unpaused": False}
    assert marker_clears == []


def _set_lease(monkeypatch: pytest.MonkeyPatch, lease: DeployLease | None) -> None:
    monkeypatch.setattr(_ops, "read_update_lease", lambda: lease)


def _set_alive(monkeypatch: pytest.MonkeyPatch, alive: Callable[[int], bool]) -> None:
    monkeypatch.setattr(_ops, "process_alive", alive)


def test_dead_local_holders_unexpired_lease_is_cleared_at_once(
    monkeypatch: pytest.MonkeyPatch, recover_calls: dict[str, bool]
) -> None:
    """THE 2026-08-12 regression: an unexpired lease with kind='rollout' whose
    local holder pid is dead must clear immediately, not refuse until TTL."""
    _set_lease(monkeypatch, _lease("m1:pid123", kind="rollout"))
    _set_alive(monkeypatch, lambda _pid: False)

    result = _ops.cluster_recover_op()

    assert result == {"unlocked_holder": "m1:pid123"}
    assert recover_calls == {"released": True, "unpaused": True}


def test_live_local_holder_refuses_and_clears_nothing(
    monkeypatch: pytest.MonkeyPatch, recover_calls: dict[str, bool]
) -> None:
    # This process's own pid: alive, and its create_time is real for the
    # recycling bound (held_for_s=0.0 keeps the bound inert here).
    _set_lease(monkeypatch, _lease(f"m1:pid{os.getpid()}"))
    _set_alive(monkeypatch, lambda _pid: True)

    with pytest.raises(ClusterUpdateInProgress, match="live process"):
        _ops.cluster_recover_op()
    assert recover_calls == {"released": False, "unpaused": False}


def test_remote_holder_is_conservatively_treated_as_live(
    monkeypatch: pytest.MonkeyPatch, recover_calls: dict[str, bool]
) -> None:
    """A holder on another machine cannot be pid-probed from here — refuse rather
    than risk clobbering a real rollout (its TTL, or recover run there, clears it)."""
    _set_lease(monkeypatch, _lease("elsewhere:pid1"))
    _set_alive(monkeypatch, lambda _pid: False)  # irrelevant: never probed cross-machine

    with pytest.raises(ClusterUpdateInProgress):
        _ops.cluster_recover_op()
    assert recover_calls == {"released": False, "unpaused": False}


def test_live_local_updater_lease_refuses_without_a_deploy_lease(
    monkeypatch: pytest.MonkeyPatch, recover_calls: dict[str, bool]
) -> None:
    """The lease-less watchdog-spawned updater is invisible to the deploy lease;
    its own host lease must still refuse recovery."""
    _set_lease(monkeypatch, None)
    monkeypatch.setattr(_ops, "updater_lease_live", lambda: True)

    with pytest.raises(ClusterUpdateInProgress, match="updater lease"):
        _ops.cluster_recover_op()
    assert recover_calls == {"released": False, "unpaused": False}


def test_settle_hold_of_a_dead_holder_is_broken(
    monkeypatch: pytest.MonkeyPatch, recover_calls: dict[str, bool]
) -> None:
    """`ava cluster recover` remains the documented way to break a settle hold —
    the exited orchestration's pid is dead, so the hold clears."""
    _set_lease(monkeypatch, _lease("m1:pid123", note="settling, waiting for: win"))
    _set_alive(monkeypatch, lambda _pid: False)

    result = _ops.cluster_recover_op()

    assert result == {"unlocked_holder": "m1:pid123"}
    assert recover_calls == {"released": True, "unpaused": True}


def test_no_lease_still_unpauses_a_stranded_host(
    monkeypatch: pytest.MonkeyPatch, recover_calls: dict[str, bool]
) -> None:
    """A stranded pause can outlive its lease (TTL lapsed); recover still clears both."""
    _set_lease(monkeypatch, None)

    _ops.cluster_recover_op()

    assert recover_calls == {"released": True, "unpaused": True}


def test_recover_cas_loses_to_a_new_owner_without_unpausing_or_clearing(
    monkeypatch: pytest.MonkeyPatch, recover_calls: dict[str, bool]
) -> None:
    _set_lease(monkeypatch, None)
    marker_clears: list[str] = []

    def _lose_claim(
        _recovery_holder: str,
        _observed: DeployLease | None,
        *,
        ttl_s: float = 60.0,
    ) -> RecoveryClaim:
        del ttl_s
        return RecoveryClaim(acquired=False)

    monkeypatch.setattr(
        _ops,
        "claim_recovery_lock",
        _lose_claim,
    )
    monkeypatch.setattr(
        _ops.ui_update_state,
        "read",
        lambda: _ops.ui_update_state.UiUpdateSnapshot(
            status="updating", generation="winner", kind="rollout"
        ),
    )
    monkeypatch.setattr(_ops.ui_update_state, "clear", marker_clears.append)

    with pytest.raises(ClusterUpdateInProgress, match="lease changed"):
        _ops.cluster_recover_op()

    assert recover_calls == {"released": False, "unpaused": False}
    assert marker_clears == []


def test_recycled_pid_on_an_old_lease_reads_as_dead(
    monkeypatch: pytest.MonkeyPatch, recover_calls: dict[str, bool]
) -> None:
    """An alive pid is not enough: this very process is alive, but it started long
    AFTER a lease this old was acquired, so it cannot be the holder — the pid was
    recycled and recovery must clear, not refuse (real process_alive + psutil)."""
    _set_lease(monkeypatch, _lease(f"m1:pid{os.getpid()}", held_for_s=1_000_000.0))

    result = _ops.cluster_recover_op()

    assert result == {"unlocked_holder": f"m1:pid{os.getpid()}"}
    assert recover_calls == {"released": True, "unpaused": True}


def test_fresh_lease_held_by_a_live_process_still_refuses(
    monkeypatch: pytest.MonkeyPatch, recover_calls: dict[str, bool]
) -> None:
    """The recycling bound must not misread a genuine holder: a live process on a
    lease acquired after it started is the holder (real process_alive + psutil)."""
    _set_lease(monkeypatch, _lease(f"m1:pid{os.getpid()}", held_for_s=0.0))

    with pytest.raises(ClusterUpdateInProgress, match="live process"):
        _ops.cluster_recover_op()
    assert recover_calls == {"released": False, "unpaused": False}
