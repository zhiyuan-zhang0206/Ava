"""shared.host_deploy_state — posture/updater-lease row (R1, Task #1021).

Covers the R1 host-level explicit model: the posture transitions the pause
lifecycle drives (idle -> paused -> idle), the updater lease liveness judgment,
Host transitions must never mutate the separate cluster UI-maintenance marker.
"""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from shared import host_deploy_state as hds


@pytest.fixture(autouse=True)
def _clean_row(db_conn: psycopg.Connection) -> Iterator[None]:
    """host_deploy_state is infra (not in the conftest TRUNCATE list) — this
    module self-manages its row the way test_cluster_lock.py manages the
    singleton lease row."""
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM host_deploy_state WHERE machine = %s", (_machine(),))
    db_conn.commit()
    yield
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM host_deploy_state WHERE machine = %s", (_machine(),))
    db_conn.commit()


def _machine() -> str:
    from shared.machine import machine_name

    return machine_name()


def test_no_row_reads_as_none() -> None:
    assert hds.read() is None


def test_host_transitions_never_mutate_an_existing_ui_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Host posture is control-plane state, not maintenance-page ownership.

    A local pause/start inside a rollout must not create or clear the cluster UI
    generation, which spans the full Phase-B tail.
    """
    marker = tmp_path / "deploy-state.json"
    original = b'{"schema_version":2,"generation":"owner"}'
    marker.write_bytes(original)

    # Use an explicit sentinel file: an absence assertion against an unrelated
    # tmp_path would be vacuous and could not catch host-state code clearing the
    # real cluster marker.
    from shared import ui_update_state

    monkeypatch.setattr(ui_update_state, "state_path", lambda: marker)
    hds.set_posture("paused")
    hds.touch_updater_lease(ttl_s=600)
    hds.clear_updater_lease()
    hds.set_posture("idle")

    state = hds.read()
    assert state is not None
    assert state.posture == "idle"
    assert state.updater_lease_expires_at is None
    assert marker.read_bytes() == original


def test_invalid_posture_is_rejected() -> None:
    with pytest.raises(ValueError):
        hds.set_posture("bogus")


def test_touch_updater_lease_enters_converging_and_live() -> None:
    hds.touch_updater_lease(ttl_s=600)
    state = hds.read()
    assert state is not None
    assert state.posture == "converging"
    assert state.updater_live is True
    assert hds.updater_lease_live() is True


def test_clear_updater_lease_drops_liveness_keeps_posture() -> None:
    hds.touch_updater_lease(ttl_s=600)
    hds.clear_updater_lease()
    state = hds.read()
    assert state is not None
    assert state.updater_live is False
    assert hds.updater_lease_live() is False
    assert state.posture == "converging"  # unpause owns the return to idle


def test_expired_lease_reads_as_not_live() -> None:
    hds.touch_updater_lease(ttl_s=-10)
    assert hds.updater_lease_live() is False


def test_set_posture_paused_stamps_paused_at() -> None:
    """The pause window's anchor (R1 PR5): entering `paused` stamps the moment,
    exactly where the retired `cluster_paused` file's mtime used to be."""
    hds.set_posture("paused")
    state = hds.read()
    assert state is not None
    assert state.paused_at is not None


def test_touch_updater_lease_preserves_paused_at() -> None:
    """Transitions INSIDE the window must not move the anchor: `updated_at` is
    bumped by `converging`, `paused_at` is the pause moment and stays."""
    hds.set_posture("paused")
    first = hds.read()
    assert first is not None and first.paused_at is not None

    hds.touch_updater_lease(ttl_s=600)
    state = hds.read()
    assert state is not None
    assert state.posture == "converging"
    assert state.paused_at == first.paused_at


def test_set_posture_idle_clears_paused_at() -> None:
    """Returning to serving clears the anchor: a host that is not paused has no
    pause window for the updater-outcome reader to anchor on."""
    hds.set_posture("paused")
    hds.set_posture("idle")
    state = hds.read()
    assert state is not None
    assert state.posture == "idle"
    assert state.paused_at is None


def test_repause_refreshes_paused_at() -> None:
    """A second pause is a NEW window: the anchor must move to the new pause, not
    keep the old one (a fresh update's logs must not be dated against the
    previous pause)."""
    hds.set_posture("paused")
    first = hds.read()
    assert first is not None and first.paused_at is not None
    hds.set_posture("idle")
    hds.set_posture("paused")
    second = hds.read()
    assert second is not None and second.paused_at is not None
    assert second.paused_at > first.paused_at


