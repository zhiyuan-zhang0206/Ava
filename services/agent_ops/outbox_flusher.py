"""Resident retrier for the deferred-delivery outbox (task #3757).

The recording half of the outbox runs in whatever process saw its send fail
(`base.agents.messages.delivery_outbox.record_failed_send`); this half is the dead hand: a
resident loop of the machine's ops server that keeps redelivering due records —
through the canonical chat-inbound path — until they land or their budget is
spent. It outlives every sender process by construction, which is the whole point.

The loop is one of the ops server's resident loops (`services/agent_ops/daemon.py`
owns it in a `TaskGroup` beside the server): one round per configured tick, each
round one `asyncio.to_thread`; a quiesced unit (an `ava stop` draining) skips rounds
and keeps its records. The state of every record (attempts, flush attempts, the
backoff position, the abandonment) is the record's own file, not process memory and
not the database: a failed send is usually a failure to reach the data plane, so the
journal must be readable without it. A restart therefore resumes every cooldown.

A round that raises ends the loop, and through the `TaskGroup` the ops server: the
supervisor restarts it. An unreadable record or a failed delivery is not such an
error — the flush pass keeps the record and counts the attempt.
"""

from __future__ import annotations

import asyncio
import logging

from psycopg_pool import ConnectionPool

from base.agents.messages import delivery_outbox
from base.daemon import round_loop
from base.daemon.loop_health import LoopProgress
from base.deploy.maintenance import admission

_log = logging.getLogger("services.agent_ops.outbox_flusher")

# One record costs at most three connection waits of one flush interval each (the
# agent lookup, the notice policy, the insert); a loop that has handled no record for
# that plus this slack is wedged.
_CONNECTIONS_PER_RECORD = 3
_LIVENESS_SLACK_S = 60.0
# Until the first round has read the live knobs.
INITIAL_LIVENESS_TIMEOUT_S = 600.0


def liveness_timeout_s(flush_interval_seconds: float) -> float:
    """How long the loop may go without finishing a record (or a round) before
    `/healthz` reads it as wedged."""
    return (_CONNECTIONS_PER_RECORD + 1) * flush_interval_seconds + _LIVENESS_SLACK_S


async def outbox_round(pool: ConnectionPool, progress: LoopProgress, cadence: list[float]) -> None:
    """One redelivery round: read the live knobs, then flush unless quiesced.

    `cadence` is the one-slot cell the loop reads its next wait from: the interval
    in force when the round started.
    """
    knobs = await asyncio.to_thread(delivery_outbox.limits)
    cadence[:] = [knobs.flush_interval_seconds]
    progress.timeout_s = liveness_timeout_s(knobs.flush_interval_seconds)
    if admission.quiesced():
        return
    report = await asyncio.to_thread(delivery_outbox.flush, pool, on_record=progress.beat)
    if report.touched or report.expired:
        _log.info(
            "[delivery-outbox] flush pass: delivered=%s buffered=%s abandoned=%s "
            "deferred=%s unreadable=%s expired=%s",
            report.delivered,
            report.buffered,
            report.abandoned,
            report.deferred,
            report.unreadable,
            report.expired,
        )


async def outbox_loop(pool: ConnectionPool, progress: LoopProgress) -> None:
    """The outbox redelivery as a resident sequential loop, one immediate round
    first (so a restart right after a recovery backfills at once)."""
    cadence: list[float] = []

    async def one_round() -> None:
        await outbox_round(pool, progress, cadence)

    await round_loop.run_rounds("delivery-outbox", progress, lambda: cadence[0], one_round)
