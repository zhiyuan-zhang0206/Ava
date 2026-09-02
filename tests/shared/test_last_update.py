"""`shared.last_update` — the last rollout's outcome as a stated fact.

Real-DB tests (the record IS a Postgres singleton row). The load-bearing case is
the one the write-ahead design exists for: an orchestration that dies never files
its own report, and the reader has to produce that report from outside — from a row
that was opened before the work and a deploy lease whose holder stopped renewing.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from shared.cluster_lock import acquire_update_lock, release_update_lock
from shared.last_update import (
    UpdateOutcome,
    begin_update,
    finish_update,
    note_observed_recovery,
    read_last_update,
)


@pytest.fixture(autouse=True)
def _clear_record(db_conn: psycopg.Connection) -> Iterator[None]:
    """Reset the singleton record + the deploy lease around each test — both are
    infra rather than data, so they are not in the conftest TRUNCATE list and this
    module self-manages them (same pattern as tests/shared/test_cluster_pin.py)."""

    def _clear() -> None:
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE cluster_last_update SET target_sha=NULL, origin=NULL, holder=NULL, "
                "started_at=NULL, ended_at=NULL, outcome=NULL, failing_step=NULL, "
                "observed_by=NULL, log_path=NULL, pin_advanced=FALSE WHERE id=1"
            )
            cur.execute(
                "UPDATE deployment_state SET holder=NULL, acquired_at=NULL, "
                "expires_at=NULL, note=NULL, settle_hosts=NULL, settle_note=NULL, "
                "phase='stable', kind=NULL WHERE id=1"
            )
        db_conn.commit()

    _clear()
    yield
    _clear()


def test_no_update_ever_run_reads_as_no_record() -> None:
    """A seeded-but-never-written row is not an outcome. Rendering it as one would
    put a verdict on a cluster that has never been updated."""
    assert read_last_update() is None


def test_a_finished_rollout_reports_its_own_verdict() -> None:
    begin_update(target_sha="abc1234", origin="agent:7", holder="mini:pid1")
    finish_update(UpdateOutcome.CLEAN)

    record = read_last_update()

    assert record is not None
    assert record.outcome is UpdateOutcome.CLEAN
    assert record.failed is False
    assert record.target_sha == "abc1234"
    assert record.origin == "agent:7"
    assert record.ended_at is not None


def test_a_failed_rollout_carries_the_step_and_whether_the_pin_moved() -> None:
    """The two facts a pin/head mismatch cannot distinguish: which step failed, and
    whether the gateway got far enough to move the cluster pin. A failure with the
    pin advanced and one with it held leave the cluster in different states and want
    different next actions."""
    begin_update(target_sha="8bdd366", origin="frontend", holder="mini:pid1")
    finish_update(
        UpdateOutcome.INCOMPLETE,
        failing_step="the gateway was not serving, so Phase B never fanned out",
        pin_advanced=True,
    )

    record = read_last_update()

    assert record is not None
    assert record.failed is True
    assert record.pin_advanced is True
    assert "Phase B never fanned out" in (record.failing_step or "")
    assert "FAILED" in record.describe()


def test_a_successful_update_clears_the_previous_failure() -> None:
    """The record is a singleton that each rollout overwrites, so a green run erases
    a red one by replacing it. Nothing has to remember to reset a flag — which is
    how a stale failure banner outlives the failure it describes."""
    begin_update(target_sha="8bdd366", origin="frontend", holder="mini:pid1")
    finish_update(UpdateOutcome.ABORTED, failing_step="gateway local update (rc=2)")
    assert (read_last_update() or {}) and read_last_update().failed is True  # type: ignore[union-attr]

    begin_update(target_sha="7e571b4", origin="cli:mini", holder="mini:pid2")
    finish_update(UpdateOutcome.CLEAN)

    record = read_last_update()
    assert record is not None
    assert record.failed is False
    assert record.failing_step is None, "the previous run's step must not survive into this one"
    assert record.pin_advanced is False


# ─── the write-ahead half: an orchestration that dies files no report ─────────


def test_an_open_row_under_a_live_lease_reads_as_running() -> None:
    """A rollout runs for as long as it runs — the lease is renewed while it does —
    so elapsed time proves nothing about whether it is alive. The holder still
    holding is what does."""
    holder = "mini:pid1"
    assert acquire_update_lock(holder)
    try:
        begin_update(target_sha="abc1234", origin="cli:mini", holder=holder)

        record = read_last_update()

        assert record is not None
        assert record.outcome is UpdateOutcome.RUNNING
        assert record.failed is False, "an update in flight is not a failed one"
    finally:
        release_update_lock(holder)


def test_an_open_row_with_no_lease_is_an_orphaned_update() -> None:
    """The case the write-ahead design exists for. A killed orchestration never runs
    `finish_update`, so nothing it could have done would report the failure — the
    reader produces the verdict from outside, from a row opened while the process was
    still alive plus a lease its holder stopped renewing."""
    begin_update(target_sha="abc1234", origin="cli:mini", holder="mini:pid999")

    record = read_last_update()

    assert record is not None
    assert record.outcome is UpdateOutcome.ORPHANED
    assert record.failed is True
    assert "died without reporting an outcome" in record.describe()


def test_a_lease_held_by_someone_else_does_not_make_a_dead_rollout_look_alive() -> None:
    """A later, unrelated deploy taking the lease must not resurrect the previous
    orchestration's row. The comparison is against the holder that opened it."""
    begin_update(target_sha="abc1234", origin="cli:mini", holder="mini:pid999")
    other = "mini:pid1000"
    assert acquire_update_lock(other)
    try:
        record = read_last_update()
        assert record is not None
        assert record.outcome is UpdateOutcome.ORPHANED
    finally:
        release_update_lock(other)


