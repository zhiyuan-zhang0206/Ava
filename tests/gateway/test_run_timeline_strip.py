"""Raw-context strip contract tests (P4-2, task #4023)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Literal

import pytest
from fastapi import HTTPException

from gateway.routers import run_timeline_strip as strip
from shared import checkpoint
from shared.timeline import TimelineItem

_ItemKind = Literal[
    "inbound_chat",
    "inbound_compact_summary",
    "inbound_compact_request",
    "attach",
    "agent_chat",
    "agent_code",
    "agent_reasoning",
    "code_output",
    "system_prompt",
    "system_marker",
]


def _item(
    item_id: str,
    kind: _ItemKind,
    payload: str = "x",
    created_at: str | None = None,
    source: str | None = None,
) -> TimelineItem:
    return TimelineItem(
        item_id=item_id,
        kind=kind,
        payload=payload,
        created_at=created_at,
        source=source,
    )


def _strip_settings(messages_max: int, text_max: int) -> SimpleNamespace:
    return SimpleNamespace(
        display=SimpleNamespace(
            run_timeline_messages_max=messages_max,
            run_timeline_message_text_max=text_max,
        )
    )


def _no_anchors(_messages: list[object]) -> bool:
    return False


def _checkpoint_messages(_agent_id: int) -> list[object]:
    return ["m"]


def _no_boundaries(_agent_id: int) -> list[str]:
    return []


def test_part_kind_mapping_is_total() -> None:
    expected = {
        "agent_reasoning": "think",
        "agent_chat": "text",
        "agent_code": "call",
        "code_output": "out",
        "system_marker": "note",
        "system_prompt": "prompt",
        "inbound_compact_summary": "compact",
        "inbound_compact_request": "compact",
        "inbound_chat": "inbound",
        "attach": "attach",
    }
    for item_kind, part_kind in expected.items():
        assert strip._message_part_kind(item_kind) == part_kind
    with pytest.raises(ValueError):
        strip._message_part_kind("brand-new-kind")


def test_message_kind_mapping_is_total() -> None:
    assert strip._message_kind({"agent_reasoning"}) == "ai"
    assert strip._message_kind({"agent_chat", "agent_code"}) == "ai"
    assert strip._message_kind({"code_output"}) == "exec"
    assert strip._message_kind({"system_marker"}) == "note"
    assert strip._message_kind({"system_prompt"}) == "prompt"
    assert strip._message_kind({"inbound_chat"}) == "inbound"
    assert strip._message_kind({"attach"}) == "attach"
    assert strip._message_kind({"inbound_compact_summary"}) == "compact"
    assert strip._message_kind({"inbound_compact_request"}) == "compact"
    with pytest.raises(ValueError):
        strip._message_kind({"agent_chat", "code_output"})


def test_group_strip_items_keys_current_and_history_segments() -> None:
    groups = dict(
        strip._group_strip_items(
            [
                _item("4.0", "agent_reasoning"),
                _item("4.1", "agent_chat"),
                _item("s1.ck-a.9.0", "code_output"),
            ]
        )
    )
    assert list(groups) == ["c.4", "s1.ck-a.9"]
    assert [item.item_id for item in groups["c.4"]] == ["4.0", "4.1"]
    assert [item.item_id for item in groups["s1.ck-a.9"]] == ["s1.ck-a.9.0"]


def test_group_strip_items_rejects_malformed_ids() -> None:
    with pytest.raises(ValueError):
        strip._group_strip_items([_item("nodot", "agent_chat")])


def test_message_from_group_merges_adjacent_parts_and_totals_chars() -> None:
    message = strip._message_from_group(
        "c.7",
        [
            _item("7.0", "agent_reasoning", "think!"),
            _item("7.1", "agent_reasoning", "more"),
            _item("7.2", "agent_code", "run()"),
        ],
    )
    assert message.kind == "ai"
    assert message.idx == 7
    assert message.ts is None
    assert message.chars == 15
    assert [(part.kind, part.chars) for part in message.parts] == [("think", 10), ("call", 5)]


def test_message_from_group_carries_inbound_source() -> None:
    message = strip._message_from_group(
        "c.2",
        [
            _item(
                "2.0",
                "inbound_chat",
                "hi",
                created_at="2026-09-01T00:05:00+00:00",
                source="agent:42",
            )
        ],
    )
    assert message.kind == "inbound"
    assert message.source == "agent:42"
    assert message.ts == datetime(2026, 9, 1, 0, 5, tzinfo=UTC)


def test_window_uses_current_segment_when_it_covers_the_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window_start = datetime(
        2026, 9, 1, 0, 0, tzinfo=UTC
    )  # time-bomb-ok: explicit fixture window input
    items = [
        _item("1.0", "agent_chat", "a" * 10, created_at="2026-08-31T23:00:00+00:00"),
        _item("2.0", "agent_chat", "b" * 10, created_at="2026-09-01T00:30:00+00:00"),
        _item("3.0", "agent_chat", "c" * 10, created_at="2026-09-01T02:00:00+00:00"),
    ]
    monkeypatch.setattr(strip, "settings", _strip_settings(600, 20000))
    monkeypatch.setattr(strip, "needs_chat_anchors", _no_anchors)

    def _build(
        _messages: list[object], _anchors: list[object], **_kwargs: object
    ) -> tuple[list[TimelineItem], int]:
        return items, 3

    monkeypatch.setattr(strip, "build_timeline_items", _build)
    monkeypatch.setattr(checkpoint, "load_checkpoint_messages", _checkpoint_messages)
    walked: list[int] = []

    def _boundaries(agent_id: int) -> list[str]:
        walked.append(agent_id)
        return ["ck-1", "ck-2"]

    monkeypatch.setattr(checkpoint, "list_compact_boundary_checkpoint_ids", _boundaries)

    messages, truncated = strip._strip_messages_for_window(
        7, window_start, window_start.replace(hour=1)
    )

    assert walked == []
    assert [message.key for message in messages] == ["c.2"]
    assert truncated is False


def _history_walk_fixture(
    monkeypatch: pytest.MonkeyPatch,
    segment_stamps: list[str],
) -> list[str]:
    """Five boundaries; each segment's stamped ts comes from ``segment_stamps``."""
    current = [_item("1.0", "agent_chat", "a", created_at="2026-09-01T00:50:00+00:00")]
    seen_segments: list[str] = []

    monkeypatch.setattr(strip, "settings", _strip_settings(600, 20000))
    monkeypatch.setattr(strip, "needs_chat_anchors", _no_anchors)

    def _build(
        _messages: list[object], _anchors: list[object], **kwargs: object
    ) -> tuple[list[TimelineItem], int]:
        prefix = kwargs.get("segment_prefix")
        if prefix is None:
            return current, 1
        index = len(seen_segments)
        seen_segments.append(str(prefix))
        return [
            _item(
                f"s{index + 1}.ck-{index + 1}.1.0",
                "agent_chat",
                "b",
                created_at=segment_stamps[index],
            )
        ], 1

    monkeypatch.setattr(strip, "build_timeline_items", _build)
    monkeypatch.setattr(checkpoint, "load_checkpoint_messages", _checkpoint_messages)

    def _boundaries(_agent_id: int) -> list[str]:
        return [f"ck-{i}" for i in range(1, 6)]

    def _segments(_agent_id: int, boundary: str) -> list[object]:
        return [boundary]

    monkeypatch.setattr(checkpoint, "list_compact_boundary_checkpoint_ids", _boundaries)
    monkeypatch.setattr(checkpoint, "load_checkpoint_messages_segment", _segments)
    return seen_segments


