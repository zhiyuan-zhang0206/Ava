"""Timeline cases: timeline skips inbound without langgraph state."""

from __future__ import annotations

import psycopg
import pytest
from langchain_core.messages import BaseMessage, HumanMessage

from base.agents.history.timeline import build_timeline_items, tail_window
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.agents.inbox.insert import insert_inbound_message
from base.clock import Clock
from base.config import settings
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from gateway.agents.history.timeline import window_before
from gateway.tests.history.test_timeline import _items
from gateway.tests.history.test_timeline import test_client as test_client
from tests.fixtures.gateway_config import GatewayTestClient

_TIMELINE_INPUTS = TimelineReadInputs(
    Clock.from_settings, lambda: settings.general.message_timestamps
)


def test_timeline_skips_inbound_without_langgraph_state(
    db_conn: psycopg.Connection,
    test_client: GatewayTestClient,
    database: Database,
    event_bus: EventBus,
) -> None:
    """Data in the inbound_messages table does not directly enter the timeline — it only
    appears after the claim node envelope-wraps the HumanMessage into the LangGraph state.
    Here we only INSERT an inbound without running the graph; the timeline should not see
    this inbound."""
    tid = create_agent(db_conn)
    insert_inbound_message(
        db_conn,
        tid,
        "a user message but graph didn't run",
        source="user",
        bus=event_bus,
        database=database,
    )

    resp = test_client.get(f"/api/agents/{tid}/timeline")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [it for it in items if it["kind"] == "inbound_chat"] == []


def test_item_created_at_prefers_real_meiva_created_at_over_synthetic() -> None:
    """A message carrying a real `meiva_created_at` surfaces it verbatim — the
    timeline shows the message's own wall-clock time, not the synthetic
    anchor+microsecond offset (which renders as 1970 when no chat preceded it)."""
    from langchain_core.messages import ToolMessage

    msg = ToolMessage(
        content="out",
        tool_call_id="t1",
        additional_kwargs={
            "meiva_msg_type": "exec_output",
            "meiva_created_at": "2026-06-19T12:00:00+00:00",
        },
    )
    items, _ = build_timeline_items([msg], inputs=_TIMELINE_INPUTS)
    assert items[0].created_at == "2026-06-19T12:00:00+00:00"


@pytest.mark.parametrize("malformed_id", ["70598", True, False, 0, -1, None])
def test_inbound_rejects_malformed_embedded_id(malformed_id: object) -> None:
    """A present correlation key is contractual, never a legacy fallback hint."""
    from langchain_core.messages import HumanMessage

    inbound = HumanMessage(
        content="message",
        additional_kwargs={
            "meiva_msg_type": "inbound",
            "meiva_source": "user",
            "meiva_inbound_id": malformed_id,
            "meiva_created_at": "2026-08-22T17:47:00+00:00",
        },
    )

    with pytest.raises(ValueError, match="meiva_inbound_id"):
        build_timeline_items([inbound], inputs=_TIMELINE_INPUTS)


def test_aimessage_blocks_share_one_real_meiva_created_at() -> None:
    """All blocks of one AIMessage carry the message's single real ts — the
    timeline no longer fans its reasoning/text/code items out across synthetic
    per-block microsecond offsets."""
    from langchain_core.messages import AIMessage

    msg = AIMessage(
        content=[
            {"type": "thinking", "thinking": "hmm", "index": 0},
            {"type": "text", "text": "done", "index": 1},
        ],
        additional_kwargs={"meiva_created_at": "2026-06-19T12:00:00+00:00"},
    )
    items, _ = build_timeline_items([msg], inputs=_TIMELINE_INPUTS)
    assert [it.created_at for it in items] == [
        "2026-06-19T12:00:00+00:00",
        "2026-06-19T12:00:00+00:00",
    ]


class TestTimelineWindowing:
    """tail_window / window_before pure-function contract — the slicing
    behind GET /timeline pagination + the agent-published snapshot trim
    (no DB)."""

    def test_tail_window_shorter_than_limit_returns_all_no_more(self) -> None:
        window, has_more = tail_window(_items("1.0", "2.0"), 5)
        assert [i.item_id for i in window] == ["1.0", "2.0"]
        assert has_more is False

    def test_tail_window_trims_to_newest_and_flags_more(self) -> None:
        window, has_more = tail_window(_items("1.0", "2.0", "3.0", "4.0"), 2)
        assert [i.item_id for i in window] == ["3.0", "4.0"]
        assert has_more is True

    def test_window_before_returns_items_immediately_older(self) -> None:
        window, has_more = window_before(_items("1.0", "2.0", "3.0", "4.0", "5.0"), "4.0", 2)
        assert [i.item_id for i in window] == ["2.0", "3.0"]
        assert has_more is True  # 1.0 still older than the window

    def test_window_before_reaching_start_has_no_more(self) -> None:
        window, has_more = window_before(_items("1.0", "2.0", "3.0"), "3.0", 5)
        assert [i.item_id for i in window] == ["1.0", "2.0"]
        assert has_more is False

    def test_window_before_unknown_cursor_returns_empty(self) -> None:
        window, has_more = window_before(_items("1.0", "2.0"), "9.9", 5)
        assert window == []
        assert has_more is False