def test_the_unfinished_readings_are_never_written() -> None:
    """`RUNNING` / `ORPHANED` are how an *open* row reads, not outcomes anyone
    records — writing one would make a row claim a verdict its writer cannot have."""
    for reading in (UpdateOutcome.RUNNING, UpdateOutcome.ORPHANED):
        with pytest.raises(ValueError, match="unfinished row"):
            finish_update(reading)


# ─── the observer half: what was DONE about the failure ──────────────────────
#
# A reader can derive THAT an update failed (an open row with no live lease). It
# cannot derive what has since been done about it — and on 2026-07-30 that missing
# half is what made a correct auto-recovery read as an inexplicable pin change.
# The processes that clean up after a dead orchestration provably witnessed the
# death, so they are the ones that can say.


def test_an_observer_can_record_what_it_did_about_a_failure() -> None:
    begin_update(target_sha="8bdd366", origin="frontend", holder="mini:pid1")
    finish_update(UpdateOutcome.ABORTED, failing_step="gateway local update (rc=2)")

    note_observed_recovery("rolled back 8bdd366 -> 7e571b4")

    record = read_last_update()
    assert record is not None
    assert record.observed_by == "rolled back 8bdd366 -> 7e571b4"
    assert "since then: rolled back" in record.describe()


def test_an_orphaned_update_a_rollback_cleaned_up_after_reads_as_recovered() -> None:
    """The 2026-07-30 case, end to end. Nothing closed the row, so the only two facts
    that exist are "the holder stopped renewing" and "a rollback says it moved the
    cluster to X" — and the reader has to put them together into the state the
    operator is actually in: an update that failed, on a cluster that is fine."""
    begin_update(target_sha="8bdd366", origin="frontend", holder="mini:pid999")

    note_observed_recovery("rolled back 8bdd366 -> 7e571b4")

    record = read_last_update()
    assert record is not None
    assert record.outcome is UpdateOutcome.RECOVERED
    assert record.failed is True, "a recovery still has to be reported — that is the whole bug"
    assert record.observed_by == "rolled back 8bdd366 -> 7e571b4"
    assert "RECOVERED" in record.describe()


