"""Raw Loki event-row reads (the backfill's live source; the gateway's readers use `telemetry_events`)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from base import telemetry
from base.config import settings
from base.events.contract import EventTier
from base.telemetry.loki_index_labels import LokiReadEra, LokiReadSlice, split_index_label_window
from gateway.lgtm import _loki_logql, _loki_transport

_event_id = telemetry.event_id


def _parse_line(line: str, ts_ns: int) -> dict[str, Any] | None:
    """Parse one Loki line back to the EventRow-shaped dict (id synthesized)."""
    try:
        obj: dict[str, Any] = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    payload = obj.get("attributes")
    ts_raw = obj.get("ts")
    try:
        ts = datetime.fromisoformat(ts_raw) if ts_raw else datetime.fromtimestamp(ts_ns / 1e9, UTC)
    except ValueError:
        ts = datetime.fromtimestamp(ts_ns / 1e9, UTC)
    return {
        "id": _event_id(line, ts_ns),
        "line_sha256": sha256(line.encode()).hexdigest(),
        "ts": ts,
        "trace_id": obj.get("trace_id"),
        "span_id": obj.get("span_id"),
        "agent_id": obj.get("agent_id"),
        "machine": obj.get("machine") or "",
        "process": obj.get("process") or "",
        "category": obj.get("category") or "",
        "event_name": obj.get("event_name") or "",
        "level": (obj.get("level") or "").lower(),
        "source": obj.get("source") or "",
        "target_agent_id": obj.get("target_agent_id"),
        "attributes": payload if isinstance(payload, dict) else {},
    }


def query_events(
    *,
    agent_id: int | None = None,
    exclude_agent_ids: list[int] | None = None,
    service_only: bool = False,
    event_names: list[str] | None = None,
    level_min: str | None = None,
    level: str | None = None,
    grep: str | None = None,
    categories: list[str] | None = None,
    tiers: list[EventTier] | None = None,
    cluster: str | None = None,
    machine: str | None = None,
    trace_id: str | None = None,
    attribute_filters: dict[str, str] | None = None,
    archive: bool = False,
    from_: datetime | None = None,
    to: datetime | None = None,
    # Internal read sizing (task #3696 exception inventory): the HTTP-layer
    # default lives in display.events_default_limit; this bounds an
    # omitted-argument read for direct callers.
    limit: int = 100,
    offset: int = 0,
    direction: str = "backward",
    timeout_s: float | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Slice of the event stream from Loki, newest-first by default.

    Returns (rows, has_more). ``offset`` pages in memory (Loki has no
    offset); the fetch is ``limit + offset + 1`` rows so ``has_more`` is
    exact. ``from_``/``to`` bound the window; ``from_`` defaults to
    now - 24h (same lower-bound contract as the old PG API). ``direction``
    is ``"backward"`` (newest first) or ``"forward"`` (oldest first — the
    aggregate path uses it for per-agent first-event timestamps). ``timeout_s``
    overrides the shared client's default for this request only.

    With ``archive=True`` the rows come from the task #1281 archive stream
    (all pre-cutover events, with no indexed labels). Callers bound
    ``from_``/``to`` to the archive's span
    (ARCHIVE_FLOOR_AT..ARCHIVE_FREEZE_AT) to stay under Loki's 90d
    max_query_length.
    """
    _loki_transport._read_gate()
    window = _loki_logql._window(from_, to)
    if window is None:
        return [], False
    url = settings.observability.telemetry_loki_url.rstrip("/") + "/loki/api/v1/query_range"

    if archive:
        # The archive stream is one era (no index-label cutover inside it).
        slices = (LokiReadSlice(LokiReadEra.LEGACY, window[0], window[1]),)
    else:
        slices = split_index_label_window(*window)
    raw: list[tuple[int, str]] = []
    for slice_ in slices:
        logql = _loki_logql._build_logql(
            era=slice_.era,
            archive=archive,
            agent_id=agent_id,
            exclude_agent_ids=exclude_agent_ids,
            service_only=service_only,
            event_names=event_names,
            level_min=level_min,
            level=level,
            grep=grep,
            categories=categories,
            tiers=tiers,
            cluster=cluster,
            machine=machine,
            trace_id=trace_id,
            attribute_filters=attribute_filters,
            drop_json_errors=tiers is not None,
        )
        params = {
            "query": logql,
            "limit": limit + offset + 1,  # +1 lookahead for has_more
            "direction": direction,
            "start": int(slice_.start.timestamp() * 1e9),
            "end": int(slice_.end.timestamp() * 1e9),
        }
        payload = _loki_transport._get_json(
            url, params, endpoint="query_range", timeout_s=timeout_s
        )
        for stream in payload.get("data", {}).get("result", []):
            for ts_ns, line in stream.get("values", []):
                raw.append((int(ts_ns), line))
    # query_range groups by stream; each stream is already direction-sorted,
    # but cross-stream ordering needs one merge pass.
    raw.sort(key=lambda pair: pair[0], reverse=(direction == "backward"))

    # Separate Loki streams can repeat a line at the same timestamp.
    seen: set[tuple[int, str]] = set()
    rows: list[dict[str, Any]] = []
    for ts_ns, line in raw:
        if (ts_ns, line) in seen:
            continue
        seen.add((ts_ns, line))
        parsed = _parse_line(line, ts_ns)
        if parsed is not None:
            rows.append(parsed)
            if len(rows) >= limit + offset + 1:
                break

    has_more = len(rows) > offset + limit
    return rows[offset : offset + limit], has_more