def test_set_posture_preserves_updater_lease() -> None:
    """Posture and the updater lease are orthogonal (audit 2026-08-08 P2): a
    pause/unpause landing mid-rollout must not clear the updater's liveness
    claim — the stalled-updater controller would otherwise reap a live
    update. touch_updater_lease owns the lease column exclusively."""
    hds.set_posture("idle")
    hds.touch_updater_lease(ttl_s=600)
    state = hds.read()
    assert state is not None and state.updater_live

    hds.set_posture("paused")  # mid-rollout pause must not clear the lease
    state = hds.read()
    assert state is not None
    assert state.posture == "paused"
    assert state.updater_live, "set_posture must not clear the updater lease"

    hds.set_posture("idle")
    state = hds.read()
    assert state is not None
    assert state.updater_live, "unpause must not clear the updater lease either"

    hds.clear_updater_lease()  # only the updater's own exit clears it
    state = hds.read()
    assert state is not None and not state.updater_live


# ─── updater mutual-exclusion lock (task #1181) ──────────────────────────────


def test_updater_lock_is_exclusive_and_releasable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two concurrent updaters must not both hold the host lock — a second
    acquire fails while the first is held (flock/msvcrt contend per fd, so a
    same-process second acquire is a faithful stand-in for a second process),
    and release makes the lock acquirable again without replacing its inode."""
    monkeypatch.setattr(hds, "_updater_lock_path", lambda: tmp_path / "updater.lock")
    assert hds.try_acquire_updater_lock() is True
    assert hds.try_acquire_updater_lock() is False  # second updater: declines
    hds.release_updater_lock()
    inode = (tmp_path / "updater.lock").stat().st_ino
    assert hds.try_acquire_updater_lock() is True
    hds.release_updater_lock()
    assert (tmp_path / "updater.lock").stat().st_ino == inode


@pytest.mark.skipif(os.name == "nt", reason="fork barrier exercises POSIX flock inode identity")
def test_updater_lock_contends_across_processes_on_one_stable_inode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A second process cannot acquire until the first releases the same inode."""
    path = tmp_path / "updater.lock"
    held = multiprocessing.get_context("fork").Event()
    release = multiprocessing.get_context("fork").Event()

    def _holder() -> None:
        hds._updater_lock_path = lambda: path  # type: ignore[method-assign]
        assert hds.try_acquire_updater_lock()
        held.set()
        assert release.wait(5)
        hds.release_updater_lock()

    monkeypatch.setattr(hds, "_updater_lock_path", lambda: path)
    proc = multiprocessing.get_context("fork").Process(target=_holder)
    proc.start()
    assert held.wait(5)
    inode = path.stat().st_ino
    assert hds.try_acquire_updater_lock() is False
    release.set()
    proc.join(5)
    assert proc.exitcode == 0
    assert hds.try_acquire_updater_lock() is True
    hds.release_updater_lock()
    assert path.stat().st_ino == inode


