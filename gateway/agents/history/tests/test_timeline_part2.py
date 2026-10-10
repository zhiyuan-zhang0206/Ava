"""Contract tests for GET /api/agents/{id}/timeline.

Design (2026-05-04 in-place simplification):
  - LangGraph state.messages is the sole source of truth
  - The inbound_messages table does **not** directly enter the timeline (it is already
    envelope-wrapped into LangGraph state), only used as a ts anchor
  - AIMessage.content (agent text) / tool_call code / reasoning are all rendered from
    state; adjacent timestamps within the same LLM turn no longer drift apart

Coverage:
  - endpoint basic contract (404 / empty agent / no orphaned inbounds displayed /
    lifecycle anchor filtering)
  - `_ai_message_items` helper unit (TestAiMessageItems): directly feed AIMessage to test
    block splitting
  - dispatch end-to-end (TestTimelineDispatch): truly load mixed state.messages into
    PostgresSaver checkpoint, run a full GET /timeline, verify that AIMessage / lifecycle /
    string-content paths all correctly render through the dispatch chain — unit test helpers
    passing ≠ dispatch routing correct (the bug in refactor 8a3c520 that accidentally deleted
    the elif header and silently lost AIMessage is exactly this kind of bug)
"""

from langchain_core.messages import HumanMessage

from base.agents.history.timeline import TimelineItem, build_timeline_items
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.clock import Clock
from base.config import settings

_TIMELINE_INPUTS = TimelineReadInputs(
    Clock.from_settings, lambda: settings.general.message_timestamps
)

# Side effect: get_timeline's _log should have had a warning trace (operators can grep)
# Do not assert log content — avoid coupling with logger implementation details


def _items(*ids: str) -> list[TimelineItem]:
    return [TimelineItem(item_id=i, kind="agent_chat", payload=i) for i in ids]


class TestBuildTimelineItemsStartOffset:
    """`build_timeline_items(..., start=N)` — the incremental-snapshot render
    path. Item ids keep their ABSOLUTE msg_idx; msg_count stays the FULL
    history length; anchors are NOT consumed when start > 0."""

    def test_start_offset_keeps_absolute_item_ids_and_full_msg_count(self):
        from langchain_core.messages import AIMessage, SystemMessage

        messages = [
            SystemMessage(content="prompt"),
            AIMessage(content="first"),
            AIMessage(content="second"),
        ]
        items, msg_count = build_timeline_items(messages, [], start=2, inputs=_TIMELINE_INPUTS)
        assert msg_count == 3  # full length, never the window length
        assert [it.item_id for it in items] == ["2.0"]
        assert items[0].payload == "second"

    def test_start_zero_matches_no_offset(self):
        from langchain_core.messages import AIMessage, SystemMessage

        messages = [SystemMessage(content="prompt"), AIMessage(content="first")]
        full, full_count = build_timeline_items(messages, [], inputs=_TIMELINE_INPUTS)
        sliced, sliced_count = build_timeline_items(messages, [], start=0, inputs=_TIMELINE_INPUTS)
        assert full_count == sliced_count
        assert [it.item_id for it in full] == [it.item_id for it in sliced]
        assert [it.payload for it in full] == [it.payload for it in sliced]

    def test_start_offset_does_not_consume_anchors(self):
        # An inbound inside the incremental window must NOT consume the first
        # historical anchor (it would misalign ts / inbound_id). Modern
        # messages carry ava_created_at, so the anchor list is irrelevant on
        # the incremental path — pass [] and the item still renders.
        from langchain_core.messages import SystemMessage

        msg = HumanMessage(
            content="hi",
            additional_kwargs={
                "ava_msg_type": "inbound",
                "ava_created_at": "2026-01-01T00:00:00+00:00",
            },
        )
        items, msg_count = build_timeline_items(
            [SystemMessage(content="p"), msg], [], start=1, inputs=_TIMELINE_INPUTS
        )
        assert msg_count == 2
        assert items[0].item_id == "1.0"
        assert items[0].created_at == "2026-01-01T00:00:00+00:00"
        assert items[0].inbound_id is None

    def test_start_offset_keeps_modern_embedded_inbound_id_without_anchors(self):
        from langchain_core.messages import SystemMessage

        msg = HumanMessage(
            content="hi",
            additional_kwargs={
                "ava_msg_type": "inbound",
                "ava_source": "user",
                "ava_inbound_id": 70598,
                "ava_created_at": "2026-01-01T00:00:00+00:00",
            },
        )

        items, msg_count = build_timeline_items(
            [SystemMessage(content="p"), msg], [], start=1, inputs=_TIMELINE_INPUTS
        )

        assert msg_count == 2
        assert items[0].item_id == "1.0"
        assert items[0].inbound_id == 70598

    def test_segment_prefix_keeps_local_message_and_block_positions(self):
        from langchain_core.messages import AIMessage

        items, msg_count = build_timeline_items(
            [
                HumanMessage(content="older inbound"),
                AIMessage(
                    content=[
                        {"type": "thinking", "thinking": "older reasoning", "index": 0},
                        {"type": "text", "text": "older answer", "index": 1},
                    ]
                ),
            ],
            [],
            segment_prefix="s2.1f0b9b12-0000-6000-8000-000000000000",
            inputs=_TIMELINE_INPUTS,
        )

        assert msg_count == 2
        assert [item.item_id for item in items] == [
            "s2.1f0b9b12-0000-6000-8000-000000000000.0.0",
            "s2.1f0b9b12-0000-6000-8000-000000000000.1.0",
            "s2.1f0b9b12-0000-6000-8000-000000000000.1.1",
        ]


def test_inbound_item_ts_is_read_time_not_arrival() -> None:
    """A message that arrived mid-stream (arrival < the AIMessage's stamp) is
    placed on the timeline at its pickup time, after the AIMessage."""
    from langchain_core.messages import HumanMessage

    from base.agents.history.timeline import build_timeline_items

    messages = [
        HumanMessage(
            content="[2026-01-01 00:00:01]\n\nhi",
            additional_kwargs={
                "ava_msg_type": "inbound",
                "ava_source": "user",
                "ava_inbound_id": 1,
                "ava_created_at": "2026-01-01T00:00:01+00:00",
                "ava_picked_up_at": "2026-01-01T00:00:20+00:00",
            },
        ),
    ]
    items, _ = build_timeline_items(messages, [], inputs=_TIMELINE_INPUTS)
    assert items[0].created_at == "2026-01-01T00:00:20+00:00"