def test_a_recovery_note_never_promotes_a_rollout_that_is_still_running() -> None:
    """The promotion is gated on the row already reading as a failure. A live rollout
    owns its own state, and calling it recovered would report an ending to something
    that has not ended."""
    holder = "mini:pid1"
    assert acquire_update_lock(holder)
    try:
        begin_update(target_sha="abc1234", origin="cli:mini", holder=holder)
        note_observed_recovery("rolled back abc1234 -> 7e571b4")

        record = read_last_update()

        assert record is not None
        assert record.outcome is UpdateOutcome.RUNNING
        assert record.failed is False
    finally:
        release_update_lock(holder)


def test_an_observer_never_annotates_an_update_that_succeeded() -> None:
    """A rollback can follow a perfectly successful update — a bad commit that
    deployed fine. Hanging the recovery note on that record would read as the update
    having failed, which is a claim the observer is in no position to make."""
    begin_update(target_sha="8bdd366", origin="frontend", holder="mini:pid1")
    finish_update(UpdateOutcome.CLEAN)

    note_observed_recovery("rolled back 8bdd366 -> 7e571b4")

    record = read_last_update()
    assert record is not None
    assert record.observed_by is None
    assert record.failed is False


def test_an_observer_never_overwrites_the_recorded_verdict(db_conn: psycopg.Connection) -> None:
    """It writes the fact it witnessed, not a verdict on a run it never made. The
    stored outcome is the orchestration's own and stays untouched — the promotion to
    `recovered` happens in the reader, so a writer race can never destroy a verdict
    somebody else filed."""
    begin_update(target_sha="8bdd366", origin="frontend", holder="mini:pid1")
    finish_update(UpdateOutcome.INCOMPLETE, pin_advanced=True)

    note_observed_recovery("rolled back 8bdd366 -> 7e571b4")

    with db_conn.cursor() as cur:
        cur.execute("SELECT outcome FROM cluster_last_update WHERE id=1")
        row = cur.fetchone()
    assert row is not None and row[0] == "incomplete"


def test_a_failure_an_observer_recovered_reads_as_recovered_and_keeps_what_failed() -> None:
    """Naming the newer fact first must not throw away the older one: which step
    failed and whether the pin moved are still the facts that decide what to do
    next, so they survive the promotion."""
    begin_update(target_sha="8bdd366", origin="frontend", holder="mini:pid1")
    finish_update(
        UpdateOutcome.INCOMPLETE,
        failing_step="the Phase-B poll: acked agent-runners never reported back",
        pin_advanced=True,
    )

    note_observed_recovery("rolled back 8bdd366 -> 7e571b4")

    record = read_last_update()
    assert record is not None
    assert record.outcome is UpdateOutcome.RECOVERED
    assert record.pin_advanced is True
    assert "Phase-B poll" in (record.failing_step or "")


def test_lkg_promotion_officially_finalizes_its_incomplete_rollout(
    db_conn: psycopg.Connection,
) -> None:
    """A Phase-B timeout remains historical, not an open failure, once the health
    window has proved its target became last-known-good after self-healing."""
    from shared.cluster_pin import (
        promote_pending_known_good_if_ready,
        set_target_with_pending_known_good,
    )

    target = "8bdd366"
    begin_update(target_sha=target, origin="cli:mini", holder="mini:pid1")
    finish_update(
        UpdateOutcome.INCOMPLETE,
        failing_step="the Phase-B poll: an acked runner did not report back",
        pin_advanced=True,
    )
    set_target_with_pending_known_good(target)

    assert promote_pending_known_good_if_ready(min_age_s=0.0) is True

    record = read_last_update()
    assert record is not None
    assert record.outcome is UpdateOutcome.RECOVERED
    assert record.failed is True
    assert record.failing_step == "the Phase-B poll: an acked runner did not report back"
    assert record.observed_by == "cluster self-healed; last-known-good advanced to 8bdd366"
    with db_conn.cursor() as cur:
        cur.execute("SELECT outcome FROM cluster_last_update WHERE id = 1")
        row = cur.fetchone()
    assert row is not None and row[0] == "recovered"