def test_window_stops_walking_once_a_segment_covers_the_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window_start = datetime(
        2026, 9, 1, 0, 0, tzinfo=UTC
    )  # time-bomb-ok: explicit fixture window input
    window_end = datetime(
        2026, 9, 1, 1, 0, tzinfo=UTC
    )  # time-bomb-ok: explicit fixture window input
    seen_segments = _history_walk_fixture(
        monkeypatch,
        [
            "2026-09-01T00:40:00+00:00",  # inside the window, keep walking
            "2026-08-31T23:00:00+00:00",  # covers the window start, stop here
        ],
    )

    messages, truncated = strip._strip_messages_for_window(7, window_start, window_end)

    # Coverage stopped the walk after two segments; the three further
    # boundaries only hold older history, so nothing is truncated. The
    # covering segment's own message sits BEFORE the window and is filtered
    # out — it served coverage only.
    assert seen_segments == ["s1.ck-1", "s2.ck-2"]
    assert [message.key for message in messages] == ["s1.ck-1.1", "c.1"]
    assert truncated is False


def test_window_flags_the_walk_cap_when_coverage_is_unreached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window_start = datetime(
        2026, 9, 1, 0, 0, tzinfo=UTC
    )  # time-bomb-ok: explicit fixture window input
    window_end = datetime(
        2026, 9, 1, 1, 0, tzinfo=UTC
    )  # time-bomb-ok: explicit fixture window input
    seen_segments = _history_walk_fixture(
        monkeypatch,
        [
            "2026-09-01T00:40:00+00:00",
            "2026-09-01T00:30:00+00:00",
            "2026-09-01T00:20:00+00:00",  # still inside the window at the cap
        ],
    )

    messages, truncated = strip._strip_messages_for_window(7, window_start, window_end)

    assert seen_segments == ["s1.ck-1", "s2.ck-2", "s3.ck-3"]
    assert [message.key for message in messages] == ["s3.ck-3.1", "s2.ck-2.1", "s1.ck-1.1", "c.1"]
    assert truncated is True


