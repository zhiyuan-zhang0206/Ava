"""Timeline cases: attach items."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage

from base.agents.history.timeline import (
    build_timeline_items,
)
from base.db import Database, create_agent, insert_inbound_message
from base.events.live.bus import EventBus
from gateway.agents.tests.test_timeline import (
    test_client as test_client,
)


class TestAttachItems:
    """`build_timeline_items` dispatch for ava_msg_type="attach" messages.

    An attach message (product of `agent/graph/_attach_drain.py`) carries a
    leading text caption block plus provider-native media blocks (image data
    URIs, pdf document blocks, ...). It must render as a dedicated `attach`
    item: payload = caption text only (the base64 must never leak into the
    payload), images = the image data URIs for thumbnails, item_id = msg_idx.0.
    """

    @staticmethod
    def _attach_message(
        tmp_path: Path,
        *,
        blocks_override: list[dict[str, Any]] | None = None,
    ) -> HumanMessage:
        from agent.messages import attach_message
        from base.lm.attach import AttachEntry, pack_attachments

        image = tmp_path / "render.png"
        from PIL import Image

        Image.new("RGB", (1, 1)).save(image)
        pack = pack_attachments(
            "glm-5.3-flash",
            [AttachEntry(path=str(image.resolve()), label="after fix")],
        )
        assert pack is not None
        blocks = blocks_override if blocks_override is not None else pack.blocks
        from datetime import UTC, datetime

        return attach_message(blocks=blocks, text=pack.text, created_at=datetime.now(UTC))

    def test_attach_renders_caption_only_with_image_data_uris(self, tmp_path: Path):
        msg = self._attach_message(tmp_path)
        items, _ = build_timeline_items([msg], [])
        assert len(items) == 1
        item = items[0]
        assert item.kind == "attach"
        assert item.source is None
        assert item.item_id == "0.0"
        assert item.images is not None and len(item.images) == 1
        assert item.images[0].startswith("data:image/png;base64,")
        # The image's own caption line rides beside the thumbnail (1:1 with
        # images) so the frontend can interleave label and image.
        assert item.image_captions is not None and len(item.image_captions) == 1
        assert "[1] render.png" in item.image_captions[0]
        assert "after fix" in item.image_captions[0]
        # Caption text only — the base64 must never reach the payload.
        assert "base64" not in item.payload
        assert "data:image" not in item.payload
        assert "[1] render.png" in item.payload
        assert "after fix" in item.payload

    def test_attach_without_images_has_no_images_field(self, tmp_path: Path):
        # A text-only attach (e.g. a model that cannot receive media: the pack
        # still emits a caption message listing skipped files).
        msg = self._attach_message(
            tmp_path,
            blocks_override=[
                {
                    "type": "text",
                    "text": "[system] Files attached during this turn:\n- [1] x.png (image/png) — not delivered",
                }
            ],
        )
        items, _ = build_timeline_items([msg], [])
        assert len(items) == 1
        item = items[0]
        assert item.kind == "attach"
        assert item.images is None
        assert item.image_captions is None
        assert "not delivered" in item.payload

    def test_legacy_single_caption_block_has_no_image_captions(self, tmp_path: Path):
        # Pre-interleave attach messages stored ONE caption text block followed
        # by the image blocks; per-image pairing cannot be recovered there, so
        # image_captions stays None and the frontend falls back to the legacy
        # all-text-then-all-images layout instead of mispairing.
        msg = self._attach_message(
            tmp_path,
            blocks_override=[
                {"type": "text", "text": "[system] Files attached:\n- [1] a.png (image/png)"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,QUJDRA=="},
                },
            ],
        )
        items, _ = build_timeline_items([msg], [])
        assert len(items) == 1
        item = items[0]
        assert item.kind == "attach"
        assert item.images == ["data:image/png;base64,QUJDRA=="]
        assert item.image_captions is None

    def test_multiple_images_carry_aligned_image_captions(self, tmp_path: Path):
        # Two delivered images with interleaved blocks: image_captions must be
        # the two per-file caption lines in image order, and skipped entries
        # must not shift the alignment.
        msg = self._attach_message(
            tmp_path,
            blocks_override=[
                {
                    "type": "text",
                    "text": "[system] Files attached during this turn:",
                },
                {"type": "text", "text": '- [1] first.png (image/png, 1 B) — "one"'},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,RklyU1Q="},
                },
                {"type": "text", "text": "- [2] notes.txt (unknown) — not delivered"},
                {"type": "text", "text": '- [3] second.png (image/png, 2 B) — "two"'},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,U0VDT05E"},
                },
            ],
        )
        items, _ = build_timeline_items([msg], [])
        assert len(items) == 1
        item = items[0]
        assert item.images == ["data:image/png;base64,RklyU1Q=", "data:image/png;base64,U0VDT05E"]
        assert item.image_captions == [
            '- [1] first.png (image/png, 1 B) — "one"',
            '- [3] second.png (image/png, 2 B) — "two"',
        ]
        # The joined payload keeps every line (notice + all three entries).
        assert item.payload == (
            "[system] Files attached during this turn:\n"
            '- [1] first.png (image/png, 1 B) — "one"\n'
            "- [2] notes.txt (unknown) — not delivered\n"
            '- [3] second.png (image/png, 2 B) — "two"'
        )

    def test_non_image_media_blocks_never_leak_into_payload_or_images(self, tmp_path: Path):
        # pdf document blocks + media blocks are not thumbnailable; they must be
        # ignored for images AND their bytes must not leak into the payload.
        msg = self._attach_message(
            tmp_path,
            blocks_override=[
                {"type": "text", "text": "caption"},
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": "cGVuZGluZw==",
                    },
                },
                {"type": "media", "mime_type": "video/mp4", "data": b"video-bytes"},
            ],
        )
        items, _ = build_timeline_items([msg], [])
        assert len(items) == 1
        item = items[0]
        assert item.kind == "attach"
        assert item.images is None
        assert item.payload == "caption"
        assert "cGVuZGluZw==" not in item.payload
        assert "video-bytes" not in item.payload

    def test_attach_position_in_mixed_conversation(self, tmp_path: Path):
        # The attach message lands right after the exec-output ToolMessage in
        # state.messages (exec-node drain, user ruling 2026-08-26); item ids
        # must keep absolute msg_idx alignment.
        from langchain_core.messages import AIMessage

        from agent.messages import exec_output_message

        tool_call = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "execute_code",
                    "args": {"code": "x = 1"},
                    "id": "tc-1",
                    "type": "tool_call",
                }
            ],
        )
        output = exec_output_message(content="ok", tool_call_id="tc-1")
        attach = self._attach_message(tmp_path)
        items, count = build_timeline_items([tool_call, output, attach], [])
        assert count == 3
        kinds = [it.kind for it in items]
        assert kinds == ["agent_code", "code_output", "attach"]
        assert items[2].item_id == "2.0"


class TestTimelineDispatch:
    """End-to-end dispatch chain tests: load real state.messages into PostgresSaver
    checkpoint, run the full GET /timeline endpoint, verify the returned items'
    order + kind + item_id.

    Difference from `TestAiMessageItems`: that directly feeds AIMessage to the helper,
    **bypassing the _load_langgraph_timeline_items dispatch chain** — the helper unit
    tests may all pass, but if the dispatch branch is miswired (e.g. AIMessage elif header
    accidentally deleted, embedded inside the lifecycle branch), the snapshot still doesn't
    return AIMessage content; the frontend renders as "two inbounds squeezed together +
    reasoning/chat/code floating at the end" in a scrambled order. This class prevents
    regression: the full messages list → endpoint output must be verified end-to-end.
    """

    @staticmethod
    def _put_checkpoint(agent_id: int, messages: list) -> None:
        """Directly use PostgresSaver.put to set a checkpoint with
        channel_values.messages = `messages`. Bypass the entire graph, so tests
        only care about dispatch behavior."""
        from langgraph.checkpoint.base import empty_checkpoint
        from langgraph.checkpoint.postgres import PostgresSaver

        from base.config import settings

        ckpt = empty_checkpoint()
        ckpt["channel_values"] = {"messages": messages}
        # `__start__` is a LangGraph internal channel; messages is what we care about.
        # Give both a version so PostgresSaver.put serializes messages into the blobs table.
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

    def test_full_two_turn_conversation_renders_all_blocks(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        """Main regression test: two rounds of complete conversation (inbound → AIMessage → exec_output) × 2,
        each AIMessage contains three blocks: thinking + text + tool_use. The endpoint must return
        all items in msg_idx order; the three blocks of AIMessage all go into the timeline.

        Bug scene (8a3c520): AIMessage elif header lost, all three blocks silently dropped,
        snapshot only had [inbound_1, exec_output_1, inbound_2, exec_output_2] — the partial
        reasoning/chat/code accumulated by frontend streaming landed in clientExtras floating at the end.
        """
        from langchain_core.messages import AIMessage, ToolMessage

        from agent.messages import inbound_message

        tid = create_agent(db_conn)
        # Real inbound rows as ts anchor (the timeline endpoint uses inbound_messages
        # table to estimate the ts for the inbound HumanMessage)
        insert_inbound_message(
            db_conn, tid, "msg 1", source="user", kind="chat", bus=event_bus, database=database
        )
        insert_inbound_message(
            db_conn, tid, "msg 2", source="user", kind="chat", bus=event_bus, database=database
        )
        db_conn.commit()

        # Simulate the state.messages after the graph ran two rounds — msg_idx corresponds
        # one-to-one with enumeration position
        messages = [
            inbound_message(content="envelope:msg 1", source="user", inbound_id=1),  # 0
            AIMessage(  # 1
                content=[
                    {"type": "thinking", "thinking": "thinking 1", "index": 0},
                    {"type": "text", "text": "reply 1", "index": 1},
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "execute_code",
                        "input": {"code": "print('one')"},
                        "index": 2,
                    },
                ],
                tool_calls=[
                    {"name": "execute_code", "args": {"code": "print('one')"}, "id": "call_1"}
                ],
            ),
            ToolMessage(  # 2
                content="one\n",
                tool_call_id="call_1",
                additional_kwargs={"ava_msg_type": "exec_output"},
            ),
            inbound_message(content="envelope:msg 2", source="user", inbound_id=2),  # 3
            AIMessage(  # 4
                content=[
                    {"type": "thinking", "thinking": "thinking 2", "index": 0},
                    {"type": "text", "text": "reply 2", "index": 1},
                    {
                        "type": "tool_use",
                        "id": "call_2",
                        "name": "execute_code",
                        "input": {"code": "print('two')"},
                        "index": 2,
                    },
                ],
                tool_calls=[
                    {"name": "execute_code", "args": {"code": "print('two')"}, "id": "call_2"}
                ],
            ),
            ToolMessage(  # 5
                content="two\n",
                tool_call_id="call_2",
                additional_kwargs={"ava_msg_type": "exec_output"},
            ),
        ]
        self._put_checkpoint(tid, messages)  # pyright: ignore[reportUnknownMemberType]

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        items = resp.json()["items"]

        # Complete order + item_id must strictly align with msg_idx.block_idx to match streaming SSE
        assert [(it["kind"], it["item_id"]) for it in items] == [
            ("inbound_chat", "0.0"),
            ("agent_reasoning", "1.0"),
            ("agent_chat", "1.1"),
            ("agent_code", "1.2"),
            ("code_output", "2.0"),
            ("inbound_chat", "3.0"),
            ("agent_reasoning", "4.0"),
            ("agent_chat", "4.1"),
            ("agent_code", "4.2"),
            ("code_output", "5.0"),
        ]
        assert [it["payload"] for it in items if it["kind"] == "agent_code"] == [
            "print('one')",
            "print('two')",
        ]

    def test_lifecycle_marker_does_not_swallow_following_aimessage(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        """Regression prevention (the specific refactor direction that caused the bug): the
        system_note branch must not mix in the AIMessage dispatch logic. If someone
        mistakenly embeds the _ai_message_items() call inside the system_note branch body,
        a system_note (HumanMessage) would trigger _ai_message_items's
        `assert isinstance(msg, AIMessage)` AssertionError, the entire dispatch chain
        would be silently swallowed by the outer except Exception — subsequent messages
        would all be lost.
        """
        from langchain_core.messages import AIMessage

        from agent.messages import NoteTag, inbound_message, system_note_message

        tid = create_agent(db_conn)

        insert_inbound_message(
            db_conn, tid, "before", source="user", kind="chat", bus=event_bus, database=database
        )
        db_conn.commit()

        messages = [
            inbound_message(content="before", source="user", inbound_id=1),  # 0
            system_note_message(  # 1 — system_note branch (lifecycle tag)
                content="[system] You have been restarted",
                tag=NoteTag.LIFECYCLE_RESTART,
            ),
            AIMessage(content="after restart, hello"),  # 2 — must render
        ]
        self._put_checkpoint(tid, messages)  # pyright: ignore[reportUnknownMemberType]

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        items = resp.json()["items"]

        assert [(it["kind"], it["item_id"]) for it in items] == [
            ("inbound_chat", "0.0"),
            ("system_marker", "1.0"),
            ("agent_chat", "2.0"),
        ]
        # system_note's source field exposes the note tag (frontend chip uses it)
        lifecycle = items[1]
        assert lifecycle["source"] == "lifecycle_restart"
        # AIMessage renders completely
        assert items[2]["payload"] == "after restart, hello"

    def test_system_prompt_renders_at_index_zero_and_shifts_rest(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        """SystemMessage at state.messages[0] renders end-to-end as a
        system_prompt item at "0.0"; because it occupies index 0, the following
        inbound/AIMessage item_ids shift up by one (msg_idx is 1:1 with the
        position in state.messages). Pins the full dispatch path, not just the
        build_timeline_items unit."""
        from langchain_core.messages import AIMessage, SystemMessage

        from agent.messages import inbound_message

        tid = create_agent(db_conn)
        insert_inbound_message(
            db_conn, tid, "hi", source="user", kind="chat", bus=event_bus, database=database
        )
        db_conn.commit()

        messages = [
            SystemMessage(content="You are Ava."),  # 0
            inbound_message(content="envelope:hi", source="user", inbound_id=1),  # 1
            AIMessage(content="hello back"),  # 2
        ]
        self._put_checkpoint(tid, messages)  # pyright: ignore[reportUnknownMemberType]

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert [(it["kind"], it["item_id"]) for it in items] == [
            ("system_prompt", "0.0"),
            ("inbound_chat", "1.0"),
            ("agent_chat", "2.0"),
        ]
        assert items[0]["payload"] == "You are Ava."
        assert items[0]["created_at"] is None

    def test_aimessage_string_content_renders_as_chat(
        self, db_conn: psycopg.Connection, test_client: TestClient
    ) -> None:
        """legacy / no-tools coerce path: when AIMessage.content is a string, the
        whole thing goes into agent_chat (block_idx=0). Must pass through dispatch
        to reach this path."""
        from langchain_core.messages import AIMessage

        tid = create_agent(db_conn)
        messages = [AIMessage(content="plain string reply")]
        self._put_checkpoint(tid, messages)  # pyright: ignore[reportUnknownMemberType]

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert [(it["kind"], it["item_id"], it["payload"]) for it in items] == [
            ("agent_chat", "0.0", "plain string reply"),
        ]

    def test_items_ordered_by_item_id_not_created_at(
        self, db_conn: psycopg.Connection, test_client: TestClient
    ) -> None:
        """The endpoint orders items by item_id (msg_idx.block_idx) — the logical
        append order — not by created_at. Two AIMessages whose real ava_created_at
        runs backward vs their position must still render in position order; a
        created_at sort would flip them."""
        from langchain_core.messages import AIMessage

        tid = create_agent(db_conn)
        messages = [
            AIMessage(
                content="first", additional_kwargs={"ava_created_at": "2026-06-20T10:00:00+00:00"}
            ),
            AIMessage(
                content="second", additional_kwargs={"ava_created_at": "2026-06-19T08:00:00+00:00"}
            ),
        ]
        self._put_checkpoint(tid, messages)  # pyright: ignore[reportUnknownMemberType]
        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        assert [it["item_id"] for it in resp.json()["items"]] == ["0.0", "1.0"]


def test_item_sort_key_is_numeric_not_lexical() -> None:
    """item_id ordering is numeric (msg_idx, block_idx), so "10.0" follows "2.0"
    and "3.10" follows "3.2" — a lexical sort would get both backwards."""
    from gateway.agents.timeline import _item_sort_key

    assert _item_sort_key("2.0") < _item_sort_key("10.0")
    assert _item_sort_key("3.2") < _item_sort_key("3.10")

    def test_attach_message_renders_through_full_dispatch(
        self,
        db_conn: psycopg.Connection,
        test_client: TestClient,
        tmp_path: Path,
    ) -> None:
        """End-to-end: an attach HumanMessage in the checkpoint must come back
        as a single kind=attach item with caption-only payload + image data
        URIs — not the old red system_marker with the base64 str()-d into the
        payload (Task #1668)."""
        from PIL import Image

        from agent.messages import attach_message
        from base.lm.attach import AttachEntry, pack_attachments

        image = tmp_path / "render.png"
        Image.new("RGB", (2, 2)).save(image)
        pack = pack_attachments(
            "glm-5.3-flash",
            [AttachEntry(path=str(image.resolve()), label="brand")],
        )
        assert pack is not None
        from datetime import UTC, datetime

        attach = attach_message(blocks=pack.blocks, text=pack.text, created_at=datetime.now(UTC))
        tid = create_agent(db_conn)
        self._put_checkpoint(tid, [attach])  # pyright: ignore[reportUnknownMemberType]

        resp = test_client.get(f"/api/agents/{tid}/timeline")
        assert resp.status_code == 200
        body = resp.json()
        assert body["msg_count"] == 1
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item["kind"] == "attach"
        assert item["source"] is None
        assert "[1] render.png" in item["payload"]
        assert "base64" not in item["payload"]
        assert "data:image/png;base64," in item["images"][0]
