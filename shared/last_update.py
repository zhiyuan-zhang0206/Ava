"""The last cluster update's outcome — a fact the surfaces state, not one they infer.

A failed rollout used to leave nothing behind but a **symptom**. On 2026-07-30 the
21:10 rollout failed and auto-recovered to last-known-good; recovery worked, but the
status page showed a yellow warning on a pin/head mismatch and never said that an
update had failed, when, toward what commit, or at which step. A sha mismatch is
shared by several unrelated states (a node that missed a rollout, a checkout moved
without a restart, a deliberate pin hold), so an operator reading one has to
reconstruct which it is. "The last update failed" is the fact; the mismatch is one
of its symptoms.

**Written ahead of the work, because a process that dies cannot write its own
epitaph.** `begin_update` records the attempt — target, origin, and the deploy-lease
holder that is about to do it — before Phase A touches anything. `finish_update`
records how it ended. An orchestration that is killed between them never runs
`finish_update`, and that is exactly the case the record has to cover: the reader
resolves a still-open row by asking whether its holder still holds the lease
(`shared.cluster_lock`). Holder gone, row still open -> `ORPHANED`, which is a
report of a death observed from outside rather than a report the dead process
managed to file. The same move covers the other half of the 2026-07-30 story: the
rollback that cleaned up records what it did, and the reader turns a failure
carrying that observation into `RECOVERED` — the state in which nothing needs
repairing and the operator only needs telling.

Singleton, like `cluster_pin`: one standing record that each rollout overwrites, so
a **successful update clears a previous failure** by replacing it rather than by
anyone remembering to reset a flag.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

import shared.db


class UpdateOutcome(StrEnum):
    """How the last update ended, as the surfaces need to say it.

    `CLEAN` / `INCOMPLETE` / `ABORTED` mirror
    `cli.commands._update_recover.RolloutOutcome` — the orchestration's own
    three-way verdict, persisted rather than re-derived, so the banner cannot
    disagree with the rollout log.

    `RECOVERED` is the fourth terminal state, and it is a distinct one rather than a
    shade of `ABORTED` because it answers a different operator question: the attempt
    failed AND the cluster is already back on a commit that works, so there is
    nothing to repair — only something to know. It is reached two ways, and both are
    real recoveries: the orchestration's own gateway leg rolling back to
    last-known-good (written by `finish_update`), and a *later* `ava cluster
    rollback` cleaning up after an orchestration that died (derived by
    `read_last_update` from the observation that rollback left behind). Collapsing
    it into `ABORTED` was what made the 2026-07-30 recovery read as an open incident.

    `RUNNING` and `ORPHANED` are the two readings of a row with no recorded end, and
    they are told apart by the deploy lease rather than by a timeout: a rollout runs
    for as long as it runs (the lease is renewed while it does), so elapsed time
    proves nothing, while the holder having stopped renewing does.
    """

    CLEAN = "clean"
    RECOVERED = "recovered"
    INCOMPLETE = "incomplete"
    ABORTED = "aborted"
    RUNNING = "running"
    ORPHANED = "orphaned"


# The outcomes an operator has to be told about. RUNNING is normal and already has
# its own in-progress indicator; CLEAN is the steady state. RECOVERED is in here
# even though the cluster is healthy: the update still failed, and 2026-07-30 is
# precisely the incident where a recovery nobody was told about left the operator
# reading a sha mismatch as a live fault.
FAILED_OUTCOMES = frozenset(
    {
        UpdateOutcome.RECOVERED,
        UpdateOutcome.INCOMPLETE,
        UpdateOutcome.ABORTED,
        UpdateOutcome.ORPHANED,
    }
)


class LastUpdate(BaseModel):
    """The last cluster update, as served to `ava cluster status` and the frontend.

    `failed` is computed here rather than in each surface: two consumers deciding
    independently what counts as a failure is how they end up disagreeing, and this
    is the value a banner is switched on.
    """

    model_config = ConfigDict(frozen=True)

    outcome: UpdateOutcome
    failed: bool
    target_sha: str | None = None
    origin: str | None = None
    # The deploy-lease holder that opened this attempt, `<machine>:pid<N>`
    # (`shared.cluster_lock.self_holder`). Carried on the model rather than left inside
    # the RUNNING/ORPHANED derivation because it is the only place the *process* running
    # a rollout is named: `ops.controllers.stalled_rollout` turns it into the pid it
    # interrupts when the orchestration stops making progress, and re-reading the lease
    # to recover a field this read already had would be a second answer to one question.
    holder: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    # The orchestration step that failed, in the orchestration's own words
    # (`local update failed (rc=2)`, `phase B poll`), or None on a clean run.
    failing_step: str | None = None
    # What an EXTERNAL observer recorded about this attempt — today the rollback
    # that cleaned up after it ("rolled back to abc1234"). The orchestration that
    # dies files nothing, so a reader can derive THAT it failed (an open row with
    # no live lease) but not what has since been done about it; this is that half.
    observed_by: str | None = None
    # Whether the gateway reached its target and moved the cluster pin before the
    # rollout ended. A failure with the pin ADVANCED and one with it held are
    # different situations, and the difference is not visible from the pin alone.
    pin_advanced: bool = False
    # The rollout session's own log, as an absolute path — the file the detached
    # session tees into. Recorded so the surfaces can name THE log rather than the
    # `rollout-<epoch>.log` glob an operator then has to pick from by mtime.
    # Written once, by `begin_update`, from the path `spawn_rollout` created before
    # it launched this orchestration; None for a foreground `ava cluster update --local`,
    # which is not teed to a file and must not borrow the previous rollout's log.
    log_path: str | None = None

    def _observed(self) -> str:
        return f" — since then: {self.observed_by}" if self.observed_by else ""

    def describe(self) -> str:
        """One line for `ava cluster status` and the frontend banner."""
        target = f" to {self.target_sha[:7]}" if self.target_sha else ""
        when = f" at {self.started_at:%Y-%m-%d %H:%M}" if self.started_at else ""
        if self.outcome is UpdateOutcome.CLEAN:
            return f"last update{target}{when}: succeeded"
        if self.outcome is UpdateOutcome.RUNNING:
            return f"update{target}{when}: in progress"
        if self.outcome is UpdateOutcome.RECOVERED:
            step = f" at {self.failing_step}" if self.failing_step else ""
            return (
                f"last update{target}{when} FAILED{step}, and the cluster RECOVERED onto a "
                f"commit that works — nothing to repair{self._observed()}"
            )
        if self.outcome is UpdateOutcome.ORPHANED:
            return (
                f"last update{target}{when} FAILED: its orchestration died without "
                f"reporting an outcome (its deploy lease is gone){self._observed()}"
            )
        step = f" at {self.failing_step}" if self.failing_step else ""
        pin = "the cluster pin advanced" if self.pin_advanced else "the cluster pin was not moved"
        return f"last update{target}{when} FAILED ({self.outcome}){step}; {pin}{self._observed()}"


def begin_update(
    *, target_sha: str | None, origin: str, holder: str, log_path: str | None = None
) -> None:
    """Record that an update is starting, before it touches anything.

    `holder` is the deploy-lease holder this orchestration already took
    (`shared.cluster_lock.self_holder()`); it is what lets a reader decide later
    whether a row with no recorded end is still running or was orphaned.

    `log_path` is the rollout session's log, threaded in from the `spawn_rollout`
    that created the file and launched this process (`ava cluster update --rollout-log`).
    It is recorded HERE and nowhere else on purpose: the intent write is the one
    moment the path is known first-hand, so the record names the log this run is
    actually writing rather than whichever `rollout-*.log` a later reader would
    guess at. Nothing downstream rewrites it — `finish_update` and
    `note_observed_recovery` each touch only their own columns — so no
    late-arriving writer can attach a stale log to a newer record.

    Clears the previous run's terminal fields in the same statement — a stale
    `failing_step` or `observed_by` surviving beside a new `started_at` would
    describe the wrong rollout, which is the failure mode this whole record exists
    to avoid. This is also where a previous FAILURE stops being shown, and it
    happens here rather than at the pin advance so that the record never reads
    `failed` while the new rollout is already moving the cluster: from this write
    until the `finally`, an open row under a live lease reads `running`.
    """
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE cluster_last_update SET target_sha = %s, origin = %s, holder = %s, "
            "log_path = %s, started_at = now(), ended_at = NULL, outcome = NULL, "
            "failing_step = NULL, observed_by = NULL, pin_advanced = FALSE WHERE id = 1",
            (target_sha, origin, holder, log_path),
        )
        _assert_singleton(cur.rowcount)
        # Mirror into the deployment_state row's last_outcome (R1 wave, Task
        # #1021): same writer, same transaction — the state row answers "what was
        # the last result" without a second table read. The old readers keep
        # reading cluster_last_update until the old-signal sweep; nothing reads
        # this column yet. holder is deliberately NOT mirrored: deployment_state
        # has one holder column and it is the deploy LEASE's, owned by
        # shared.cluster_lock — writing the last-outcome's holder there would
        # wedge the next acquire.
        cur.execute(
            "UPDATE deployment_state SET target_sha = %s, origin = %s, "
            "log_path = %s, started_at = now(), ended_at = NULL, outcome = NULL, "
            "failing_step = NULL, observed_by = NULL, pin_advanced = FALSE WHERE id = 1",
            (target_sha, origin, log_path),
        )
        _assert_singleton(cur.rowcount)


def finish_update(
    outcome: UpdateOutcome,
    *,
    failing_step: str | None = None,
    pin_advanced: bool = False,
) -> None:
    """Record how the update ended. Called from the orchestration's `finally`, so it
    runs on the abort paths too — those are the ones worth reporting.

    Only the four terminal outcomes belong here — including `RECOVERED`, which the
    orchestration writes for itself when its own gateway leg rolled back to
    last-known-good, because that is a first-hand report and not an inference.
    `RUNNING` / `ORPHANED` are readings of an *unfinished* row and are never written.

    Leaves `log_path` alone: it was recorded by `begin_update` for this run, and a
    second writer touching it is how a record ends up naming another run's log.
    """
    if outcome in (UpdateOutcome.RUNNING, UpdateOutcome.ORPHANED):
        raise ValueError(f"{outcome} is a reading of an unfinished row, not a recorded outcome")
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE cluster_last_update SET ended_at = now(), outcome = %s, "
            "failing_step = %s, pin_advanced = %s WHERE id = 1",
            (str(outcome), failing_step, pin_advanced),
        )
        _assert_singleton(cur.rowcount)
        cur.execute(
            "UPDATE deployment_state SET ended_at = now(), outcome = %s, "
            "failing_step = %s, pin_advanced = %s WHERE id = 1",
            (str(outcome), failing_step, pin_advanced),
        )
        _assert_singleton(cur.rowcount)


def note_observed_recovery(reason: str) -> None:
    """Record what an external observer did about the last update, without claiming
    to know how that update ended.

    The one thing a dying orchestration cannot leave behind is a report — but the
    processes that clean up after it can, because cleaning up IS witnessing the
    death. `ava cluster rollback` (which the health probe's `--auto-rollback` shells
    into) is the first such observer: it knows the cluster was rolled back and to
    what, which is exactly the sentence that turned the 2026-07-30 recovery from an
    inexplicable pin change into a stated action.

    Deliberately does NOT touch `outcome`, even though a rollback IS a recovery and
    the surfaces do end up saying `RECOVERED`. The observer writes the *fact* it
    witnessed and `read_last_update` turns that into the status, for the same reason
    `ORPHANED` is derived rather than written: an observer that stamped the verdict
    would be filing a verdict on an attempt it never ran, and could overwrite one the
    orchestration itself had filed. **Refuses to annotate a CLEAN record**: a
    rollback can follow a successful update (a bad commit that deployed fine), and
    hanging a recovery note on it would read as that update having failed.

    Touches only `observed_by`, so it can neither move a terminal outcome nor
    disturb the `log_path` this run's `begin_update` recorded.
    """
    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE cluster_last_update SET observed_by = %s "
            "WHERE id = 1 AND started_at IS NOT NULL AND outcome IS DISTINCT FROM %s",
            (reason, str(UpdateOutcome.CLEAN)),
        )
        cur.execute(
            "UPDATE deployment_state SET observed_by = %s "
            "WHERE id = 1 AND started_at IS NOT NULL AND outcome IS DISTINCT FROM %s",
            (reason, str(UpdateOutcome.CLEAN)),
        )


def read_last_update() -> LastUpdate | None:
    """The last update's record, or None when no update has ever been recorded.

    Resolves an unfinished row against the deploy lease: still held by the same
    holder -> `RUNNING`; not held -> `ORPHANED`, the orchestration died. That is the
    one reading nothing else can produce, and it is why the row is written up front.

    Then folds in the second thing no writer could state: a failed attempt that an
    external observer has since recovered reads `RECOVERED`. Both derivations are
    the same move — the reader combining a fact with something the writer could not
    know at write time.
    """
    from shared.cluster_lock import read_update_lease

    with shared.db.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT target_sha, origin, holder, started_at, ended_at, outcome, "
            "failing_step, observed_by, pin_advanced, log_path "
            "FROM cluster_last_update WHERE id = 1"
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("cluster_last_update singleton row missing (its migration seeds it)")
    (
        target_sha,
        origin,
        holder,
        started_at,
        ended_at,
        outcome,
        failing_step,
        observed_by,
        pinned,
        log_path,
    ) = row
    if started_at is None:
        return None  # seeded but never written: no update has run against this cluster
    if outcome is None:
        lease = read_update_lease()
        resolved = (
            UpdateOutcome.RUNNING
            if lease is not None and lease.holder == holder
            else UpdateOutcome.ORPHANED
        )
    else:
        resolved = UpdateOutcome(outcome)
    # A recovery witnessed from outside promotes a failure to RECOVERED. Gated on
    # the row already reading as a failure so the promotion can never touch the two
    # states it would misreport: CLEAN (a rollback after a good update — the update
    # did not fail) and RUNNING (a live rollout owns its own state). What the
    # recovery WAS stays in `observed_by`, and the pin/step columns keep saying what
    # failed, so nothing is lost by naming the newer fact first.
    if observed_by and resolved in FAILED_OUTCOMES:
        resolved = UpdateOutcome.RECOVERED
    return LastUpdate(
        outcome=resolved,
        failed=resolved in FAILED_OUTCOMES,
        target_sha=target_sha,
        origin=origin,
        holder=holder,
        started_at=started_at,
        ended_at=ended_at,
        failing_step=failing_step,
        observed_by=observed_by,
        pin_advanced=pinned,
        log_path=log_path,
    )


def _assert_singleton(rowcount: int) -> None:
    # Seeded by its migration and never deleted, so the UPDATE must hit exactly it.
    # rowcount 0 means the row vanished — an invariant breach, surfaced rather than
    # silently no-op'd. Same guard, same reason, as `shared.cluster_pin`.
    if rowcount != 1:
        raise RuntimeError(f"cluster_last_update singleton row missing (rowcount={rowcount})")
