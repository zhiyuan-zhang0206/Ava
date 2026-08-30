"""`ava cluster cancel` — formally cancel a live rollout/restart orchestration.

The formal cancel path (P1, 2026-08-30): before this verb, an operator watching a
rollout being dragged by one stuck host had no controlled way out — the only
"cancel" was hand-killing processes, which leaves the cluster paused and the
deploy lease held. The orchestration's own `finally` is the recovery (compensating
resume of every paused host, the deploy lease released or converted to a settle
hold, the durable maintenance marker cleared), and it is what runs when the
orchestration process receives `SIGINT` — the same unwinding the stalled-rollout
controller triggers unattended after its no-progress bound. This verb sends that
signal on purpose, now: `ops.ops_cluster.cluster_cancel_op` probes the holder
pid, refuses when nothing provably live is there to cancel, and the `finally`
does the rest.

Runs in-process (like `ava cluster recover`), so it works with the gateway's
HTTP surface down — the orchestration process lives on this host, and its pid is
in the deploy lease's holder string.
"""

from __future__ import annotations

import sys


def cmd_cluster_cancel() -> int:
    """Cancel this host's live rollout/restart orchestration via SIGINT.

    Prints what it found before acting, so the operator sees what owned the
    cluster. Returns 0 when the cancel landed (the orchestration is unwinding —
    watch its rollout log, `ava cluster status` after), 1 when there was nothing
    provably live to cancel — that refusal is the command working, not failing.
    """
    from ops.cluster import ClusterUpdateInProgress
    from ops.ops_cluster import cluster_cancel_op
    from shared.cluster_lock import update_lock_holder

    holder = update_lock_holder()
    if holder is None:
        print("· no deploy lease is held; looking for a running orchestration anyway")
    else:
        print(f"· deploy lease held by {holder}")

    try:
        result = cluster_cancel_op()
    except ClusterUpdateInProgress as e:
        print(f"\n✗ {e}", file=sys.stderr)
        return 1

    print(f"✓ cancelled: SIGINT sent to {result['cancelled']}")
    print(
        "  the orchestration is unwinding along its own recovery path: every paused "
        "host resumes, the deploy lease releases (or settles over hosts still "
        "mid-transition), and the maintenance marker clears. Watch its rollout log; "
        "`ava cluster status` after."
    )
    return 0
