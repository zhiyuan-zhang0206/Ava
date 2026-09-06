"""Boundary and selector contract for promoted Loki event labels."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from shared import loki_index_labels as labels


def test_selector_is_legacy_safe_and_indexed_selective() -> None:
    assert (
        labels.event_stream_selector(
            era=labels.LokiReadEra.LEGACY,
            agent_id=405,
            event_names=["spawn"],
        )
        == '{service_name="unknown_service", stream!="archive"}'
    )
    assert (
        labels.event_stream_selector(
            era=labels.LokiReadEra.INDEXED,
            agent_id=405,
            event_names=["spawn", 'say "hi"'],
        )
        == '{service_name="unknown_service", stream!="archive", agent_id="405", event_name=~"spawn|say \\"hi\\""}'
    )


def test_indexed_selector_preserves_existing_regex_event_filters() -> None:
    assert (
        labels.event_stream_selector(
            era=labels.LokiReadEra.INDEXED,
            agent_id=405,
            event_names=["^exec\\(.*"],
        )
        == '{service_name="unknown_service", stream!="archive", agent_id="405", event_name=~"^exec\\\\(.*"}'
    )


def test_selector_flags_are_explicit_and_every_variant_excludes_archive() -> None:
    base = '{service_name="unknown_service", stream!="archive"}'
    for era in labels.LokiReadEra:
        for legacy_unlabeled in (False, True):
            for indexed_labeled in (False, True):
                selector = labels.event_stream_selector(
                    era=era,
                    agent_id=405,
                    event_names=["resurrect"],
                    legacy_unlabeled=legacy_unlabeled,
                    indexed_labeled=indexed_labeled,
                )
                assert 'stream!="archive"' in selector
                if era is labels.LokiReadEra.LEGACY:
                    expected = f'{base[:-1]}, event_name=""}}' if legacy_unlabeled else base
                    assert selector == expected
                else:
                    labeled = ', event_name!=""' if indexed_labeled else ""
                    assert (
                        selector
                        == f'{base[:-1]}{labeled}, agent_id="405", event_name="resurrect"}}'
                    )


def test_ledger_gap_plan_covers_retained_and_final_ledger_days() -> None:
    floor = datetime(2026, 8, 10, 12, tzinfo=UTC)
    retained = labels.ledger_gap_plan(date(2026, 8, 11), floor)
    assert retained == labels.LedgerGapPlan(
        gap_live=True,
        day_lt=date(2026, 8, 11),
        tail_from=datetime(2026, 8, 11, tzinfo=UTC),
    )

    final = labels.ledger_gap_plan(date(2026, 8, 9), floor)
    assert final == labels.LedgerGapPlan(gap_live=False, day_lt=None, tail_from=floor)

    no_ledger = labels.ledger_gap_plan(None, floor)
    assert no_ledger == labels.LedgerGapPlan(gap_live=False, day_lt=None, tail_from=floor)


def test_ledger_gap_plan_includes_the_floor_boundary_and_clamps_watermark() -> None:
    boundary = datetime(2026, 8, 10, tzinfo=UTC)
    assert labels.ledger_gap_plan(date(2026, 8, 10), boundary) == labels.LedgerGapPlan(
        gap_live=True,
        day_lt=date(2026, 8, 10),
        tail_from=boundary,
    )
    floor = datetime(2026, 8, 10, 18, tzinfo=UTC)
    assert labels.ledger_gap_plan(date(2026, 8, 9), floor).tail_from == floor
    # retention_floor derives from the retention constant: the floor is the
    # requested instant minus EVENT_STREAM_RETENTION (84h today — 08-17 00:00
    # minus 84h lands at 08-13 12:00, so a fixed 7d-boundary fixture would
    # silently break if the horizon changes again). Assert the constant-derived
    # value instead.
    source_now = datetime(2026, 8, 17, tzinfo=UTC)
    assert labels.retention_floor(source_now) == source_now - labels.EVENT_STREAM_RETENTION


def test_split_before_after_and_straddle_cutover() -> None:
    cutover = labels.INDEX_LABEL_CUTOVER_AT
    before = labels.split_index_label_window(
        cutover - timedelta(hours=2),
        cutover - timedelta(hours=1),
        now=cutover,
    )
    after = labels.split_index_label_window(
        cutover + timedelta(hours=1),
        cutover + timedelta(hours=2),
        now=cutover,
    )
    straddle = labels.split_index_label_window(
        cutover - timedelta(hours=1),
        cutover + timedelta(hours=1),
        now=cutover,
    )

    assert [slice_.era for slice_ in before] == [labels.LokiReadEra.LEGACY]
    assert [slice_.era for slice_ in after] == [labels.LokiReadEra.INDEXED]
    assert [(slice_.era, slice_.start, slice_.end) for slice_ in straddle] == [
        (labels.LokiReadEra.LEGACY, cutover - timedelta(hours=1), cutover),
        (labels.LokiReadEra.INDEXED, cutover, cutover + timedelta(hours=1)),
    ]


def test_expired_legacy_reads_are_replaced_by_one_indexed_slice() -> None:
    cutover = labels.INDEX_LABEL_CUTOVER_AT
    slices = labels.split_index_label_window(
        cutover - timedelta(days=1),
        cutover + timedelta(days=1),
        now=labels.LEGACY_READ_EXPIRES_AT,
    )
    assert slices == (
        labels.LokiReadSlice(
            labels.LokiReadEra.INDEXED,
            cutover - timedelta(days=1),
            cutover + timedelta(days=1),
        ),
    )