class TestMultimodalInbound:
    """A multimodal inbound HumanMessage (list content: a text block + native
    base64 image blocks) renders as an inbound_chat item whose payload is the
    text part and whose `images` are the reference urls — the base64 in the
    message content is never str()-d into the payload."""

    @staticmethod
    def _msg() -> HumanMessage:
        return HumanMessage(
            content=[
                {"type": "text", "text": "User:\n\nwhat is this?"},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
                },
            ],
            additional_kwargs={
                "meiva_msg_type": "inbound",
                "meiva_source": "user",
                "meiva_inbound_id": 1,
                "meiva_image_urls": ["/api/agents/7/uploads/shot.png"],
            },
        )

    def test_renders_text_and_images_not_base64(self) -> None:

        items, _ = build_timeline_items([self._msg()], inputs=_TIMELINE_INPUTS)
        (item,) = items
        assert item.kind == "inbound_chat"
        assert item.payload == "User:\n\nwhat is this?"
        assert item.images == ["/api/agents/7/uploads/shot.png"]
        # The base64 payload must never leak into the rendered text.
        assert "QUJD" not in item.payload

    def test_image_only_payload_is_placeholder_text(self) -> None:

        msg = HumanMessage(
            content=[
                {"type": "text", "text": "User:\n\n[image]"},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
                },
            ],
            additional_kwargs={
                "meiva_msg_type": "inbound",
                "meiva_source": "user",
                "meiva_inbound_id": 2,
                "meiva_image_urls": ["/api/agents/7/uploads/a.png"],
            },
        )
        items, _ = build_timeline_items([msg], inputs=_TIMELINE_INPUTS)
        assert items[0].images == ["/api/agents/7/uploads/a.png"]
        assert "QUJD" not in items[0].payload


