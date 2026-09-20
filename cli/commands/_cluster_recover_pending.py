"""`ava cluster recover-pending` — recover (or exactly abort) an interrupted rollout's durable pending publication.

A rollout that entered the managed-writer publication protocol leaves a durable
`pending` record before it stops anything. That record outlives its lease by
design: every new `ava cluster update` is refused ("a durable pending
publication requires its checked recovery first"), every agent birth defers, and
no generic cluster recovery, settle or release will touch it. The supported way
forward is an explicit recovery — a new rollout lease, a new observation
challenge and a fresh complete per-unit writer closure
(`ops.publication_recovery`).

This verb is that seat's operator entrance. It performs every non-mutating proof
(live owner, journal shape, holder death) and, while the trusted closure producer
is not yet connected, refuses BEFORE touching the lease rather than take an
authority it cannot finish with. Hand-clearing evidence, hand-writing a closure,
or editing `runtime_protocol_version` is never the path — refusal is the command
working.

`--pre-stop` selects the window's other exit (task #4129 C-4): the exact
pre-stop abort. While every journaled unit reports no bootstrap-recovery journal
(no hop child has begun) and the journal records no effects, it clears the
never-effective pending record and releases the abandoned lease in one guarded
write; any doubt refuses and leaves the record for the checked recovery above.
"""

from __future__ import annotations

import sys


def cmd_cluster_recover_pending(*, pre_stop: bool = False) -> int:
    """Run the checked recovery seat for a stranded pending publication.

    Prints the abandoned publication it found, then runs the seat: 0 with the
    outcome once the producer is connected, 1 for every refusal (a live owner
    still holds the cluster, the recorded holder cannot be proven gone, the
    journal is unreadable, or the trusted closure producer is not connected in
    this build) — each refusal names its own next step.

    With `pre_stop=True`, runs the exact pre-stop abort instead: it proves
    every journaled unit is effect-free and clears the never-effective journal
    plus its abandoned lease; a unit that cannot answer, a unit that reports a
    journal, or a journal that already records effects refuses.
    """
    from ops.cluster import ClusterUpdateInProgress
    from ops.publication_recovery import (
        pending_publication_recovery_op,
        pre_stop_abort_pending_publication_op,
        read_pending_recovery_state,
    )

    try:
        state = read_pending_recovery_state()
        if state.pending and state.abandoned is not None:
            print(
                "· pending managed-writer publication from an interrupted rollout "
                f"(holder {state.abandoned.holder}, target {state.abandoned.target_sha[:7]})"
            )
        if pre_stop:
            result = pre_stop_abort_pending_publication_op()
        else:
            result = pending_publication_recovery_op()
    except ClusterUpdateInProgress as exc:
        print(f"\n✗ {exc}", file=sys.stderr)
        return 1
    if pre_stop:
        if bool(result["aborted"]):
            print(
                "✓ pre-stop abort: every journaled unit was proven effect-free — cleared the "
                "pending journal and released the abandoned lease (the staged files stay on "
                "disk; they were never effective)"
            )
            return 0
        print(f"✓ {result['detail']}")
        return 0
    if result["recovered"]:
        print(
            f"✓ recovery claimed under {result['new_holder']} — agent births stay frozen "
            "until the replacement operation completes or is itself recovered"
        )
        return 0
    print(f"✓ {result['detail']}")
    return 0
