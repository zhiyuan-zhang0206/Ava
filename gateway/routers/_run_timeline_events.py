"""Complete timeline event reads without repeatedly fetching the window prefix."""

from datetime import datetime

from gateway import loki_events

# Start reads at the existing timeline page size. Dense windows split by time;
# a repeated prefix resumes offsets within that smaller window. This is a
# transport page size, never a display limit.
_PAGE_SIZE = 1_000


def query_all_events(
    agent_id: int, from_: datetime, to: datetime, *, event_names: tuple[str, ...]
) -> list[dict[str, object]]:
    """Read chronological, inclusive windows, deduplicating shared boundaries.

    Splitting uses query timestamps, not the JSON event timestamp: Loki's
    ingestion clock can differ from the timestamp inside a log line. Only an
    indivisible window or a split repeating its parent's first page uses
    offsets. This keeps concentrated bursts from repeatedly bisecting without
    reducing the read, while retaining every equal-time event.
    """
    windows: list[tuple[datetime, datetime, frozenset[object]]] = [(from_, to, frozenset())]
    events: list[dict[str, object]] = []
    seen: set[object] = set()
    while windows:
        start, stop, parent_page = windows.pop()
        offset = 0
        while True:
            page, has_more = loki_events.query_events(
                agent_id=agent_id,
                event_names=list(event_names),
                from_=start,
                to=stop,
                limit=_PAGE_SIZE,
                offset=offset,
                direction="forward",
            )
            middle = start + (stop - start) / 2
            page_ids = frozenset(event["id"] for event in page)
            if has_more and offset == 0 and page_ids != parent_page and start < middle < stop:
                windows.extend([(middle, stop, page_ids), (start, middle, page_ids)])
                break
            for event in page:
                if event["id"] not in seen:
                    seen.add(event["id"])
                    events.append(event)
            if not has_more:
                break
            offset += _PAGE_SIZE
    return events
