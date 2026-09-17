"""Replay existing SDK/API events into session history; collection remains upstream."""

from datetime import datetime
from typing import Any

import httpx
from psycopg.types.json import Jsonb

from ava._gateway_transport import _get
from shared._impersonation_store import lock_lease
from shared.db_transaction import write_transaction
from shared.impersonation_events import consume_events
from shared.impersonation_history import event_belongs_to_agent


def consume_recorded_events(session: dict[str, Any], *, page_budget: int = 4) -> None:
    """Consume a bounded portion of the fixed activation/end interval.

    A completed sweep restarts from its beginning on the next maintenance pass,
    including after native handoff. Late indexing and shifted offset pages are
    repaired by replay, never by assuming an empty page means delivery completed.
    Only the upstream collector's explicit manifest closes this pending work.
    """
    if session["activated_at"] is None or session["events_completed_at"] is not None:
        return
    with write_transaction() as conn:
        # Each pass has a bounded page budget and a durable continuation.
        # The locked snapshot is refreshed even if this session dict is stale.
        lease = lock_lease(conn, str(session["id"]))
        if lease["events_completed_at"] is not None:
            return
        cursor: list[dict[str, Any]] = lease["events_cursor"] or [
            {
                "kind": kind,
                "start": lease["activated_at"].isoformat(),
                "end": lease["ended_at"].isoformat(),
                "offset": 0,
            }
            for kind in ("sdk", "audit")
        ]
    # HTTP calls and event writes run outside the lease lock. Native and background
    # readers may overlap; stable IDs deduplicate them and replay repairs cursors.
    for _ in range(page_budget):
        if not cursor:
            break
        window = cursor[0]
        filters = (
            {"event_name": "sdk_call", "agent_id": session["agent_id"]}
            if window["kind"] == "sdk"
            else {"category": "audit"}
        )
        response: httpx.Response = _get(
            "/api/events",
            params={
                **filters,
                "from": window["start"],
                "to": window["end"],
                # 1000 = the events API's page ceiling (le); the durable
                # cursor resumes across steps (task #3696 exception inventory).
                "limit": 1000,
                "offset": window["offset"],
            },
        )
        response.raise_for_status()
        page: dict[str, Any] = response.json()
        consume_events(
            session["agent_id"],
            session["session_id"],
            (
                event
                for event in page["items"]
                if event_belongs_to_agent(event, session["agent_id"])
            ),
        )
        if not page["meta"]["has_more"]:
            cursor.pop(0)
        elif window["offset"] < 10_000:
            # Walk to the API's offset ceiling (le=10_000); past it the window
            # is bisected below.
            window["offset"] += 1000
        else:
            start, end = (
                datetime.fromisoformat(window["start"]),
                datetime.fromisoformat(window["end"]),
            )
            midpoint = start + (end - start) / 2
            if midpoint in (start, end):
                raise RuntimeError("Too many SDK events at one timestamp for the event API")
            cursor[:1] = [
                {"kind": window["kind"], "start": a.isoformat(), "end": b.isoformat(), "offset": 0}
                for a, b in ((start, midpoint), (midpoint, end))
            ]
    with write_transaction() as conn:
        lock_lease(conn, str(session["id"]))
        conn.execute(
            "UPDATE agent_impersonations SET events_cursor=%s,"
            "events_next_read_at=clock_timestamp()+interval '1 minute' "
            "WHERE id=%s AND events_completed_at IS NULL",
            (Jsonb(cursor) if cursor else None, session["id"]),
        )