@pytest.mark.skipif(os.name == "nt", reason="Windows runs the post-checkout leg in a child")
def test_updater_lock_survives_the_posix_post_checkout_exec(tmp_path: Path) -> None:
    """The exec image must retain the pre-checkout flock until it exits.

    ``os.open`` creates non-inheritable descriptors by default. A replacement
    image that loses this fd has the same PID but no mutex, allowing a second
    updater to race its post-checkout stop/start leg.
    """
    lock_path = tmp_path / "updater.lock"
    probe = f"""
from pathlib import Path

from shared import host_deploy_state as hds

hds._updater_lock_path = lambda: Path({str(lock_path)!r})
print(hds.try_acquire_updater_lock())
"""
    continuation = f"""
import subprocess
import sys

result = subprocess.run(
    [sys.executable, "-c", {probe!r}],
    capture_output=True,
    text=True,
    check=False,
)
assert result.returncode == 0, result.stderr
assert result.stdout == "False\\n", result.stdout
"""
    pre_exec = f"""
import os
import sys
from pathlib import Path

from shared import host_deploy_state as hds

hds._updater_lock_path = lambda: Path({str(lock_path)!r})
assert hds.try_acquire_updater_lock()
os.execv(sys.executable, [sys.executable, "-c", {continuation!r}])
"""
    proc = subprocess.run(  # noqa: S603 — fixed argv and test-controlled lock path
        [sys.executable, "-c", pre_exec],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr


def test_updater_lock_uncontended_on_fresh_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A host with no updater running acquires immediately; the lock file lands
    under the run dir (the path the updater actually uses)."""
    monkeypatch.setattr(hds, "_updater_lock_path", lambda: tmp_path / "updater.lock")
    try:
        assert hds.try_acquire_updater_lock() is True
        assert (tmp_path / "updater.lock").exists()
    finally:
        hds.release_updater_lock()


# ── updater_expired: which update the lease in the row belongs to ─────────────


def _row(
    *,
    lease_offset_s: float | None,
    paused_offset_s: float | None,
    posture: str = "paused",
) -> hds.HostDeployState:
    """A row built by hand, with both timestamps placed relative to one `now`.

    Offsets are seconds from that `now`, so a lease "armed at" A expires at
    `A + UPDATER_LEASE_TTL_S` — the same arithmetic the DB writes.
    """
    now = datetime.now(UTC)
    return hds.HostDeployState(
        machine="t",
        posture=posture,
        updated_at=now,
        updater_lease_expires_at=(
            None if lease_offset_s is None else now + timedelta(seconds=lease_offset_s)
        ),
        paused_at=(None if paused_offset_s is None else now + timedelta(seconds=paused_offset_s)),
        db_now=now,
    )


def _armed_offset(lease_offset_s: float) -> float:
    """When a lease expiring at `lease_offset_s` was armed — the value
    `updater_expired` reconstructs, so the tests place `paused_at` against it."""
    return lease_offset_s - hds.UPDATER_LEASE_TTL_S


def test_a_lease_armed_after_the_pause_and_run_out_is_this_updates_stall() -> None:
    """The provable stop: this window armed it, and it expired."""
    state = _row(lease_offset_s=-60, paused_offset_s=_armed_offset(-60) - 1)
    assert state.updater_live is False
    assert state.updater_expired is True


def test_a_lease_armed_before_the_pause_is_a_previous_updates_residue() -> None:
    """The false positive this exists to remove: a run that ended without clearing
    leaves its expiry behind, and the next update's pause opens in front of it."""
    state = _row(lease_offset_s=-60, paused_offset_s=_armed_offset(-60) + 1)
    assert state.updater_live is False
    assert state.updater_expired is False


def test_the_boundary_counts_the_lease_as_this_windows() -> None:
    """`armed == paused_at` exactly. The pause is written first and the updater's
    touch follows it, so a lease stamped at the same instant is this window's — the
    comparison is `>=` for that reason, and the equal case is the one a same-clock
    Postgres can actually produce."""
    state = _row(lease_offset_s=-60, paused_offset_s=_armed_offset(-60))
    assert state.updater_expired is True


def test_a_live_lease_is_never_expired_whatever_the_pause_says() -> None:
    """Liveness outranks the dating: a lease with time left is a working updater,
    and no arithmetic about which window armed it changes that."""
    for paused_offset in (_armed_offset(60) - 1, _armed_offset(60) + 1):
        state = _row(lease_offset_s=60, paused_offset_s=paused_offset)
        assert state.updater_live is True
        assert state.updater_expired is False


def test_an_undatable_row_is_not_evidence() -> None:
    """`paused_at` NULL with an expiry present — the host is not in a pause window,
    so nothing says which update that expiry belongs to. Both callers must read
    "cannot tell" as "do not act": one would kill a live updater, the other would
    strand a working host."""
    state = _row(lease_offset_s=-60, paused_offset_s=None, posture="converging")
    assert state.updater_expired is False


def test_no_lease_at_all_is_not_expired() -> None:
    assert _row(lease_offset_s=None, paused_offset_s=-10).updater_expired is False


def test_the_lease_expiry_is_stamped_by_the_database_not_the_writer() -> None:
    """P1: the row is written by the runner and judged on the gateway, so an expiry
    computed from the writer's clock is a subtraction across two of them. Both the
    expiry and the `paused_at` it is dated against come from the same `now()`, which
    is what makes a host whose clock is minutes behind still judge correctly.

    Asserted against the DB's own clock rather than the test process's: they are the
    same machine here, so only the SQL can be checked, not the drift."""
    hds.set_posture("paused")
    hds.touch_updater_lease(ttl_s=600)
    state = hds.read()
    assert state is not None
    assert state.updater_lease_expires_at is not None
    assert state.paused_at is not None
    armed = state.updater_lease_expires_at - timedelta(seconds=600)
    # The touch follows the pause, both stamped by the same clock: sub-second apart.
    assert timedelta(0) <= armed - state.paused_at < timedelta(seconds=5)
    assert state.updater_live is True
    assert state.updater_expired is False
