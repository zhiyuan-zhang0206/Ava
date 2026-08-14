"""`ava cluster recover` — clear a deploy lock that no live deploy owns.

`ava cluster update --force` skips the cluster-wide deploy-window check, which is the right
override for a *signal* the operator knows is stale — an unreachable host, a reaped
updater session. It does not help against the other half: a crashed orchestration
leaves a live `cluster_update_lock` row behind, and `_run_gateway_orchestration`
takes that lock *after* the window check, so a forced rollout still aborts with
"another cluster update is in progress" until the lock's TTL expires. That is up to
`LOCK_TTL_S` (30 min) of a cluster nobody can deploy to, on the strength of a
process that is already dead.

`ops.ops_cluster.cluster_recover_op` has always been able to fix that — it clears
the lock plus a stranded paused posture, and carries the safety that makes it
sound: it refuses while the lease holder's process is still running (pid-probed
when the holder is this host) and while this host's updater lease is live, so it
can only ever clear a hold whose owner is definitively gone. It was reachable only through
`POST /api/cluster/recover`, on the gateway — and a stranded deploy hold usually
comes with a gateway that is down, so the moment the override is most needed is the
moment HTTP cannot deliver it. This verb runs the same op in-process, needing only
the data plane.

A holder on *another* machine cannot be pid-probed from here and is conservatively
treated as live (refuse rather than risk clobbering a real rollout); wait out its
TTL or run this on that host.
"""

from __future__ import annotations

import sys


def cmd_cluster_recover() -> int:
    """Clear a stranded deploy lock + paused posture on this host.

    Prints the holder it is about to clear before clearing it, so the operator sees
    what owned the cluster even when the answer is "nothing, it was already free".
    Returns 0 when the cluster is (or has been made) deployable, 1 when a live
    deploy still owns it — that refusal is the command working, not failing.
    """
    from ops.cluster import ClusterUpdateInProgress
    from ops.ops_cluster import cluster_recover_op
    from shared.cluster_lock import update_lock_holder

    holder = update_lock_holder()
    if holder is None:
        print("· no update lock is held; checking for a stranded pause anyway")
    else:
        print(f"· update lock held by {holder}")

    try:
        result = cluster_recover_op()
    except ClusterUpdateInProgress as e:
        print(f"\n✗ {e}", file=sys.stderr)
        return 1

    cleared = result["unlocked_holder"]
    if cleared is None:
        print("✓ no update lock to clear; this host is unpaused")
    else:
        print(f"✓ cleared the stranded update lock (was {cleared}) and unpaused this host")
    print("  a new `ava cluster update` can start now.")
    return 0
