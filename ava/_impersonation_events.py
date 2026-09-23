"""Replay existing SDK/API events into session history; collection remains upstream."""

from datetime import datetime, timedelta
from typing import Any

import httpx
from psycopg.types.json import Jsonb

from ava._gateway_transport import _get
from shared._impersonation_store import lock_lease
from shared.agents.impersonation_manifest import (
    certify,
    frozen_items,
    is_protocol_v1,
    set_pending_reason,
)
from shared.config import settings
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
        protocol_v1 = is_protocol_v1(lease)
        if _awaits_manifest_freeze(lease, protocol_v1=protocol_v1):
            return
        start = _replay_start(lease, protocol_v1=protocol_v1)
        cursor: list[dict[str, Any]] = lease["events_cursor"] or [
            {
                "kind": kind,
                "start": start.isoformat(),
                "end": (
                    lease["ended_at"]
                    + timedelta(
                        seconds=settings.general.impersonation_event_clock_skew_guard_seconds
                    )
                ).isoformat(),
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
        filters = _reader_filters(session, window["kind"])
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
                **_session_filter(session, protocol_v1=protocol_v1),
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
    _certify_if_complete(session, lease, cursor, protocol_v1=protocol_v1)


def _reader_filters(session: dict[str, Any], kind: str) -> dict[str, Any]:
    if kind == "sdk":
        return {"event_name": "sdk_call", "agent_id": session["agent_id"]}
    return {"category": "audit"}


def _awaits_manifest_freeze(lease: dict[str, Any], *, protocol_v1: bool) -> bool:
    return protocol_v1 and lease["manifest_frozen_at"] is None


def _replay_start(lease: dict[str, Any], *, protocol_v1: bool) -> Any:
    return lease["manifest_envelope_floor_at"] if protocol_v1 else lease["activated_at"]


def _session_filter(session: dict[str, Any], *, protocol_v1: bool) -> dict[str, str]:
    if not protocol_v1:
        return {}
    return {"impersonation_session": f"{session['agent_id']}:{session['session_id']}"}


def _certify_if_complete(
    session: dict[str, Any],
    lease: dict[str, Any],
    cursor: list[dict[str, Any]],
    *,
    protocol_v1: bool,
) -> None:
    if not cursor and protocol_v1 and _indexed_manifest_matches(lease):
        certify(str(session["id"]), machine=session["machine"])


def _indexed_manifest_matches(lease: dict[str, Any]) -> bool:
    """Compare the entire tagged Loki envelope with the frozen union.

    Loki is necessarily outside the following certification transaction.  This
    check is therefore the final external observation: a missing expected row
    stays retryable, while an extra or byte-different one is an upstream
    manifest breach.  The SQL function immediately afterwards independently
    compares the same frozen union with the durable consumed entries.
    """
    lease_id = str(lease["id"])
    with write_transaction() as conn:
        expected = frozen_items(conn, lease_id)
    actual: dict[str, tuple[str, str]] = {}
    session = f"{lease['agent_id']}:{lease['session_id']}"
    max_items = settings.general.impersonation_event_manifest_max_items
    for kind, filters in (
        ("sdk_call", {"event_name": "sdk_call", "agent_id": lease["agent_id"]}),
        ("api_event", {"category": "audit"}),
    ):
        for offset in range(0, max_items + 1, 1000):
            response: httpx.Response = _get(
                "/api/events",
                params={
                    **filters,
                    "from": lease["manifest_envelope_floor_at"].isoformat(),
                    "to": (
                        lease["ended_at"]
                        + timedelta(
                            seconds=settings.general.impersonation_event_clock_skew_guard_seconds
                        )
                    ).isoformat(),
                    "impersonation_session": session,
                    "limit": 1000,
                    "offset": offset,
                },
            )
            response.raise_for_status()
            page: dict[str, Any] = response.json()
            for event in page["items"]:
                event_kind = "sdk_call" if event["event_name"] == "sdk_call" else "api_event"
                if event_kind != kind:
                    raise RuntimeError("Tagged event reader returned an unexpected event family")
                key = f"event:{event['id']}"
                item = (event["line_sha256"], event_kind)
                previous = actual.setdefault(key, item)
                if previous != item:
                    set_pending_reason(lease_id, "manifest_mismatch")
                    return False
            if not page["meta"]["has_more"]:
                break
        else:
            set_pending_reason(lease_id, "manifest_mismatch")
            return False
    if actual == expected:
        return True
    reason = "awaiting_indexed_ids" if expected.keys() - actual.keys() else "manifest_mismatch"
    set_pending_reason(lease_id, reason)
    return False
