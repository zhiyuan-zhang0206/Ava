from langchain_core.messages import HumanMessage

from base.agents.history.timeline import TimelineItem, build_timeline_items
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.clock import Clock
from base.config import settings

_TIMELINE_INPUTS = TimelineReadInputs(
    Clock.from_settings, lambda: settings.general.message_timestamps
)


def _items(*ids: str) -> list[TimelineItem]:
    return [TimelineItem(item_id=i, kind="agent_chat", payload=i) for i in ids]


class TestBuildTimelineItemsStartOffset:
    def test_start_offset_keeps_absolute_item_ids_and_full_msg_count(self):
        from langchain_core.messages import AIMessage, SystemMessage

        messages = [
            SystemMessage(content="prompt"),
            AIMessage(content="first"),
            AIMessage(content="second"),
        ]
        items, msg_count = build_timeline_items(messages, start=2, inputs=_TIMELINE_INPUTS)
        assert msg_count == 3
        assert [it.item_id for it in items] == ["2.0"]
        assert items[0].payload == "second"

    def test_start_zero_matches_no_offset(self):
        from langchain_core.messages import AIMessage, SystemMessage

        messages = [SystemMessage(content="prompt"), AIMessage(content="first")]
        full, full_count = build_timeline_items(messages, inputs=_TIMELINE_INPUTS)
        sliced, sliced_count = build_timeline_items(messages, start=0, inputs=_TIMELINE_INPUTS)
        assert full_count == sliced_count
        assert [it.item_id for it in full] == [it.item_id for it in sliced]
        assert [it.payload for it in full] == [it.payload for it in sliced]

    def test_start_offset_renders_an_inbound_with_its_own_time(self):
        from langchain_core.messages import SystemMessage

        msg = HumanMessage(
            content="hi",
            additional_kwargs={
                "ava_msg_type": "inbound",
                "ava_created_at": "2026-01-01T00:00:00+00:00",
            },
        )
        items, msg_count = build_timeline_items(
            [SystemMessage(content="p"), msg], start=1, inputs=_TIMELINE_INPUTS
        )
        assert msg_count == 2
        assert items[0].item_id == "1.0"
        assert items[0].created_at == "2026-01-01T00:00:00+00:00"
        assert items[0].inbound_id is None

    def test_start_offset_keeps_modern_embedded_inbound_id(self):
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
            [SystemMessage(content="p"), msg], start=1, inputs=_TIMELINE_INPUTS
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
    items, _ = build_timeline_items(messages, inputs=_TIMELINE_INPUTS)
    assert items[0].created_at == "2026-01-01T00:00:20+00:00"
