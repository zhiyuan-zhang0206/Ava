"""agent/graph/_claim.py:_build_chat_inbound — text vs multimodal delivery.

Pure unit over the message builder (no DB / no graph run). Verifies a plain
chat inbound still becomes an envelope-wrapped string HumanMessage, and a
multimodal inbound becomes a list message that inlines the referenced image as
a native base64 block while carrying the reference url on additional_kwargs for
the timeline. A missing image degrades to a text note instead of crashing.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from langchain_core.messages import HumanMessage

from agent.db import ClaimedInbound
from agent.graph._chat_inbound import build_chat_inbound


def _blocks(msg: HumanMessage) -> list[dict[str, Any]]:
    """The message's content as a list of blocks (langchain types it broadly as
    str | list[str | dict]; a multimodal inbound is always the dict-list case)."""
    assert isinstance(msg.content, list)  # pyright: ignore[reportUnknownMemberType]
    return cast("list[dict[str, Any]]", msg.content)  # pyright: ignore[reportUnknownMemberType]


def _inbound(content: str, payload: dict | None = None) -> ClaimedInbound:
    return ClaimedInbound(
        id=1,
        agent_id=7,
        content=content,
        kind="chat",
        source="user",
        payload=payload,
        created_at=datetime(2026, 6, 30, 12, 0, 0, tzinfo=UTC),
    )


def _stub_upload_fetch(monkeypatch: pytest.MonkeyPatch, raw: bytes) -> dict[str, list[str]]:
    """Stand in for the gateway upload endpoint: fetch_upload_b64's HTTP GET
    returns `raw` (mime derived from the filename). Returns the seen URLs."""
    from shared import http_dial

    seen: dict[str, list[str]] = {"urls": []}

    def _fake_get(url: str, **kwargs: object) -> object:
        seen["urls"].append(url)

        class _Resp:
            content = raw
            status_code = 200

            def raise_for_status(self) -> None:
                return None

        return _Resp()

    monkeypatch.setattr(http_dial, "get", _fake_get)
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw.test:8000")
    return seen


def test_plain_text_inbound_is_string_message() -> None:
    msg = build_chat_inbound(_inbound("hello there"))
    assert isinstance(msg.content, str)  # pyright: ignore[reportUnknownMemberType]
    assert "hello there" in msg.content
    assert msg.additional_kwargs["ava_msg_type"] == "inbound"  # pyright: ignore[reportUnknownMemberType]
    assert "ava_image_urls" not in msg.additional_kwargs  # pyright: ignore[reportUnknownMemberType]


def test_multimodal_inbound_inlines_base64_and_keeps_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b"\x89PNG\r\n\x1a\n" + b"pixels"
    seen = _stub_upload_fetch(monkeypatch, raw)
    payload = {
        "content_blocks": [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": "/api/agents/7/uploads/shot.png"}},
        ]
    }
    msg = build_chat_inbound(_inbound("what is this?", payload))
    # The image is fetched from the gateway over HTTP — one uniform path,
    # never the claim node's local disk.
    assert seen["urls"] == ["http://gw.test:8000/api/agents/7/uploads/shot.png"]

    text_block, image_block = _blocks(msg)
    assert text_block["type"] == "text"
    assert "what is this?" in text_block["text"]  # envelope-wrapped
    # Standard image_url data URI format (decodable by Claude, Gemini, GPT alike).
    assert image_block["type"] == "image_url"
    data_uri = cast("str", image_block["image_url"]["url"])
    assert data_uri.startswith("data:image/png;base64,")
    assert base64.standard_b64decode(data_uri.removeprefix("data:image/png;base64,")) == raw
    # The reference url rides on additional_kwargs for the timeline thumbnail —
    # never the base64.
    assert msg.additional_kwargs["ava_image_urls"] == ["/api/agents/7/uploads/shot.png"]  # pyright: ignore[reportUnknownMemberType]


def test_image_only_message_has_empty_wrapped_text_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_upload_fetch(monkeypatch, b"\x89PNG\r\n\x1a\nx")
    payload = {
        "content_blocks": [
            {"type": "image_url", "image_url": {"url": "/api/agents/7/uploads/a.png"}},
        ]
    }
    msg = build_chat_inbound(_inbound("[image]", payload))
    blocks = _blocks(msg)
    # A leading (possibly envelope-only) text block, then the image.
    assert blocks[0]["type"] == "text"
    assert blocks[1]["type"] == "image_url"
    assert msg.additional_kwargs["ava_image_urls"] == ["/api/agents/7/uploads/a.png"]  # pyright: ignore[reportUnknownMemberType]


def test_missing_image_degrades_to_text_note(monkeypatch: pytest.MonkeyPatch) -> None:
    # The gateway answers 404 (deleted between send and claim): fetch_upload_b64
    # raises OSError, the claim node degrades the block to a text note.
    from shared import http_dial

    def _fake_get(url: str, **kwargs: object) -> object:
        class _Resp:
            content = b""
            status_code = 404

            def raise_for_status(self) -> None:
                import httpx

                raise httpx.HTTPStatusError(
                    "404",
                    request=None,  # type: ignore[arg-type]
                    response=None,  # type: ignore[arg-type]
                )

        return _Resp()

    monkeypatch.setattr(http_dial, "get", _fake_get)
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw.test:8000")
    payload = {
        "content_blocks": [
            {"type": "text", "text": "see this"},
            {"type": "image_url", "image_url": {"url": "/api/agents/7/uploads/gone.png"}},
        ]
    }
    msg = build_chat_inbound(_inbound("see this", payload))
    blocks = _blocks(msg)
    # No image block; the missing image became a text note, delivery survived.
    assert all(b["type"] == "text" for b in blocks)
    assert any("unavailable" in b["text"] for b in blocks)
    # No image was successfully inlined → the urls key is omitted entirely.
    assert "ava_image_urls" not in msg.additional_kwargs  # pyright: ignore[reportUnknownMemberType]


def test_command_chain_expands_inside_the_one_message() -> None:
    """Several commands in one inbound expand into that same message, in the
    order typed — the whole composite instruction reaches the model at once
    instead of arriving as unrelated turns."""
    msg = build_chat_inbound(_inbound("/recap the week /plan the migration"))
    assert isinstance(msg.content, str)  # pyright: ignore[reportUnknownMemberType]
    assert msg.content.index("Command /recap:") < msg.content.index("Command /plan:")
    assert "Additional message: the week" in msg.content
    assert "Additional message: the migration" in msg.content


def test_lifecycle_command_chains_like_any_other() -> None:
    """`/compact` gets no special handling on the way in. Its body tells the
    agent to replace its own context, so a following instruction may well lapse
    — that is the prompt's meaning, worked out by the agent reading it, not
    something the claim node predicts or blocks."""
    msg = build_chat_inbound(_inbound("/compact /recap"))
    assert isinstance(msg.content, str)  # pyright: ignore[reportUnknownMemberType]
    assert "Command /compact:" in msg.content
    assert "Command /recap:" in msg.content
