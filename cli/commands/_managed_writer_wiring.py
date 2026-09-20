"""The managed-writer wiring: coordinator-side call positions of the W chain.

The enable point (`cli.commands._managed_writer_mode`) resolves one mode decision
per rollout; these are the rollout steps that consume it and drive the
publication seats (`cli.commands._update_publication`) on the coordinator (task
#4128):

- `_begin_managed_writer_publication` (E2-b) -- the P1 journal position, after
  the prepare gate and before the first stop effect: under an `active` decision
  the all-unit prepared plan is journaled so agent births freeze and the current
  publication is preserved until P5 completes. The plan's channel (prepared
  dispatch) is not connected yet (task #4129), so the step refuses explicitly
  today rather than let an `active` rollout stop the fleet without its journal.
  `off` / `blocked` skip it untouched.
- `_commit_managed_writer_publication` (E2-a) -- the post-Phase-B step: under an
  `active` decision, publish the completed pending publication through the P5
  commit seat. `off` / `blocked` skip it untouched, so the pre-wiring rollout
  behavior is unchanged; a call with no recorded decision skips with a visible
  beacon (it can only mean the position ran outside the rollout's read point).
  A set that cannot be published refuses fail-closed, leaving the pending
  journal for checked recovery (`ava cluster recover-pending`).

The remaining call position (E2-c collect+adopt) lands here in turn; until it
does, its seat stays unwired, and the inertness pin
(`tests/cli/test_update_publication.py::test_the_seat_has_no_unnamed_production_callsite`)
names this module as a conscious production exception beside the enable point.

Each step imports its seats lazily, inside the `active` check: nothing reaches
them unless the rollout actually entered the managed-writer mode.
"""

from __future__ import annotations

import sys

from shared.rollout_telemetry import stage as _stage_telemetry


def _begin_managed_writer_publication() -> int:
    """Journal the managed-writer activation (P1 coordinator step).

    The rollout's begin position of the managed-writer chain (task #4128,
    E2-b): under an `active` decision the P1 seat journals the all-unit
    prepared plan before the rollout stops anything, so agent births freeze
    and the current publication is preserved until P5 completes. `off` /
    `blocked` leave the seat untouched, and a call with no recorded decision
    skips with the same visible beacon as the commit step.

    The plan -- each registered unit's sealed receipt, candidate image digests
    and normal-service plan, produced inside the candidate images and gathered
    for the whole registered roster -- is the prepared-dispatch channel's to
    provide (task #4129), and that channel is not connected yet. Until it is,
    the step refuses explicitly instead of degrading: an `active` rollout must
    not reach the stop-the-world without its journal open (fail-closed).
    Returns the step's exit code: 0 for a clean skip, 1 for the refusal.
    """
    from cli.commands._managed_writer_mode import managed_writer_mode

    mode = managed_writer_mode()
    if mode is None:
        print(
            "  · managed-writer begin: no mode decision recorded in this "
            "process; skipping the position",
            file=sys.stderr,
        )
        return 0
    if mode.state != "active":
        return 0
    print(
        "\n✗ managed-writer begin refused: the all-unit prepared plan "
        "channel (prepared dispatch, task #4129) is not connected yet, so the "
        "publication cannot be journaled; the rollout stops before any effect.\n"
        "  disable the managed-writer switch or wait for the channel",
        file=sys.stderr,
    )
    return 1


def _commit_managed_writer_publication() -> int:
    """Publish the completed managed-writer activation (P5 coordinator step).

    The rollout's post-Phase-B step of the managed-writer chain: when the enable
    point resolved `active`, the units' normal-release continuations have
    recorded their readbacks in the durable pending journal, and this step
    publishes exactly that complete set through the P5 commit seat. `off` /
    `blocked` leave the seat untouched, so the pre-wiring rollout behavior is
    unchanged. A set that cannot be published refuses fail-closed: the
    journaled pending stays for checked recovery (`ava cluster
    recover-pending`), never silently dropped or cleared. Returns the step's
    exit code: 0 for a commit / clean skip / no pending entry, 1 for a refusal.

    A call with no recorded decision is an invariant breach, not a mode: the
    rollout's read point always runs before this position, so no decision can
    only mean the position was reached outside the orchestration. It skips --
    but with a visible beacon, because a silently-skipped invariant breach
    would mask the next refactor accident.
    """
    from cli.commands._managed_writer_mode import managed_writer_mode

    mode = managed_writer_mode()
    if mode is None:
        print(
            "  \u00b7 managed-writer commit: no mode decision recorded in this "
            "process; skipping the position",
            file=sys.stderr,
        )
        return 0
    if mode.state != "active":
        return 0
    from cli.commands._update_publication import commit_pending_publication
    from shared.db_transaction import write_transaction
    from shared.managed_writer_barrier import ManagedWriterBarrierError

    # The stage is entered only under the active decision: an off/blocked
    # rollout's log must not grow a managed-writer stage it never ran.
    with _stage_telemetry("managed_writer_commit"):
        try:
            with write_transaction() as conn:
                publication = commit_pending_publication(conn)
        except ManagedWriterBarrierError as exc:
            print(
                f"\n\u2717 managed-writer commit refused: {exc}\n"
                "  the pending journal remains; run `ava cluster recover-pending`",
                file=sys.stderr,
            )
            return 1
    if publication is not None:
        print(f"  \u2713 managed-writer publication committed -> {publication}")
    return 0
