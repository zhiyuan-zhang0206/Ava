"""Inbox read surface: the pending-chat strip and the FYI notice lifetime."""

from datetime import datetime
from typing import Any, NamedTuple

import psycopg


class InboundRow(NamedTuple):
    """One row of inbound_messages (for timeline reads)."""

    id: int
    content: str
    kind: str
    source: str | None
    status: str
    created_at: datetime
    claimed_at: datetime | None = None
    # JSONB sidecar, None when the column is NULL. Only readers that need it
    # select the column (list_pending_inbounds — the multimodal
    # `{"content_blocks": [...]}` shape); the rest leave it defaulted.
    payload: dict[str, Any] | None = None


# FYI notice (require_response=false) lifetime in the open queue: after this many
# days an unread FYI is auto-resolved ('read') — the open feed, the unread badge
# and the IM bridge stop carrying it (audit 05-gateway-lifecycle C1: open FYIs
# used to pile up forever). require_response notices NEVER expire — the user's
# answer is the only close. The expiry is applied as a query-side rule
# (created_at cutoff, `make_interval(days => ...)`) plus a lazy auto-resolve in
# the gateway's open-feed query, so no background sweeper is needed. One knob:
# change it here (and re-run the notice tests) to configure the TTL.
NOTICE_FYI_TTL_DAYS = 30


def list_pending_inbounds(db: psycopg.Connection, agent_id: int) -> list[InboundRow]:
    """List chat inbounds still queued for an agent, oldest first.

    Only `status='pending'` (the claim node has not picked these up yet)
    and `kind='chat'` (user-visible dialogue, not control signals). Once a
    message is claimed it enters the agent's in-memory messages and shows
    up in the timeline snapshot, so it is intentionally excluded here — the
    web UI renders these as a compact "pending" strip above the composer,
    distinct from the timeline.

    Rows the active takeover pipeline has already absorbed are also
    excluded: while an unexpired `active` lease exists, a pending chat that
    the session trail has transcribed (an `agent_impersonation_entries`
    row — what the timeline renders) or that the relay has read
    (`agent_impersonation_messages`) is display-side delivered, and must
    not appear in both the timeline and the strip (#3683). The row itself
    stays `status='pending'` — this is a read-surface view, not a state
    change; once the lease ends, unacknowledged rows are visible here
    again (delivery evidence counts only while the lease is alive).

    `payload` rides along (unlike the other InboundRow readers) because the
    strip renders multimodal messages: the endpoint extracts their image
    reference urls from the content blocks so the browser can show
    thumbnails before the message is claimed.
    """
    with db.cursor() as cur:
        cur.execute(
            "SELECT ib.id, ib.content, ib.kind, ib.source, ib.status, ib.created_at, "
            "       ib.claimed_at, ib.payload "
            "FROM inbound_messages ib "
            "WHERE ib.agent_id = %s AND ib.status = 'pending' AND ib.kind = 'chat' "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM agent_impersonations lease "
            "  WHERE lease.agent_id = ib.agent_id "
            "    AND lease.status = 'active' "
            "    AND lease.expires_at > clock_timestamp() "
            "    AND (EXISTS ("
            "      SELECT 1 FROM agent_impersonation_messages relay_read "
            "      WHERE relay_read.lease_id = lease.id AND relay_read.inbound_id = ib.id"
            "    ) OR EXISTS ("
            "      SELECT 1 FROM agent_impersonation_entries transcribed "
            "      WHERE transcribed.lease_id = lease.id "
            "        AND transcribed.kind = 'message' "
            "        AND transcribed.event_key = 'inbound:' || ib.id::text"
            "    ))"
            ") "
            "ORDER BY ib.created_at ASC",
            (agent_id,),
        )
        return [InboundRow(*row) for row in cur.fetchall()]
