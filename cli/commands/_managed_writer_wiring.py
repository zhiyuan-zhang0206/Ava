"""The managed-writer wiring: coordinator-side call positions of the W chain.

The enable point (`cli.commands._managed_writer_mode`) resolves one mode decision
per rollout; these are the rollout steps that consume it and drive the
publication seats (`cli.commands._update_publication`) on the coordinator (task
#4128):

- `_commit_managed_writer_publication` (E2-a) -- the post-Phase-B step: under an
  `active` decision, publish the completed pending publication through the P5
  commit seat. Every other decision (`off` / `blocked` / no decision) skips it
  untouched, so the pre-wiring rollout behavior is unchanged. A set that cannot
  be published refuses fail-closed, leaving the pending journal for checked
  recovery (`ava cluster recover-pending`).

The remaining call positions (E2-b begin, E2-c collect+adopt) land here in turn;
until they do, their seats stay unwired, and the inertness pin
(`tests/cli/test_update_publication.py::test_the_seat_has_no_unnamed_production_callsite`)
names this module as a conscious production exception beside the enable point.

Each step imports its seats lazily, inside the `active` check: nothing reaches
them unless the rollout actually entered the managed-writer mode.
"""

from __future__ import annotations

import sys

from shared.rollout_telemetry import stage as _stage_telemetry


def _commit_managed_writer_publication() -> int:
    """Publish the completed managed-writer activation (P5 coordinator step).

    The rollout's post-Phase-B step of the managed-writer chain: when the enable
    point resolved `active`, the units' normal-release continuations have
    recorded their readbacks in the durable pending journal, and this step
    publishes exactly that complete set through the P5 commit seat. Every other
    decision (`off` / `blocked` / no decision) leaves the seat untouched, so the
    pre-wiring rollout behavior is unchanged. A set that cannot be published
    refuses fail-closed: the journaled pending stays for checked recovery (`ava
    cluster recover-pending`), never silently dropped or cleared. Returns the
    step's exit code: 0 for a commit / clean skip / no pending entry, 1 for a
    refusal.
    """
    from cli.commands._managed_writer_mode import managed_writer_mode

    mode = managed_writer_mode()
    if mode is None or mode.state != "active":
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