def test_window_caps_the_message_budget_and_flags_it(monkeypatch: pytest.MonkeyPatch) -> None:
    window_start = datetime(
        2026, 9, 1, 0, 0, tzinfo=UTC
    )  # time-bomb-ok: explicit fixture window input
    window_end = datetime(
        2026, 9, 1, 1, 0, tzinfo=UTC
    )  # time-bomb-ok: explicit fixture window input
    items = [
        _item(f"{index}.0", "agent_chat", "x", created_at=f"2026-09-01T00:{index:02d}:00+00:00")
        for index in range(1, 5)
    ]
    monkeypatch.setattr(strip, "settings", _strip_settings(2, 20000))
    monkeypatch.setattr(strip, "needs_chat_anchors", _no_anchors)

    def _build(
        _messages: list[object], _anchors: list[object], **_kwargs: object
    ) -> tuple[list[TimelineItem], int]:
        return items, 4

    monkeypatch.setattr(strip, "build_timeline_items", _build)
    monkeypatch.setattr(checkpoint, "load_checkpoint_messages", _checkpoint_messages)
    monkeypatch.setattr(checkpoint, "list_compact_boundary_checkpoint_ids", _no_boundaries)

    messages, truncated = strip._strip_messages_for_window(7, window_start, window_end)

    assert [message.key for message in messages] == ["c.3", "c.4"]
    assert truncated is True


def test_window_budget_parameter_overrides_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    window_start = datetime(
        2026, 9, 1, 0, 0, tzinfo=UTC
    )  # time-bomb-ok: explicit fixture window input
    window_end = datetime(
        2026, 9, 1, 1, 0, tzinfo=UTC
    )  # time-bomb-ok: explicit fixture window input
    items = [
        _item(f"{index}.0", "agent_chat", "x", created_at=f"2026-09-01T00:{index:02d}:00+00:00")
        for index in range(1, 5)
    ]
    monkeypatch.setattr(strip, "settings", _strip_settings(600, 20000))
    monkeypatch.setattr(strip, "needs_chat_anchors", _no_anchors)

    def _build(
        _messages: list[object], _anchors: list[object], **_kwargs: object
    ) -> tuple[list[TimelineItem], int]:
        return items, 4

    monkeypatch.setattr(strip, "build_timeline_items", _build)
    monkeypatch.setattr(checkpoint, "load_checkpoint_messages", _checkpoint_messages)
    monkeypatch.setattr(checkpoint, "list_compact_boundary_checkpoint_ids", _no_boundaries)

    messages, truncated = strip._strip_messages_for_window(7, window_start, window_end, 2)

    assert [message.key for message in messages] == ["c.3", "c.4"]
    assert truncated is True


