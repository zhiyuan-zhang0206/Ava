"""`StrandedLeaseController` — the automatic reclaim of a dead-holder deploy lease.

2026-09-12: a killed orchestration left its deploy lease stranded while the host it
had paused sat blocked; every new deploy was refused until a human ran
`ava cluster recover` 9.5 minutes later. These tests pin the reclaim's bounds —
only a plain executing lease, only positive local-death evidence, only with no
live local session/updater, and only through the compare-and-set that protects a
racing owner.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Generator
from typing import NamedTuple

import pytest

from ops.controllers import stranded_lease as sl
from shared.cluster_lock import DeployLease, RecoveryClaim

_THIS_HOST = "m1"


def _lease(
    holder: str = "m1:pid123", *, note: str | None = None, held_for_s: float = 100.0
) -> DeployLease:
    return DeployLease(holder=holder, held_for_s=held_for_s, expires_in_s=600.0, note=note)


def _lease_and_reader(
    *, holder: str = "m1:pid123", note: str | None = None
) -> tuple[DeployLease, Callable[[], DeployLease]]:
    """A lease plus a reader returning that SAME instance (tests assert identity)."""
    lease = _lease(holder, note=note)

    def _read() -> DeployLease:
        return lease

    return lease, _read


def _pid_probe(alive: bool) -> Callable[[int], bool]:
    def _probe(_pid: int) -> bool:
        return alive

    return _probe


class _Writes(NamedTuple):
    """The claim/release writes one reclaim performed."""

    claims: list[tuple[str, DeployLease | None]]
    releases: list[str]


@pytest.fixture(autouse=True)
def _wired(monkeypatch: pytest.MonkeyPatch) -> _Writes:
    """Stub the collaborators; record the claim/release writes."""
    monkeypatch.setattr(sl, "machine_name", lambda: _THIS_HOST)
    monkeypatch.setattr("shared.machine.machine_name", lambda: _THIS_HOST)
    monkeypatch.setattr(sl.cluster_session, "live_orchestration_session", lambda: None)
    monkeypatch.setattr(sl, "updater_lease_live", lambda: False)

    @contextlib.contextmanager
    def _lock() -> Generator[None, None, None]:
        yield

    monkeypatch.setattr(sl.ui_update_state, "lifecycle_lock", _lock)
    claims: list[tuple[str, DeployLease | None]] = []
    releases: list[str] = []

    def _claim(holder: str, observed: DeployLease | None) -> RecoveryClaim:
        claims.append((holder, observed))
        return RecoveryClaim(
            acquired=True, previous_holder=observed.holder if observed is not None else None
        )

    def _release(holder: str) -> None:
        releases.append(holder)

    monkeypatch.setattr(sl, "claim_recovery_lock", _claim)
    monkeypatch.setattr(sl, "release_update_lock", _release)
    return _Writes(claims=claims, releases=releases)


def test_reclaims_a_plain_lease_whose_holder_is_provably_gone(
    monkeypatch: pytest.MonkeyPatch, _wired: _Writes
) -> None:
    """The incident shape: an executing lease whose local holder pid is absent is
    cleared in one round — attributed, CAS-guarded, and released."""
    lease, read = _lease_and_reader()
    monkeypatch.setattr(sl, "read_update_lease", read)
    monkeypatch.setattr("shared.proc.process_alive", _pid_probe(False))

    assert sl.reclaim_dead_deploy_lease() == "m1:pid123"

    ((claimed_holder, observed),) = _wired.claims
    assert claimed_holder.startswith(f"recovery:{_THIS_HOST}:pid")
    assert observed is lease
    assert _wired.releases == [claimed_holder]


def test_settle_hold_is_never_reclaimed(monkeypatch: pytest.MonkeyPatch, _wired: _Writes) -> None:
    """A settle hold's whole purpose is to outlive its writer — even a provably
    dead holder does not license touching it (convergence or its TTL releases)."""
    _, read = _lease_and_reader(note="settling, waiting for: win")
    monkeypatch.setattr(sl, "read_update_lease", read)
    monkeypatch.setattr("shared.proc.process_alive", _pid_probe(False))

    assert sl.reclaim_dead_deploy_lease() is None
    assert _wired.claims == []


def test_foreign_or_unparseable_holder_reads_live(
    monkeypatch: pytest.MonkeyPatch, _wired: _Writes
) -> None:
    """A holder on another machine (never probed from here) and an operator-chosen
    hold name (not a pid) both defer — the conservative direction the manual op
    shares."""
    monkeypatch.setattr("shared.proc.process_alive", _pid_probe(False))
    for holder in ("cloud:pid123", "operator-chosen-campaign"):
        _, read = _lease_and_reader(holder=holder)
        monkeypatch.setattr(sl, "read_update_lease", read)
        assert sl.reclaim_dead_deploy_lease() is None
    assert _wired.claims == []


def test_live_local_orchestration_session_defers(
    monkeypatch: pytest.MonkeyPatch, _wired: _Writes
) -> None:
    """A fresh local owner may be racing for the row; the reclaim stands down
    (mirroring the manual op's live-session refusal)."""
    monkeypatch.setattr(sl, "read_update_lease", _lease)
    monkeypatch.setattr("shared.proc.process_alive", _pid_probe(False))
    monkeypatch.setattr(sl.cluster_session, "live_orchestration_session", lambda: "ava-rollout")

    assert sl.reclaim_dead_deploy_lease() is None
    assert _wired.claims == []


def test_live_local_updater_lease_defers(monkeypatch: pytest.MonkeyPatch, _wired: _Writes) -> None:
    monkeypatch.setattr(sl, "read_update_lease", _lease)
    monkeypatch.setattr("shared.proc.process_alive", _pid_probe(False))
    monkeypatch.setattr(sl, "updater_lease_live", lambda: True)

    assert sl.reclaim_dead_deploy_lease() is None
    assert _wired.claims == []


def test_lost_cas_preserves_the_row(monkeypatch: pytest.MonkeyPatch, _wired: _Writes) -> None:
    """The lease changed while its holder's death was being proven — a new owner
    may have started; the reclaim refuses and releases nothing."""
    monkeypatch.setattr(sl, "read_update_lease", _lease)
    monkeypatch.setattr("shared.proc.process_alive", _pid_probe(False))

    def _lost_cas(_holder: str, _observed: DeployLease | None) -> RecoveryClaim:
        return RecoveryClaim(acquired=False)

    monkeypatch.setattr(sl, "claim_recovery_lock", _lost_cas)

    assert sl.reclaim_dead_deploy_lease() is None
    assert _wired.releases == []


def test_unreadable_lease_degrades_without_raising(
    monkeypatch: pytest.MonkeyPatch, _wired: _Writes
) -> None:
    def _down() -> DeployLease:
        raise ConnectionError("pgbouncer blip")

    monkeypatch.setattr(sl, "read_update_lease", _down)

    assert sl.reclaim_dead_deploy_lease() is None


def test_lifecycle_lock_timeout_defers(monkeypatch: pytest.MonkeyPatch, _wired: _Writes) -> None:
    from shared.platform import LockTimeoutError

    monkeypatch.setattr(sl, "read_update_lease", _lease)
    monkeypatch.setattr("shared.proc.process_alive", _pid_probe(False))

    @contextlib.contextmanager
    def _timeout() -> Generator[None, None, None]:
        raise LockTimeoutError("held by another process")
        yield  # pragma: no cover

    monkeypatch.setattr(sl.ui_update_state, "lifecycle_lock", _timeout)

    assert sl.reclaim_dead_deploy_lease() is None
    assert _wired.claims == []


def test_controller_reports_acted_and_never_blocks(
    monkeypatch: pytest.MonkeyPatch, _wired: _Writes
) -> None:
    monkeypatch.setattr(sl, "read_update_lease", _lease)
    monkeypatch.setattr("shared.proc.process_alive", _pid_probe(False))

    result = sl.StrandedLeaseController().reconcile("gateway")

    assert result.dimension == "lease"
    assert result.acted is True
    assert result.blocks.value == "none"
    assert "m1:pid123" in (result.detail or "")


def test_controller_reports_nothing_when_the_lease_is_live(
    monkeypatch: pytest.MonkeyPatch, _wired: _Writes
) -> None:
    _, read = _lease_and_reader(holder="m1:pid1")
    monkeypatch.setattr(sl, "read_update_lease", read)
    monkeypatch.setattr("shared.proc.process_alive", _pid_probe(True))

    result = sl.StrandedLeaseController().reconcile("agent-runner")

    assert result.acted is False
    assert result.detail is None
    assert _wired.claims == []
