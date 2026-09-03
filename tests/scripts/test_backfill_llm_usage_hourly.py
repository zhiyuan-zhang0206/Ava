"""Contract tests for the `llm_usage_hourly` archive backfill."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from scripts import backfill_llm_usage_hourly as backfill
from shared.config import settings


def _row(ts: str, **attributes: Any) -> dict[str, Any]:
    """One `llm_usage` event; token counters default to a distinguishable set."""
    return {
        "event_name": "llm_usage",
        "ts": ts,
        "attributes": {
            "in_total": 100,
            "cache_read": 10,
            "out_total": 1,
            "reasoning": 0,
            **attributes,
        },
    }


def test_hour_bucket_floors_to_the_utc_hour() -> None:
    totals = backfill.aggregate_llm_usage(
        [
            _row("2026-06-01T03:59:59.900000+00:00", model="m"),
            _row("2026-06-01T04:00:00+00:00", model="m"),
        ]
    )

    assert sorted(totals) == [
        (datetime(2026, 6, 1, 3, tzinfo=UTC), "m"),
        (datetime(2026, 6, 1, 4, tzinfo=UTC), "m"),
    ]


def test_non_utc_offset_buckets_by_the_utc_hour() -> None:
    """A row written with a non-UTC offset lands in the hour UTC sees, not its own."""
    totals = backfill.aggregate_llm_usage([_row("2026-06-01T04:30:00+08:00", model="m")])

    assert sorted(totals) == [(datetime(2026, 5, 31, 20, tzinfo=UTC), "m")]


def test_naive_timestamp_is_refused() -> None:
    with pytest.raises(ValueError, match="no timezone"):
        backfill.aggregate_llm_usage([_row("2026-06-01T04:00:00", model="m")])


def test_missing_model_groups_under_unknown() -> None:
    totals = backfill.aggregate_llm_usage(
        [
            _row("2026-06-01T04:10:00+00:00"),
            _row("2026-06-01T04:20:00+00:00", model=None),
        ]
    )

    hour = datetime(2026, 6, 1, 4, tzinfo=UTC)
    assert list(totals) == [(hour, backfill.UNKNOWN_MODEL)]
    assert totals[(hour, backfill.UNKNOWN_MODEL)].in_total == 200


def test_sums_split_by_hour_and_model() -> None:
    totals = backfill.aggregate_llm_usage(
        [
            _row("2026-06-01T04:10:00+00:00", model="a", in_total=1, reasoning=7),
            _row("2026-06-01T04:50:00+00:00", model="a", in_total=2, cache_read=3),
            _row("2026-06-01T04:50:00+00:00", model="b", in_total=4, out_total=5),
            _row("2026-06-01T05:00:00+00:00", model="a", in_total=8),
        ]
    )

    hour_four = datetime(2026, 6, 1, 4, tzinfo=UTC)
    hour_five = datetime(2026, 6, 1, 5, tzinfo=UTC)
    assert totals[(hour_four, "a")].in_total == 3
    assert totals[(hour_four, "a")].cache_read == 13
    assert totals[(hour_four, "a")].reasoning == 7
    assert totals[(hour_four, "b")].in_total == 4
    assert totals[(hour_four, "b")].out_total == 5
    assert totals[(hour_five, "a")].in_total == 8


def test_cost_before_the_peak_window_is_the_same_on_both_columns() -> None:
    """No peak window existed yet, so the hour prices identically either way."""
    totals = backfill.aggregate_llm_usage(
        [
            _row("2026-08-16T15:10:00+00:00", model="m", cost_usd=0.25),
            _row("2026-08-16T15:40:00+00:00", model="m", cost_usd=0.75),
        ]
    )

    row = totals[(datetime(2026, 8, 16, 15, tzinfo=UTC), "m")]
    assert row.cost_offpeak_usd == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    assert row.cost_peak_usd == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]


def test_cost_inside_the_peak_window_doubles_the_peak_column() -> None:
    totals = backfill.aggregate_llm_usage(
        [_row("2026-08-16T16:30:00+00:00", model="m", cost_usd=0.5)]
    )

    row = totals[(datetime(2026, 8, 16, 16, tzinfo=UTC), "m")]
    assert row.cost_offpeak_usd == pytest.approx(0.5)  # pyright: ignore[reportUnknownMemberType]
    assert row.cost_peak_usd == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]


def test_unpriced_row_contributes_tokens_at_zero_cost() -> None:
    """An absent `cost_usd` means unpriced (LlmUsage contract), never unknown."""
    totals = backfill.aggregate_llm_usage([_row("2026-06-01T04:00:00+00:00", model="m")])

    row = totals[(datetime(2026, 6, 1, 4, tzinfo=UTC), "m")]
    assert row.in_total == 100
    assert row.cost_offpeak_usd == 0.0
    assert row.cost_peak_usd == 0.0


def test_read_rows_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "extract.jsonl"
    path.write_text('{"ts": "2026-06-01T04:00:00+00:00", "attributes": {}}\n\n', encoding="utf-8")

    assert list(backfill.read_rows(path)) == [{"ts": "2026-06-01T04:00:00+00:00", "attributes": {}}]


@pytest.fixture
def _empty_llm_usage_hourly() -> Iterator[None]:
    """`llm_usage_hourly` is outside the per-test TRUNCATE list — clean it here."""
    with psycopg.connect(settings.data_plane.db_url) as conn:
        conn.execute("TRUNCATE llm_usage_hourly")
    yield
    with psycopg.connect(settings.data_plane.db_url) as conn:
        conn.execute("TRUNCATE llm_usage_hourly")


@pytest.mark.usefixtures("_empty_llm_usage_hourly")
def test_upsert_is_rerunnable_and_overwrites_derived_costs() -> None:
    """A second run over a corrected extract replaces the row, never accumulates."""
    hour = datetime(2026, 6, 1, 4, tzinfo=UTC)
    first = backfill.aggregate_llm_usage(
        [_row("2026-06-01T04:10:00+00:00", model="m", cost_usd=0.25)]
    )
    backfill.upsert_totals(first, db_url=settings.data_plane.db_url)
    backfill.upsert_totals(first, db_url=settings.data_plane.db_url)

    second = backfill.aggregate_llm_usage(
        [
            _row("2026-06-01T04:10:00+00:00", model="m", cost_usd=0.25),
            _row("2026-06-01T04:20:00+00:00", model="m", cost_usd=0.25),
        ]
    )
    backfill.upsert_totals(second, db_url=settings.data_plane.db_url)

    with psycopg.connect(settings.data_plane.db_url) as conn:
        rows = conn.execute(
            "SELECT ts_hour, model, in_total, cost_peak_usd, cost_offpeak_usd "
            "FROM llm_usage_hourly ORDER BY ts_hour, model"
        ).fetchall()

    assert rows == [(hour, "m", 200, pytest.approx(0.5), pytest.approx(0.5))]  # pyright: ignore[reportUnknownMemberType]