def test_strip_read_forwards_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int | None] = []

    def _capture(
        _agent_id: int, _start: datetime, _end: datetime, budget: int | None = None
    ) -> tuple[list[object], bool]:
        seen.append(budget)
        return [], False

    monkeypatch.setattr(strip, "_strip_messages_for_window", _capture)
    now = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)  # time-bomb-ok: explicit fixture window input

    strip.strip_for_window_or_none(7, now, now, 120)
    strip.strip_for_window_or_none(7, now, now)

    assert seen == [120, None]


def test_strip_read_clamps_the_requested_budget_to_the_setting_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[int | None] = []

    def _capture(
        _agent_id: int, _start: datetime, _end: datetime, budget: int | None = None
    ) -> tuple[list[object], bool]:
        seen.append(budget)
        return [], False

    monkeypatch.setattr(strip, "_strip_messages_for_window", _capture)
    monkeypatch.setattr(strip, "settings", _strip_settings(600, 20000))
    now = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)  # time-bomb-ok: explicit fixture window input

    strip.strip_for_window_or_none(7, now, now, 9999)
    strip.strip_for_window_or_none(7, now, now, 600)

    assert seen == [600, 600]


def test_window_excludes_legacy_epoch_timestamps_and_flags_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window_start = datetime(
        2026, 9, 1, 0, 0, tzinfo=UTC
    )  # time-bomb-ok: explicit fixture window input
    window_end = datetime(
        2026, 9, 1, 1, 0, tzinfo=UTC
    )  # time-bomb-ok: explicit fixture window input
    items = [
        _item("1.0", "agent_chat", "legacy", created_at="1970-01-01T00:00:00.000001+00:00"),
        _item("2.0", "agent_chat", "modern", created_at="2026-09-01T00:30:00+00:00"),
    ]
    monkeypatch.setattr(strip, "settings", _strip_settings(600, 20000))
    monkeypatch.setattr(strip, "needs_chat_anchors", _no_anchors)

    def _build(
        _messages: list[object], _anchors: list[object], **_kwargs: object
    ) -> tuple[list[TimelineItem], int]:
        return items, 2

    monkeypatch.setattr(strip, "build_timeline_items", _build)
    monkeypatch.setattr(checkpoint, "load_checkpoint_messages", _checkpoint_messages)
    monkeypatch.setattr(checkpoint, "list_compact_boundary_checkpoint_ids", _no_boundaries)

    messages, truncated = strip._strip_messages_for_window(7, window_start, window_end)

    assert [message.key for message in messages] == ["c.2"]
    assert truncated is True


def test_strip_read_degrades_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_agent_id: int) -> list[object]:
        raise RuntimeError("checkpoint store down")

    monkeypatch.setattr(checkpoint, "load_checkpoint_messages", _boom)

    assert strip.strip_for_window_or_none(
        7, datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 1, 1, tzinfo=UTC)
    ) == (None, None)


def test_message_group_resolution_and_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(strip, "needs_chat_anchors", _no_anchors)

    def _build(
        _messages: list[object], _anchors: list[object], **_kwargs: object
    ) -> tuple[list[TimelineItem], int]:
        return [_item("5.0", "agent_chat", "hello")], 1

    monkeypatch.setattr(strip, "build_timeline_items", _build)
    monkeypatch.setattr(checkpoint, "load_checkpoint_messages", _checkpoint_messages)

    group = strip._strip_message_group(7, "c.5")
    assert [item.item_id for item in group] == ["5.0"]

    with pytest.raises(HTTPException) as excinfo:
        strip._strip_message_group(7, "c.99")
    assert excinfo.value.status_code == 404

    with pytest.raises(HTTPException):
        strip._strip_message_group(7, "bogus")


