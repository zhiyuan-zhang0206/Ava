"""`ava start` over a hold that carries failed continuation receipts.

A receipt is latched when an agent's continuation raised during the stop's drain
(`services/agent_runner/agent_host/lifecycle/maintenance.py`). The agent's restart pointer is durable in
Postgres, so the failure is crash-equivalent: the agent continues from its last
durable checkpoint once the hold releases and its restart is delivered. `ava start`
re-delivers each failed continuation, reports (an ERROR event the alert rules read) any whose pointer
is gone, then releases the hold like any other.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping

from base.agents.context.clients import DatabaseFactory
from base.deploy.maintenance import admission
from base.log import logger


def _lost_pointers(
    failed: Mapping[int, str], commands: Mapping[int, int], *, database_factory: DatabaseFactory
) -> dict[int, str]:
    """Why each failed agent's restart pointer cannot be delivered; absent when it can.

    An agent outside the hold's restart cohort (parked, or never captured) has no
    continuation to deliver, and a terminated one must not be revived by a stale
    restart; neither is lost. Every other failed agent needs its restart command
    still pending or claimed.
    """
    lost: dict[int, str] = {}
    owed = [agent for agent in failed if commands.get(agent)]
    if not owed:
        return lost
    with database_factory().connect() as conn:
        for agent in owed:
            command = commands[agent]
            row = conn.execute(
                "SELECT m.status, i.status FROM agents_meta m LEFT JOIN inbound_messages i "
                "ON i.id=%s AND i.agent_id=m.id AND i.kind='restart' WHERE m.id=%s",
                (command, agent),
            ).fetchone()
            if row is None:
                lost[agent] = "the agent's row is gone"
            elif row[0] != "terminated" and row[1] not in ("pending", "claimed"):
                lost[agent] = f"its restart command {command} is {row[1] or 'gone'}"
    return lost


def settle_failed_receipts(*, database_factory: DatabaseFactory) -> None:
    """Re-deliver every failed continuation of the standing hold and clear its receipts.

    Called once the unit is serving, immediately before the hold releases; the
    release's resume wakes every restart pointer, the failed agents' included.
    Nothing is cleared when the pointer check cannot run, so a retried
    `ava start` settles the same receipts.
    """
    current = admission.snapshot()
    if current is None or current.maintenance is None or not current.maintenance.failures:
        return
    hold = current.maintenance
    lost = _lost_pointers(hold.failures, hold.commands, database_factory=database_factory)
    cleared = admission.clear_failures()
    lost.update(
        _lost_pointers(
            {a: c for a, c in cleared.items() if a not in hold.failures},
            hold.commands,
            database_factory=database_factory,
        )
    )
    for agent, category in sorted(cleared.items()):
        if agent in lost:
            continue
        print(
            f"  → agent {agent}: continuation failed ({category}) at the maintenance hold; "
            "its restart is re-delivered when the hold releases",
            file=sys.stderr,
        )
    for agent, reason in sorted(lost.items()):
        # The `agent_continuation_lost` event is the owner's signal: the
        # `ava-ops-agent-continuation-lost` Grafana rule alerts on it.
        logger.error(
            "agent {agent_id}: continuation failed ({failure_category}) at maintenance hold "
            "{holder} and cannot be re-delivered: {reason}",
            event="agent_continuation_lost",
            agent_id=agent,
            failure_category=cleared.get(agent, "unknown"),
            holder=current.holder,
            reason=reason,
        )
        print(
            f"  ✗ agent {agent}: continuation failed ({cleared.get(agent, 'unknown')}) and "
            f"cannot be re-delivered: {reason}",
            file=sys.stderr,
        )
