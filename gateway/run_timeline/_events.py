"""Complete timeline event reads: every event of an agent's window, paged oldest first."""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from base.db import connect
from base.events.contract import EVENTS
from gateway.events import audit_rows, telemetry_rows

# Start reads at the existing timeline page size. This is a transport page size,
# never a display limit.
_PAGE_SIZE = 1_000

_Reader = Callable[..., tuple[list[dict[str, Any]], bool]]


def _is_audit(event_name: str) -> bool:
    spec = EVENTS.get(event_name)
    return spec is not None and spec.category == "audit"


def _query_pages(
    reader: _Reader, agent_id: int, from_: datetime, to: datetime, event_names: list[str]
) -> list[dict[str, object]]:
    """The rows of an inclusive window, oldest first, one transport page at a time."""
    events: list[dict[str, object]] = []
    offset = 0
    with connect(autocommit=True) as conn:
        while True:
            page, has_more = reader(
                conn,
                agent_id=agent_id,
                event_names=event_names,
                from_=from_,
                to=to,
                limit=_PAGE_SIZE,
                offset=offset,
                direction="forward",
            )
            events.extend(page)
            if not has_more:
                return events
            offset += _PAGE_SIZE


def query_all_events(
    agent_id: int, from_: datetime, to: datetime, *, event_names: tuple[str, ...]
) -> list[dict[str, object]]:
    """Audit facts come from `audit_events`, the rest from `telemetry_events`; the rows
    are returned together, in no particular order."""
    audit_names = [name for name in event_names if _is_audit(name)]
    other_names = [name for name in event_names if name not in audit_names]
    events: list[dict[str, object]] = []
    if audit_names:
        events += _query_pages(audit_rows.query_events, agent_id, from_, to, audit_names)
    if other_names:
        events += _query_pages(telemetry_rows.query_events, agent_id, from_, to, other_names)
    return events
