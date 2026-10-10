"""`chat_anchor_demand`: a render that the demand says ignores anchors renders
identically without them, and one that needs only referenced anchors renders
identically from just those rows."""

from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from base.agents.history.chat_anchors import ChatAnchorDemand, chat_anchor_demand
from base.agents.history.timeline import build_timeline_items
from base.db import ChatAnchor

# Unrelated rows on both sides of the referenced ones: an exact-only render
# must not depend on them.
_ANCHORS = [
    ChatAnchor(inbound_id, datetime(2026, 8, 22, 18, minute, tzinfo=UTC))
    for inbound_id, minute in [(5, 1), (7, 7), (9, 9), (11, 11)]
]


def _modern_inbound(inbound_id: int) -> HumanMessage:
    return HumanMessage(
        content=f"inbound {inbound_id}",
        additional_kwargs={
            "ava_msg_type": "inbound",
            "ava_source": "user",
            "ava_inbound_id": inbound_id,
            "ava_created_at": "2026-08-22T19:00:00+00:00",
        },
    )


def _legacy_inbound() -> HumanMessage:
    return HumanMessage(
        content="legacy", additional_kwargs={"ava_msg_type": "inbound", "ava_source": "ui:web"}
    )


def _exec_output(*, stamped: bool) -> ToolMessage:
    kwargs: dict[str, object] = {"ava_msg_type": "exec_output"}
    if stamped:
        kwargs["ava_created_at"] = "2026-08-22T19:00:01+00:00"
    return ToolMessage(content="out", tool_call_id="t1", additional_kwargs=kwargs)


def _stamped_ai() -> AIMessage:
    return AIMessage(
        content="ok", additional_kwargs={"ava_created_at": "2026-08-22T19:00:02+00:00"}
    )


def test_all_modern_render_ignores_anchors() -> None:
    messages: list[BaseMessage] = [
        SystemMessage(content="prompt"),
        _modern_inbound(7),
        _exec_output(stamped=True),
        _stamped_ai(),
    ]

    assert chat_anchor_demand(messages) is None
    assert build_timeline_items(messages, _ANCHORS) == build_timeline_items(messages, [])


@pytest.mark.parametrize(
    "messages",
    [
        # Mixed history: a legacy sibling takes its ts from the modern inbound's anchor.
        [_modern_inbound(7), _exec_output(stamped=False)],
        # A modern inbound missing its read time takes the anchor's ts itself.
        [
            HumanMessage(
                content="no ts",
                additional_kwargs={"ava_msg_type": "inbound", "ava_inbound_id": 9},
            ),
            _stamped_ai(),
        ],
    ],
)
def test_exact_only_render_needs_just_the_referenced_rows(messages: list[BaseMessage]) -> None:
    demand = chat_anchor_demand(messages)

    assert demand is not None
    assert not demand.positional
    referenced = [anchor for anchor in _ANCHORS if anchor.id in demand.referenced_ids]
    assert build_timeline_items(messages, referenced) == build_timeline_items(messages, _ANCHORS)
    # The anchors really are consumed: an anchor-less render differs.
    assert build_timeline_items(messages, []) != build_timeline_items(messages, _ANCHORS)


def test_id_less_inbound_demands_positional_anchors() -> None:
    messages: list[BaseMessage] = [_legacy_inbound(), _modern_inbound(9)]

    assert chat_anchor_demand(messages) == ChatAnchorDemand(referenced_ids=[9], positional=True)


def test_malformed_embedded_id_is_left_to_rendering() -> None:
    malformed = HumanMessage(
        content="bad",
        additional_kwargs={"ava_msg_type": "inbound", "ava_inbound_id": "7"},
    )

    assert chat_anchor_demand([malformed]) == ChatAnchorDemand(referenced_ids=[], positional=False)
    with pytest.raises(ValueError, match="ava_inbound_id"):
        build_timeline_items([malformed], [])
