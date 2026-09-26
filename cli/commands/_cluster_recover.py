"""`ava cluster recover` — clear a deploy lease and pause that no live owner holds.

A deploy lease or a paused host posture can outlive the process that took it.
This verb runs `ops.ops_cluster.cluster_recover_op` in-process, so it needs only
the data plane, not the gateway HTTP API that is often down in the same incident.

The op clears nothing while any owner may still act: it refuses on an unresolved
updater handoff, on a deploy-lease holder whose process is still running
(pid-probed when the holder is this host), and on this host's live updater lease.
Only then does it claim the stale lease by compare-and-set, unpause this host and
release the lease.

A holder on *another* machine cannot be pid-probed from here and is conservatively
treated as live (refuse rather than risk clobbering a real owner); wait out its
TTL or run this on that host. Prepared release operations are not recovered here:
they continue by resubmitting their captured request.
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
    from ops.ops_cluster import ClusterUpdateInProgress, cluster_recover_op
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
    return 0
