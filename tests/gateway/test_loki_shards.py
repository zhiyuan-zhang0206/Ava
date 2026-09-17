"""Status sharding admits no work for empty windows and preserves exact edges."""

from datetime import UTC, datetime, timedelta

import pytest

from gateway.routers._loki_shards import query_loki_shards, split_loki_window


@pytest.mark.parametrize("offset", [timedelta(), timedelta(seconds=-1)])
def test_empty_or_reversed_window_does_not_start_workers(offset: timedelta) -> None:
    start = datetime(2026, 9, 17, tzinfo=UTC)

    def query(_start: datetime, _end: datetime) -> None:
        pytest.fail("empty windows must not query Loki")

    assert query_loki_shards(start, start + offset, query) == []


def test_shards_cover_partial_edges_without_gaps_or_overlap() -> None:
    start = datetime(2026, 9, 17, 1, 30, tzinfo=UTC)
    end = start + timedelta(hours=7)
    spans = split_loki_window(start, end)
    assert spans == [
        (start, datetime(2026, 9, 17, 3, tzinfo=UTC)),
        (datetime(2026, 9, 17, 3, tzinfo=UTC), datetime(2026, 9, 17, 6, tzinfo=UTC)),
        (datetime(2026, 9, 17, 6, tzinfo=UTC), end),
    ]