def test_lkg_promotion_never_finalizes_an_unrelated_incomplete_rollout() -> None:
    """The promotion may prove only the target it promotes; a newer pending target
    cannot rewrite an older rollout record."""
    from shared.cluster_pin import (
        promote_pending_known_good_if_ready,
        set_target_with_pending_known_good,
    )

    begin_update(target_sha="failed-target", origin="cli:mini", holder="mini:pid1")
    finish_update(UpdateOutcome.INCOMPLETE, pin_advanced=True)
    set_target_with_pending_known_good("different-target")

    assert promote_pending_known_good_if_ready(min_age_s=0.0) is True

    record = read_last_update()
    assert record is not None
    assert record.outcome is UpdateOutcome.INCOMPLETE


# ─── the self-recovered half: the orchestration reporting its own rollback ────


def test_a_self_recovered_rollout_records_recovered_first_hand() -> None:
    """The gateway leg that rolls itself back to last-known-good does not need an
    observer to notice: it watched it happen, so it files `RECOVERED` directly.
    Recorded as a failure — the update did fail — but a distinct one, because the
    cluster is already running code that works."""
    begin_update(target_sha="8bdd366", origin="frontend", holder="mini:pid1")
    finish_update(
        UpdateOutcome.RECOVERED,
        failing_step="gateway local update (rc=1): recovered to last-known-good",
    )

    record = read_last_update()
    assert record is not None
    assert record.outcome is UpdateOutcome.RECOVERED
    assert record.failed is True
    assert "RECOVERED" in record.describe()


# ─── the rollout's own log: recorded once, by the write that knows it ─────────


def test_the_intent_write_records_the_log_this_run_is_writing() -> None:
    """The path exists only on the side that spawned the session, so it rides down
    with the run rather than being guessed at afterwards from the newest
    `rollout-*.log` on disk."""
    begin_update(
        target_sha="8bdd366",
        origin="frontend",
        holder="mini:pid1",
        log_path="/home/ava/.ava/logs/rollout-1785470000.log",
    )
    finish_update(UpdateOutcome.ABORTED)

    record = read_last_update()
    assert record is not None
    assert record.log_path == "/home/ava/.ava/logs/rollout-1785470000.log"


def test_nothing_after_the_intent_write_can_change_the_recorded_log() -> None:
    """The anti-clobber guarantee. Every later writer touches only its own columns,
    so no terminal write and no observer note — however late, however concurrent —
    can point this record at another run's log."""
    begin_update(
        target_sha="8bdd366",
        origin="frontend",
        holder="mini:pid1",
        log_path="/home/ava/.ava/logs/rollout-1785470000.log",
    )

    finish_update(UpdateOutcome.ABORTED, failing_step="gateway local update (rc=2)")
    note_observed_recovery("rolled back 8bdd366 -> 7e571b4")

    record = read_last_update()
    assert record is not None
    assert record.log_path == "/home/ava/.ava/logs/rollout-1785470000.log"


def test_an_update_with_no_log_of_its_own_does_not_inherit_the_previous_one() -> None:
    """A foreground `ava cluster update --local` is not teed to a file. Leaving the last
    rollout's log attached would send an operator to read a log about a different
    run — worse than sending them nowhere."""
    begin_update(
        target_sha="8bdd366",
        origin="frontend",
        holder="mini:pid1",
        log_path="/home/ava/.ava/logs/rollout-1785470000.log",
    )
    finish_update(UpdateOutcome.ABORTED)

    begin_update(target_sha="7e571b4", origin="cli:mini", holder="mini:pid2")
    finish_update(UpdateOutcome.ABORTED)

    record = read_last_update()
    assert record is not None
    assert record.log_path is None


def test_a_new_attempt_drops_the_previous_observer_note() -> None:
    """`begin_update` clears it with the rest of the terminal fields: a recovery note
    from the last rollout standing beside this attempt's `started_at` would describe
    the wrong rollout."""
    begin_update(target_sha="8bdd366", origin="frontend", holder="mini:pid1")
    finish_update(UpdateOutcome.ABORTED)
    note_observed_recovery("rolled back 8bdd366 -> 7e571b4")

    begin_update(target_sha="9999999", origin="cli:mini", holder="mini:pid2")

    record = read_last_update()
    assert record is not None
    assert record.observed_by is None