class TestSystemPromptInColdLoad:
    """#570: the expandable prompt card's data source.

    `tail_window` returns only the newest `limit` items, so any conversation
    with more than 50 rendered items loses the oldest item — the ~128KB
    system-prompt item 0.0 — and the prompt card would render empty on first
    open and never recover (SSE snapshots window too, and the frontend can't
    cold-load older windows without this endpoint). The endpoint must always
    serve 0.0 on the default window.
    """

    @staticmethod
    def _put_checkpoint(agent_id: int, messages: list) -> None:
        from langgraph.checkpoint.base import empty_checkpoint
        from langgraph.checkpoint.postgres import PostgresSaver

        from base.config import settings

        ckpt = empty_checkpoint()
        ckpt["channel_values"] = {"messages": messages}
        ckpt["channel_versions"] = {"messages": "1", "__start__": "1"}
        with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
            saver.setup()
            saver.put(
                config={
                    "configurable": {
                        "thread_id": str(agent_id),
                        "checkpoint_ns": "",
                    }
                },
                checkpoint=ckpt,
                metadata={"source": "input", "step": 1, "parents": {}},
                new_versions={"messages": "1"},
            )

    def test_long_conversation_keeps_system_prompt(
        self, db_conn: psycopg.Connection, test_client: GatewayTestClient
    ) -> None:
        """>50 rendered items: 0.0 must still head the default window."""
        from langchain_core.messages import HumanMessage, SystemMessage

        tid = create_agent(db_conn)
        messages: list[BaseMessage] = [SystemMessage(content="You are Meiva.")]
        for i in range(60):
            messages.append(HumanMessage(content=f"user msg {i}"))
        self._put_checkpoint(tid, messages)  # pyright: ignore[reportUnknownMemberType]

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        data = resp.json()
        # 60 humans + 1 system prompt = 61 items; tail window = default 50 + re-attached 0.0
        assert data["has_more"] is True
        assert data["items"][0]["item_id"] == "0.0"
        assert data["items"][0]["kind"] == "system_prompt"
        assert data["items"][0]["payload"] == "You are Meiva."
        assert len(data["items"]) == 51
        # The rest is the newest window in order (0.0 is prepended, not replacing)
        assert data["items"][1]["item_id"] == "11.0"

    def test_short_conversation_does_not_duplicate(
        self, db_conn: psycopg.Connection, test_client: GatewayTestClient
    ) -> None:
        """<=50 items: the window already contains 0.0 — no duplicate entry."""
        from langchain_core.messages import HumanMessage, SystemMessage

        tid = create_agent(db_conn)
        messages: list[BaseMessage] = [SystemMessage(content="You are Meiva.")]
        for i in range(10):
            messages.append(HumanMessage(content=f"user msg {i}"))
        self._put_checkpoint(tid, messages)  # pyright: ignore[reportUnknownMemberType]

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        data = resp.json()
        ids = [it["item_id"] for it in data["items"]]
        assert ids[0] == "0.0"
        assert ids.count("0.0") == 1
        assert data["has_more"] is False

    def test_default_window_comes_from_display_config(
        self,
        db_conn: psycopg.Connection,
        test_client: GatewayTestClient,
    ) -> None:
        """The implicit window is ``settings.display.timeline_default_limit``
        (``MEIVA_TIMELINE_DEFAULT_LIMIT``); 50 is only that field's default."""
        from langchain_core.messages import HumanMessage

        test_client.app.state.config_authority.runtime.display.timeline_default_limit = 5

        tid = create_agent(db_conn)
        self._put_checkpoint(  # pyright: ignore[reportUnknownMemberType]
            tid, [HumanMessage(content=f"m{i}") for i in range(8)]
        )

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_more"] is True
        assert [it["item_id"] for it in data["items"]] == ["3.0", "4.0", "5.0", "6.0", "7.0"]

    def test_long_conversation_rehangs_compact_summary(
        self, db_conn: psycopg.Connection, test_client: GatewayTestClient
    ) -> None:
        """Compact summaries are standing context: in a long conversation the
        earliest inbound_compact_summary falls off the tail window and the
        re-attached 0.0 makes older paging unable to reach it — so GET must
        re-attach every compact_summary right after the prompt (user report
        2026-08-06). Not counted against `limit`; has_more recomputed."""
        from langchain_core.messages import HumanMessage, SystemMessage

        tid = create_agent(db_conn)
        messages: list[BaseMessage] = [SystemMessage(content="You are Meiva.")]
        messages.append(
            HumanMessage(
                content="Conversation summary line",
                additional_kwargs={
                    "meiva_msg_type": "compact_summary",
                    "meiva_created_at": "2026-08-06T00:00:00+00:00",
                },
            )
        )
        for i in range(60):
            messages.append(HumanMessage(content=f"user msg {i}"))
        self._put_checkpoint(tid, messages)  # pyright: ignore[reportUnknownMemberType]

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        data = resp.json()
        # 0.0 prompt, then the re-attached compact summary, then the newest window
        assert data["items"][0]["item_id"] == "0.0"
        assert data["items"][0]["kind"] == "system_prompt"
        assert data["items"][1]["item_id"] == "1.0"
        assert data["items"][1]["kind"] == "inbound_compact_summary"
        assert "Conversation summary line" in data["items"][1]["payload"]
        assert data["items"][2]["item_id"] == "12.0"
        assert len(data["items"]) == 52  # 50 + prompt + compact summary
        assert data["has_more"] is True

    def test_short_conversation_compact_summary_not_duplicated(
        self, db_conn: psycopg.Connection, test_client: GatewayTestClient
    ) -> None:
        """<=50 items: the window already contains the compact summary —
        no duplicate entry."""
        from langchain_core.messages import HumanMessage, SystemMessage

        tid = create_agent(db_conn)
        messages: list[BaseMessage] = [SystemMessage(content="You are Meiva.")]
        messages.append(
            HumanMessage(
                content="Short summary",
                additional_kwargs={
                    "meiva_msg_type": "compact_summary",
                    "meiva_created_at": "2026-08-06T00:00:00+00:00",
                },
            )
        )
        for i in range(10):
            messages.append(HumanMessage(content=f"user msg {i}"))
        self._put_checkpoint(tid, messages)  # pyright: ignore[reportUnknownMemberType]

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        items = resp.json()["items"]
        compact = [it for it in items if it["kind"] == "inbound_compact_summary"]
        assert len(compact) == 1
        assert items[0]["item_id"] == "0.0"
        assert compact[0]["item_id"] == "1.0"

    def test_before_paging_does_not_carry_system_prompt(
        self, db_conn: psycopg.Connection, test_client: GatewayTestClient
    ) -> None:
        """Scroll-up paging (`before`) is history-only — 0.0 is not re-sent."""
        from langchain_core.messages import HumanMessage, SystemMessage

        tid = create_agent(db_conn)
        messages: list[BaseMessage] = [SystemMessage(content="You are Meiva.")]
        for i in range(60):
            messages.append(HumanMessage(content=f"user msg {i}"))
        self._put_checkpoint(tid, messages)  # pyright: ignore[reportUnknownMemberType]

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        oldest = resp.json()["items"][-1]["item_id"]
        page = test_client.get(f"/api/agents/{tid}/timeline", params={"before": oldest})
        assert page.status_code == 200
        pdata = page.json()
        assert all(it["item_id"] != "0.0" for it in pdata["items"])
