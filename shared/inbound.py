"""Inbound-queue primitives shared across layers.

`InboundKind` is the piece of the inbound queue that layers below the kernel
reach for: the timeline renderer matches on it and the claim node dispatches
on it. The transactional claim / reconcile / status-transition SQL stays in
`agent/db.py` (kernel-only, async).

Wake is Redis pub/sub: `insert_inbound_message` (`shared/db.py`) publishes to
the cluster-scoped channel `<prefix>:inbound:<agent_id>`
(`shared.cluster.inbound_channel`) on every inbound INSERT, and the claim node
blocks on `RedisInboundListener.wait_one(timeout=...)` (`shared/redis_listener.py`)
until a publish arrives. There is no module-level channel constant because the
channel is per-agent and cluster-scoped — a hardcoded prefix would fall outside
a dev cluster's `&<prefix>:*` redis ACL grant.
"""

from enum import StrEnum


class InboundKind(StrEnum):
    """Legal values for inbound_messages.kind — in sync with the CHECK constraint in db/schema.sql.

    StrEnum lets the enum value be both a str (can be passed to SQL) and comparable
    to literals. The claim node's `match item.kind:` uses `case InboundKind.CHAT:`
    so pyright statically exhaustively checks (when a new kind is added but
    dispatch isn't synced, pyright reports "unmatched"). Same pattern as
    `shared.agents.AgentStatus(StrEnum)`.
    """

    CHAT = "chat"
    COMPACT_SUMMARY = "compact_summary"
    COMPACT_REQUEST = "compact_request"
    CANCEL = "cancel"
    TERMINATE = "terminate"
    RESTART = "restart"
    RESTART_COMPLETED = "restart_completed"
    RESURRECT = "resurrect"
    FORK = "fork"
    HEARTBEAT = "heartbeat"
