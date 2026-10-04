"""Coverage-note routing for the inspector metrics read model (task #3869).

Expected limits (historical coverage, legacy archive precision) log at DEBUG
and emit no event. An unexpected limit (``missing_turn_durations`` on a window
inside the collection era) emits the ``inspect_metrics_coverage_gap`` WARNING
event on every read that still shows it. The note is best-effort: it must
never raise into the statistics read path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gateway.inspect import _metrics_health as imh
from gateway.inspect.schemas import InspectMetricsMetadata, MetricEvidence

T0 = datetime(2026, 9, 17, 10, 0, tzinfo=UTC)
T1 = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)  # collection started
T2 = datetime(2026, 9, 17, 13, 0, tzinfo=UTC)


def _ev(
    availability: str = "observed",
    reason: str | None = None,
    retained: tuple[str, ...] = (),
) -> MetricEvidence:
    return MetricEvidence(
        availability=availability,  # pyright: ignore[reportArgumentType]
        sources=["observations"],
        reason=reason,  # pyright: ignore[reportArgumentType]
        retained_unapplied_sources=list(retained),
    )


def _md(
    *,
    cost: MetricEvidence | None = None,
    turns: MetricEvidence | None = None,
    activity: MetricEvidence | None = None,
    lifecycle: MetricEvidence | None = None,
    window_start: datetime | None = T0,
) -> InspectMetricsMetadata:
    return InspectMetricsMetadata(
        window_start=window_start,
        window_end=T2,
        sampled_at=T2,
        collection_started_at=T1,
        last_observed_at=T2,
        cost=cost or _ev(),
        turns=turns or _ev(),
        activity=activity or _ev(),
        lifecycle=lifecycle or _ev(),
    )


def _gap_events(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if r["extra"].get("event") == "inspect_metrics_coverage_gap"]


def test_expected_limit_logs_at_debug_and_emits_no_event(
    loguru_records: list[dict[str, Any]],
) -> None:
    md = _md(cost=_ev("partial", "historical_coverage_unknown"))
    imh.note_inspect_metrics_coverage(42, md, spawned_at=T0)  # window reaches before collection
    limits = [r for r in loguru_records if "coverage limit" in str(r["message"])]
    assert len(limits) == 1
    assert limits[0]["level"].name == "DEBUG"
    assert limits[0]["extra"]["condition"] == "historical_coverage_unknown"
    assert limits[0]["extra"]["family"] == "cost"
    assert _gap_events(loguru_records) == []


@pytest.mark.parametrize(
    ("evidence", "condition"),
    [
        (_ev("partial", "historical_coverage_unknown"), "historical_coverage_unknown"),
        (_ev("partial", "missing_turn_durations"), "missing_turn_durations"),
        (_ev("partial", "archive_precision_unattributed"), "archive_precision_unattributed"),
        (
            _ev("partial", None, retained=("historical_archive_distribution",)),
            "archive_precision_unattributed",
        ),
        # missing durations stays primary when the retained-source caveat rides along
        (
            _ev("partial", "missing_turn_durations", retained=("historical_archive_distribution",)),
            "missing_turn_durations",
        ),
    ],
)
def test_condition_classification(evidence: MetricEvidence, condition: str) -> None:
    assert imh._conditions(_md(turns=evidence)) == [("turns", condition, evidence.availability)]


def test_unexpected_missing_durations_emits_the_gap_event_on_every_read(
    loguru_records: list[dict[str, Any]],
) -> None:
    # Window starts inside the collection era -> the gap is live (unexpected).
    bad = _md(turns=_ev("partial", "missing_turn_durations"), window_start=T1)
    imh.note_inspect_metrics_coverage(42, bad, spawned_at=T0)
    imh.note_inspect_metrics_coverage(42, bad, spawned_at=T0)  # still true: emitted again
    events = _gap_events(loguru_records)
    assert len(events) == 2
    assert events[0]["level"].name == "WARNING"
    extra = events[0]["extra"]
    assert (extra["agent_id"], extra["family"], extra["condition"]) == (
        42,
        "turns",
        "missing_turn_durations",
    )
    assert extra["availability"] == "partial"


def test_missing_durations_in_a_historical_window_is_not_a_gap(
    loguru_records: list[dict[str, Any]],
) -> None:
    md = _md(turns=_ev("partial", "missing_turn_durations"), window_start=T0)
    imh.note_inspect_metrics_coverage(42, md, spawned_at=T0)
    assert _gap_events(loguru_records) == []


def test_a_clean_read_emits_nothing(loguru_records: list[dict[str, Any]]) -> None:
    imh.note_inspect_metrics_coverage(42, _md(window_start=T1), spawned_at=T0)
    assert _gap_events(loguru_records) == []
    assert not [r for r in loguru_records if "coverage limit" in str(r["message"])]


def test_note_never_raises_into_the_read_path(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    def boom(*_a: object, **_k: object) -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(imh, "_historical", boom)
    imh.note_inspect_metrics_coverage(42, _md(), spawned_at=T0)
    assert any("note failed" in str(r["message"]) for r in loguru_records)
