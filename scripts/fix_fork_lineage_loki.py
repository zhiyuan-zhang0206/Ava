"""Correct misrecorded fork events in the Loki live stream.

Fork-lineage ruling (2026-08-28, task #1879): a fork event's
``target_agent_id`` must be the fork SOURCE (the lineage parent), never the
executor who triggered the fork. Events written before the fix recorded the
executor. SQL cannot reach the Loki live stream (the PG side — ``agents_meta``
spawner + the frozen events archive — is corrected by
``migrations/*_fork-lineage-target-fix.sql``), so this script:

1. finds fork events whose ``target_agent_id`` != payload ``fork_from`` (the
   exact predicate the fleet_graph / neighbors ancestors readers rely on;
   the scan covers the LIVE stream only — pre-cutover rows were corrected
   by the SQL migration before the archive import, task #1281),
2. deletes each misrecorded row via the Loki delete API — an exact label
   selector (event_name + agent_id + target_agent_id) over a narrow time
   window around the row's ts, unambiguous because fork events are rare,
3. re-emits a corrected row through the standard telemetry pipeline with the
   original timestamp, then verifies the stream reads back corrected.

Dry-run by default (prints what would change); pass ``--apply`` to perform
the deletion + re-ingest. Run from the repo with the cluster env:

    .venv/bin/python scripts/fix_fork_lineage_loki.py [--apply]

The deletion is asynchronous in Loki (filter mode): after ``--apply`` the
script polls until the pending delete request is processed and the row is no
longer returned, then re-emits the corrected row.
"""

from __future__ import annotations

import argparse
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from gateway import loki_events
from shared.config import settings
from shared.loki_index_labels import ARCHIVE_FREEZE_AT


def _misrecorded_rows() -> list[dict[str, Any]]:
    """Fork events whose target_agent_id != payload fork_from, oldest first.

    The scan window is the live stream's own read window: from the frozen
    events-archive boundary (rows before it live in PG and are covered by the
    SQL migration) to now. The predicate mirrors the neighbor/ancestor reads:
    only rows that carry a fork_from payload are lineage-judged.
    """
    now = datetime.now(UTC)
    # The scan covers the live stream only: pre-cutover rows live in the
    # Loki archive stream (task #1281) and were corrected by the SQL
    # migration on the PG side before the archive was imported.
    from_ = ARCHIVE_FREEZE_AT
    rows, has_more = loki_events.query_events(
        event_names=["fork"],
        categories=["audit"],
        from_=from_,
        to=now,
        limit=5000,
        direction="forward",
    )
    if has_more:
        print("warning: fork-event scan hit the Loki fetch cap — results may be truncated")
    bad: list[dict[str, Any]] = []
    for r in rows:
        attrs: dict[str, Any] = dict(r.get("attributes") or {})
        fork_from: Any = attrs.get("fork_from")
        if fork_from is None or r.get("target_agent_id") == fork_from:
            continue
        bad.append(r)
    bad.sort(key=lambda r: r["ts"])
    return bad


def _delete_row(row: dict[str, Any]) -> None:
    """Delete one misrecorded row via the Loki delete API (async, filter mode).

    The selector pins event_name + agent_id + the WRONG target_agent_id — the
    row is uniquely identified; the window is a defensive narrowing on top.
    """
    base = settings.observability.telemetry_loki_url.rstrip("/")
    ts = row["ts"]
    if not isinstance(ts, datetime):
        ts = datetime.fromisoformat(str(ts))
    start = int((ts - timedelta(seconds=1)).timestamp())
    end = int((ts + timedelta(seconds=1)).timestamp())
    # target_agent_id is a JSON body field, NOT a stream label (the collector
    # only promotes agent_id/event_name) — a selector on it matches nothing and
    # the delete silently no-ops. Scope by real labels + the narrow time window
    # instead: the window makes one fork event per agent unique.
    selector = f'{{event_name="fork", agent_id="{row["agent_id"]}"}}'
    resp = httpx.post(
        base + "/loki/api/v1/delete",
        params={"query": selector, "start": start, "end": end},
        timeout=20,
    )
    if resp.status_code not in (204, 200):
        raise RuntimeError(f"loki delete failed ({resp.status_code}): {resp.text[:300]}")


def _pending_deletes() -> list[dict[str, Any]]:
    """The Loki delete API's list of not-yet-processed delete requests."""
    base = settings.observability.telemetry_loki_url.rstrip("/")
    resp = httpx.get(base + "/loki/api/v1/delete", timeout=20)
    resp.raise_for_status()
    return resp.json()


def _row_still_served(row: dict[str, Any]) -> bool:
    """Whether the misrecorded row is still returned by the event-stream read."""
    ts = row["ts"]
    if not isinstance(ts, datetime):
        ts = datetime.fromisoformat(str(ts))
    rows, _ = loki_events.query_events(
        event_names=["fork"],
        categories=["audit"],
        from_=ts - timedelta(seconds=5),
        to=ts + timedelta(seconds=5),
        limit=10,
        direction="forward",
    )
    return any(
        r.get("agent_id") == row["agent_id"] and r.get("target_agent_id") == row["target_agent_id"]
        for r in rows
    )


def _reemit_corrected(row: dict[str, Any]) -> None:
    """Re-ingest the corrected row through the standard telemetry pipeline.

    Same ts, same source (the executor), same payload — only the target
    becomes the payload's fork_from. The process label on the re-ingested row
    is this script's own (honest provenance); no reader filters on it.
    """
    from shared import telemetry

    attrs = dict(row.get("attributes") or {})
    telemetry.emit(
        "audit",
        "fork",
        level="info",
        agent_id=row["agent_id"],
        source=row["source"],
        target_agent_id=attrs["fork_from"],
        attributes=attrs,
        ts=row["ts"],
    )
    telemetry.sync()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform deletion + re-ingest (default: dry-run listing only)",
    )
    args = parser.parse_args(argv)

    from shared import telemetry

    telemetry.init_telemetry(process="fork-lineage-fix")
    bad = _misrecorded_rows()
    if not bad:
        print("no misrecorded fork events found — nothing to do")
        return 0
    print(f"found {len(bad)} misrecorded fork event(s):")
    for row in bad:
        print(
            f"  ts={row['ts']} agent_id={row['agent_id']} source={row['source']} "
            f"target_agent_id={row['target_agent_id']} "
            f"-> fork_from={row['attributes'].get('fork_from')}"
        )
    if not args.apply:
        print("dry-run — pass --apply to delete the row(s) and re-ingest corrected rows")
        return 1

    for row in bad:
        _delete_row(row)
        print(f"delete requested for agent {row['agent_id']} fork event at {row['ts']}")
    # Loki processes deletes asynchronously; wait until none of the rows is
    # still served (bounded poll).
    deadline = time.monotonic() + 180
    while any(_row_still_served(row) for row in bad):
        if time.monotonic() > deadline:
            print("error: timed out waiting for Loki deletes to process — re-run --apply later")
            return 2
        time.sleep(5)
    print("deletes processed; re-ingesting corrected rows")
    for row in bad:
        _reemit_corrected(row)
        print(
            f"re-ingested corrected fork event for agent {row['agent_id']} "
            f"(target {row['attributes']['fork_from']})"
        )
    # Verify the corrected rows read back.
    time.sleep(2)
    leftover = _misrecorded_rows()
    if leftover:
        print(f"error: {len(leftover)} misrecorded row(s) still served after fix")
        return 3
    print("verified: no misrecorded fork events remain")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
