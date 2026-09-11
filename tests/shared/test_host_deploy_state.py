"""shared.host_deploy_state — posture/updater-lease row (R1, Task #1021).

Covers the R1 host-level explicit model: the posture transitions the pause
lifecycle drives (idle -> paused -> idle), the updater lease liveness judgment,
the stranded-hold record and its bounded recovery budget (tasks #3132/#3142),
and the table's migration shape. Host transitions must never mutate the
separate cluster UI-maintenance marker.
"""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import LiteralString, cast

import psycopg
import pytest
from psycopg import sql

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


def test_read_uses_the_callers_connection(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundled snapshot can read the row without opening or owning another connection."""
    hds.set_posture("paused")

    def _fresh_connect(**_kwargs: object) -> object:
        raise AssertionError("read opened a fresh connection")

    monkeypatch.setattr("shared.db.connect", _fresh_connect)

    state = hds.read(conn=db_conn)

    assert state is not None
    assert state.posture == "paused"


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


def test_stranded_hold_mark_is_set_once_and_clear_releases_it() -> None:
    """The failed-leg record (task #3132): declared once with a stable `since`,
    reason follows the latest reading, `updated_at` is never renewed by it, and
    clear releases both fields exactly."""
    hds.set_posture("paused")
    assert hds.mark_stranded_hold("updater exited rc=1") is True
    state = hds.read()
    assert state is not None
    first = state.stranded_hold_since
    assert first is not None
    assert state.stranded_hold_reason == "updater exited rc=1"
    updated_at_before = state.updated_at

    assert hds.mark_stranded_hold("updater exited rc=2") is False  # already declared
    state = hds.read()
    assert state is not None
    assert state.stranded_hold_since == first
    assert state.stranded_hold_reason == "updater exited rc=2"  # reason follows the latest read
    assert state.updated_at == updated_at_before  # the record never renews freshness

    assert hds.clear_stranded_hold() is True
    state = hds.read()
    assert state is not None
    assert state.stranded_hold_since is None
    assert state.stranded_hold_reason is None
    assert hds.clear_stranded_hold() is False  # idempotent


def test_stranded_hold_mark_without_a_row_is_a_no_op() -> None:
    """A hold can only follow the pause that wrote the posture row; the mark must
    not fabricate a deploy state no transition produced."""
    assert hds.read() is None
    assert hds.mark_stranded_hold("updater exited rc=1") is False
    assert hds.read() is None


# ── the bounded recovery budget (task #3142) ─────────────────────────────────


def _declare_stranded_hold() -> None:
    hds.set_posture("paused")
    assert hds.mark_stranded_hold("updater exited rc=1") is True


def test_stranded_recovery_reserves_one_attempt_per_episode() -> None:
    """The whole budget: the first reservation wins, every later one declines."""
    _declare_stranded_hold()
    assert hds.reserve_stranded_recovery(max_attempts=1, cooldown_s=900.0, note="a") == 1
    assert hds.reserve_stranded_recovery(max_attempts=1, cooldown_s=900.0, note="b") is None
    state = hds.read()
    assert state is not None
    assert state.stranded_hold_attempts == 1
    assert state.stranded_hold_recovery_note == "a"


def test_stranded_recovery_cooldown_gates_a_fresh_attempt() -> None:
    """With a larger budget the clock still rules: a fresh attempt waits 900s."""
    _declare_stranded_hold()
    assert hds.reserve_stranded_recovery(max_attempts=3, cooldown_s=900.0, note="a") == 1
    assert hds.reserve_stranded_recovery(max_attempts=3, cooldown_s=900.0, note="b") is None
    state = hds.read()
    assert state is not None
    assert state.stranded_hold_attempts == 1


def test_stranded_recovery_budget_resets_with_a_new_episode() -> None:
    _declare_stranded_hold()
    assert hds.reserve_stranded_recovery(max_attempts=1, cooldown_s=900.0, note="a") == 1
    assert hds.clear_stranded_hold() is True
    assert hds.mark_stranded_hold("updater died mid-flight") is True
    state = hds.read()
    assert state is not None
    assert state.stranded_hold_attempts == 0
    assert state.stranded_hold_recovery_note is None
    assert hds.reserve_stranded_recovery(max_attempts=1, cooldown_s=900.0, note="b") == 1


def test_stranded_recovery_never_reserves_without_the_record() -> None:
    """A released hold cannot be reserved against, even with an unspent budget."""
    hds.set_posture("paused")
    assert hds.reserve_stranded_recovery(max_attempts=1, cooldown_s=900.0, note="a") is None


def test_stranded_recovery_finish_records_without_refunding() -> None:
    _declare_stranded_hold()
    assert hds.reserve_stranded_recovery(max_attempts=1, cooldown_s=900.0, note="attempt") == 1
    hds.finish_stranded_recovery("failed at start: RuntimeError('start leg exited 5')")
    state = hds.read()
    assert state is not None
    assert state.stranded_hold_recovery_note == (
        "failed at start: RuntimeError('start leg exited 5')"
    )
    assert state.stranded_hold_attempts == 1  # never refunded
    hds.finish_stranded_recovery("late outcome")  # no record -> no-op covered below


def test_stranded_recovery_finish_without_a_record_is_a_no_op() -> None:
    hds.set_posture("paused")
    hds.finish_stranded_recovery("late outcome")
    state = hds.read()
    assert state is not None
    assert state.stranded_hold_recovery_note is None


def test_stranded_recovery_migration_round_trips_on_a_pre_migration_table(
    db_conn: psycopg.Connection,
) -> None:
    """The upgrade path a rollout takes on an older cluster: the three budget
    columns land, reverse, and re-apply (`db/schema.sql` carries the final shape
    and stamps the migration as already applied, so only an upgrade runs it)."""
    migration = (
        Path(__file__).resolve().parents[2] / "migrations/20260911T192500_stranded-hold-recovery"
    )
    with db_conn.transaction(force_rollback=True):
        db_conn.execute("CREATE SCHEMA recovery_migration")
        db_conn.execute("SET LOCAL search_path TO recovery_migration")
        db_conn.execute(
            "CREATE TABLE host_deploy_state ("
            "machine TEXT PRIMARY KEY, stranded_hold_since TIMESTAMPTZ, "
            "stranded_hold_reason TEXT)"
        )
        assert _budget_columns(db_conn) == []
        db_conn.execute(_migration_body(migration, ".sql"))
        assert _budget_columns(db_conn) == [
            "stranded_hold_attempts",
            "stranded_hold_attempted_at",
            "stranded_hold_recovery_note",
        ]
        db_conn.execute(_migration_body(migration, ".down.sql"))
        assert _budget_columns(db_conn) == []
        db_conn.execute(_migration_body(migration, ".sql"))
        assert _budget_columns(db_conn) == [
            "stranded_hold_attempts",
            "stranded_hold_attempted_at",
            "stranded_hold_recovery_note",
        ]


def _migration_body(migration: Path, suffix: str) -> sql.SQL:
    return sql.SQL(cast(LiteralString, migration.with_suffix(suffix).read_text()))


def _budget_columns(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT attname FROM pg_attribute WHERE attrelid = "
        "to_regclass('host_deploy_state') AND NOT attisdropped AND attnum > 0 "
        "ORDER BY attnum"
    ).fetchall()
    wanted = {
        "stranded_hold_attempts",
        "stranded_hold_attempted_at",
        "stranded_hold_recovery_note",
    }
    return [name for (name,) in rows if str(name) in wanted]
