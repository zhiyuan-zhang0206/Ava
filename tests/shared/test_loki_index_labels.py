"""Boundary and selector contract for promoted Loki event labels."""

from __future__ import annotations

from datetime import timedelta

from shared import loki_index_labels as labels


def test_selector_is_legacy_safe_and_indexed_selective() -> None:
    assert (
        labels.event_stream_selector(
            era=labels.LokiReadEra.LEGACY,
            agent_id=405,
            event_names=["spawn"],
        )
        == '{service_name="unknown_service"}'
    )
    assert (
        labels.event_stream_selector(
            era=labels.LokiReadEra.INDEXED,
            agent_id=405,
            event_names=["spawn", 'say "hi"'],
        )
        == '{service_name="unknown_service", agent_id="405", event_name=~"spawn|say \\"hi\\""}'
    )


def test_indexed_selector_preserves_existing_regex_event_filters() -> None:
    assert (
        labels.event_stream_selector(
            era=labels.LokiReadEra.INDEXED,
            agent_id=405,
            event_names=["^exec\\(.*"],
        )
        == '{service_name="unknown_service", agent_id="405", event_name=~"^exec\\\\(.*"}'
    )


def test_legacy_stream_only_selector_excludes_promoted_streams() -> None:
    selector = labels.event_stream_selector(
        era=labels.LokiReadEra.LEGACY,
        agent_id=None,
        event_names=["resurrect"],
        legacy_streams_only=True,
    )
    assert selector == '{service_name="unknown_service", event_name=""}'
    assert "stream" not in selector  # Archive streams are deliberately disjoint.


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