def test_message_details_clip_and_full_refetch(monkeypatch: pytest.MonkeyPatch) -> None:
    group = [_item("5.0", "code_output", "y" * 40)]
    monkeypatch.setattr(strip, "settings", _strip_settings(600, 8))

    def _group(_agent_id: int, _key: str) -> list[TimelineItem]:
        return group

    monkeypatch.setattr(strip, "_strip_message_group", _group)

    clipped = strip.get_run_timeline_message(7, "c.5", False)
    assert clipped.content_truncated is True
    assert clipped.parts[0].text_truncated is True
    assert clipped.parts[0].text == "y" * 8
    assert clipped.parts[0].chars == 40

    uncut = strip.get_run_timeline_message(7, "c.5", True)
    assert uncut.content_truncated is False
    assert uncut.parts[0].text == "y" * 40


# --- short-TTL segment cache (review condition 10, 2026-09-19) --------------


@pytest.fixture(autouse=True)
def _clear_strip_cache() -> None:
    """The strip cache is process-global; every test starts from cold so a
    stubbed loader's value can never leak into the next test."""
    strip._STRIP_CACHE.clear()


def test_segment_cache_serves_within_ttl_and_reloads_after() -> None:
    now = [100.0]
    cache = strip.SegmentReadCache(max_entries=4, clock=lambda: now[0])
    loads: list[int] = []

    def load() -> str:
        loads.append(1)
        return "v"

    assert cache.get(("k",), 5.0, load) == "v"
    assert cache.get(("k",), 5.0, load) == "v"
    assert len(loads) == 1

    now[0] += 5.5  # past the TTL
    assert cache.get(("k",), 5.0, load) == "v"
    assert len(loads) == 2


def test_segment_cache_evicts_the_least_recently_used_entry() -> None:
    cache = strip.SegmentReadCache(max_entries=2, clock=lambda: 0.0)
    cache.get(("a",), 60.0, lambda: "a")
    cache.get(("b",), 60.0, lambda: "b")
    cache.get(("a",), 60.0, lambda: "a")  # refresh a's recency
    cache.get(("c",), 60.0, lambda: "c")  # evicts b

    reloaded: list[str] = []

    def load_b() -> str:
        reloaded.append("b")
        return "b"

    assert cache.get(("b",), 60.0, load_b) == "b"
    assert reloaded == ["b"]


def test_strip_and_details_reads_share_the_cached_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window_start = datetime(
        2026, 9, 1, 0, 0, tzinfo=UTC
    )  # time-bomb-ok: explicit fixture window input
    items = [
        _item("1.0", "system_prompt", "p" * 10),
        _item("2.0", "agent_chat", "b" * 10, created_at="2026-09-01T00:30:00+00:00"),
    ]
    monkeypatch.setattr(strip, "settings", _strip_settings(600, 20000))
    monkeypatch.setattr(strip, "needs_chat_anchors", _no_anchors)

    def _build(
        _messages: list[object], _anchors: list[object], **_kwargs: object
    ) -> tuple[list[TimelineItem], int]:
        return items, 2

    monkeypatch.setattr(strip, "build_timeline_items", _build)
    loads: list[int] = []

    def _counting(agent_id: int) -> list[object]:
        loads.append(agent_id)
        return ["m"]

    monkeypatch.setattr(checkpoint, "load_checkpoint_messages", _counting)
    monkeypatch.setattr(checkpoint, "list_compact_boundary_checkpoint_ids", _no_boundaries)

    messages, _ = strip._strip_messages_for_window(7, window_start, window_start.replace(hour=1))
    # The ts-less head (c.1) stays in the strip, sorted first.
    assert [message.key for message in messages] == ["c.1", "c.2"]
    group = strip._strip_message_group(7, "c.2")
    assert len(group) == 1
    assert loads == [7]  # one read served both the strip and the details call
