"""The per-job deadlines of the watchdog's recovery loops (the round loop and the
fan-out they run on live in `base.daemon.round_loop`)."""

from __future__ import annotations

# Liveness slack above one job's deadline: a loop that has completed no round
# step for deadline + this reads as wedged on /healthz.
_LIVENESS_SLACK_S = 60.0


def rpc_deadline_s() -> float:
    """Overall deadline for one per-agent job. A job makes at most two cluster
    dispatches (hosted-turn recovery: terminate, then resurrect), each bounded by
    the RPC client's own timeout and retry budget, so the deadline is twice the
    client's worst case: it only cuts a job the client's budgets did not."""
    from ops.cluster_rpc import worst_case_dispatch_seconds

    return 2 * worst_case_dispatch_seconds()


def loop_liveness_timeout_s() -> float:
    """How long a recovery loop may go without completing a round step before
    `/healthz` reads it as wedged: a step is a round boundary or one finished
    per-agent job, so one job's deadline plus slack."""
    return rpc_deadline_s() + _LIVENESS_SLACK_S
