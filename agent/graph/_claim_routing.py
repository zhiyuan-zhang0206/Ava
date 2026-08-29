"""Lifecycle routing for the claim node: where a pass may go + who wins.

Holds the ClaimGoto vocabulary (the four targets claim itself routes to), the
routing-gated kinds (_ROUTING_KINDS), and the single batch-winner resolution
(_Routing / resolve_routing). Extracted from agent/graph/_claim.py (Task
#1006 split — the lifecycle-routing axis).

Routing semantics (preserved verbatim from the original claim module):

- Lifecycle routing within one batch is decided by the most RECENT genuine
  intent, by row id (terminate / restart = exit; resurrect = revive) — not a
  fixed kind precedence. A batch holding several of these only happens after
  the agent was stuck / down long enough for commands to pile up: it is a
  replay log of user intents, so the latest one wins. This is what lets a
  resurrect bring back an agent whose prior-life terminate still sits in the
  same batch (the terminate predates the death; the resurrect is newer). The
  winner is resolved up front by id, so the dispatch loop's arm order cannot
  decide it (per-arm pairwise guards is how order-dependent holes happen).
- cancel is a pause, not a routing intent — it always yields to a co-batched
  exit/revive (a stuck agent gets Stop-then-Terminate; the kill must not be
  paused away). One deliberate exception to "chat never decides routing": a
  terminate yields to a chat the terminating decision did not see — a chat
  co-batched with a self-initiated terminate (it arrived during the very turn
  the agent terminated in), or a chat newer than an external terminate (the
  latest intent), or any pending inbound newer than the whole claimed batch
  (the world moved before the process could die). The vetoed terminate is a
  consumed no-op — the agent stays alive and processes the message (see the
  veto handling in decide / _claim_node_impl).
- restart_completed / fork / compact never decide routing: their markers are
  appended and content carried regardless of who wins. Under an exit winner
  compact_request is dropped (its LLM call could raise before the exit arm
  runs); compact_summary still applies (agent-authored, zero cost).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from agent.db import ClaimedInbound, has_pending_inbound_after
from agent.graph._context import AvaContext
from shared.inbound import InboundKind

# The four targets claim itself routes to: BEFORE_LLM (has work), END
# (terminate with exit_requested=True, a hosted restart with
# restart_requested=True, or the turn boundary with both False — the runloop
# re-invokes and the fresh invocation's claim waits), back
# to CLAIM (cancel/pause -> re-enter with halted=True; the re-entered pass
# hits the turn boundary and ends the invocation), or INIT_CONTEXT (a
# compaction emptied the history; that node re-establishes the standing head,
# then resumes where this batch was headed). Narrower than `NodeName` on
# purpose, so pyright catches a goto claim has no business making.
ClaimGoto = Literal["before_llm", "claim", "__end__", "init_context"]

# Kinds whose dispatch is gated on the routing winner.
_ROUTING_KINDS: frozenset[InboundKind] = frozenset(
    {
        InboundKind.COMPACT_REQUEST,
        InboundKind.TERMINATE,
        InboundKind.RESTART,
        InboundKind.RESURRECT,
    }
)


@dataclass(frozen=True)
class _Routing:
    """Resolved routing for one claimed batch — winner + veto, decided once.

    exit_kind is the winning exit (TERMINATE or RESTART), or None when no exit
    won (a revive won, or there were no routing intents).  has_revive is True
    when at least one RESURRECT shares the batch.  terminate_vetoed_by_pending
    records whether veto half 2 fired (post-claim arrival stole the kill).
    """

    exit_kind: InboundKind | None
    """The winning exit kind (TERMINATE or RESTART), or None."""

    has_revive: bool
    """True when at least one RESURRECT is in the batch."""

    terminate_vetoed_by_pending: bool
    """Veto half 2: a pending inbound newer than the batch blocked the kill."""

    def is_winner(self, item: ClaimedInbound) -> bool:
        """Whether *item* should be dispatched given the routing outcome.

        Replaces the three per-arm guards in the original dispatch loop
        (``if exit_kind is not X: continue``) with a single call per routing
        kind.  Non-routing kinds (CHAT, CANCEL, FORK, …) always return True.
        """
        kind = item.kind
        if kind == InboundKind.TERMINATE:
            return self.exit_kind is InboundKind.TERMINATE
        if kind == InboundKind.RESTART:
            return self.exit_kind is InboundKind.RESTART
        if kind in (InboundKind.RESURRECT, InboundKind.COMPACT_REQUEST):
            return self.exit_kind is None
        return True

    @property
    def cancelled_applies(self) -> bool:
        """Cancel is effective only when no exit won and no revive shares the batch."""
        return self.exit_kind is None and not self.has_revive


async def resolve_routing(
    ctx: AvaContext,
    agent_id: int,
    batch: list[ClaimedInbound],
) -> _Routing:
    """Resolve the single batch winner: max-id exit vs revive, plus veto logic.

    All DB I/O is injected via *ctx* so the function is testable with a mock
    ``has_pending_inbound_after``.  No other side-effects.
    """
    assert ctx.ops_pool is not None, "resolve_routing requires ctx.ops_pool"  # noqa: S101
    latest_exit = max(
        (it for it in batch if it.kind in (InboundKind.TERMINATE, InboundKind.RESTART)),
        key=lambda it: it.id,
        default=None,
    )
    latest_revive = max(
        (it for it in batch if it.kind == InboundKind.RESURRECT),
        key=lambda it: it.id,
        default=None,
    )

    exit_kind: InboundKind | None = (
        InboundKind(latest_exit.kind)
        if latest_exit is not None and (latest_revive is None or latest_exit.id > latest_revive.id)
        else None
    )

    terminate_vetoed_by_pending = False
    if exit_kind is InboundKind.TERMINATE:
        assert latest_exit is not None, (  # noqa: S101
            "exit_kind == TERMINATE implies the exit row is a terminate"
        )
        latest_chat = max(
            (it for it in batch if it.kind == InboundKind.CHAT),
            key=lambda it: it.id,
            default=None,
        )
        # Veto half 1: same-batch chat the terminate decision did not see.
        if latest_chat is not None and (
            latest_exit.source == "self" or latest_chat.id > latest_exit.id
        ):
            exit_kind = None
        # Veto half 2: post-claim arrival newer than the whole batch.
        elif await has_pending_inbound_after(
            ctx.ops_pool, agent_id, after_id=max(it.id for it in batch)
        ):
            exit_kind = None
            terminate_vetoed_by_pending = True

    return _Routing(
        exit_kind=exit_kind,
        has_revive=latest_revive is not None,
        terminate_vetoed_by_pending=terminate_vetoed_by_pending,
    )
