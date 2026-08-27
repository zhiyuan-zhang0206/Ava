"""Build the HumanMessage for a kind='chat' inbound row.

Split out of `_claim.py` so the multimodal image-inlining path lives in one
focused place: text inbounds envelope-wrap into a string message; multimodal
inbounds wrap the text part into a leading block and inline each referenced
image upload as a native base64 block the model decodes.
"""

from __future__ import annotations

from typing import Any, cast

from langchain_core.messages import HumanMessage

from agent.db import ClaimedInbound
from agent.messages import inbound_message
from ava._commands import expand_command
from ava.security import scan_content
from shared.config import settings
from shared.envelope import wrap_inbound
from shared.log import logger
from shared.uploads import fetch_upload_b64, parse_upload_url


def build_chat_inbound(item: ClaimedInbound) -> HumanMessage:
    """Build the HumanMessage for a kind='chat' inbound row.

    Plain text (no content_blocks payload): a `/<name> ...` composer message is
    expanded here — every command in it, in the order typed — into each
    command's template + the user's note, then envelope-wrapped into a string
    message. Multimodal (payload carries content_blocks):
    the text part is wrapped into a leading text block, then each referenced
    image upload is read off disk and inlined as a native base64 image block the
    model decodes; the reference urls ride on additional_kwargs for the timeline.

    An image that cannot be read (deleted between send and claim, or a split
    deployment where the upload lives on the gateway not this runner) degrades to
    a "[image unavailable]" text note rather than crashing the claim — the miss
    is logged, the rest of the message still delivers.
    """
    # Inbound chat is untrusted (a user message, or a peer agent that may itself
    # be injected), so its text is scanned before entering the conversation. The
    # scan runs on the *raw* content before envelope wrapping — the envelope
    # framing is trusted text (e.g. `[system]` prefix added by wrap_inbound for
    # system-sourced messages) and must not trip the scan itself; image blocks
    # are binary and not scanned.
    scan_src = f"inbound.chat:{item.source}"
    raw_blocks = item.payload.get("content_blocks") if item.payload else None
    if not isinstance(raw_blocks, list):
        raw = expand_command(item.content)
        if settings.agent.security_scan_enabled:
            raw = scan_content(raw, source=scan_src)
        wrapped = wrap_inbound(raw, item.source, created_at=item.created_at)
        return inbound_message(
            content=wrapped,
            source=item.source,
            inbound_id=item.id,
            created_at=item.created_at,
        )
    blocks = cast("list[dict[str, Any]]", raw_blocks)

    text = "\n".join(b["text"] for b in blocks if b.get("type") == "text")
    raw_text = expand_command(text)
    if settings.agent.security_scan_enabled:
        raw_text = scan_content(raw_text, source=scan_src)
    wrapped_text = wrap_inbound(raw_text, item.source, created_at=item.created_at)
    content: list[dict[str, Any]] = [{"type": "text", "text": wrapped_text}]
    image_urls: list[str] = []
    for b in blocks:
        if b.get("type") != "image_url":
            continue
        url = b["image_url"]["url"]
        parsed = parse_upload_url(url)
        if parsed is None:
            content.append({"type": "text", "text": f"[image reference {url!r} unreadable]"})
            continue
        ref_agent, name = parsed
        try:
            mime, b64 = fetch_upload_b64(ref_agent, name)
        except (OSError, ValueError) as e:
            logger.warning(
                "claim: image upload {!r} unavailable (agent {}): {!r}", name, item.agent_id, e
            )
            content.append({"type": "text", "text": f"[image {name} unavailable]"})
            continue
        # Use standard image_url format (data URI) so the block is
        # decodable by Claude, Gemini, and GPT providers alike.
        # Anthropic-native {"type":"image","source":{...}} only works
        # with langchain-anthropic; langchain-google-genai and
        # langchain-openai expect {"type":"image_url","image_url":{...}}.
        data_uri = f"data:{mime};base64,{b64}"
        content.append({"type": "image_url", "image_url": {"url": data_uri}})
        image_urls.append(url)
    return inbound_message(
        content=content,
        source=item.source,
        inbound_id=item.id,
        created_at=item.created_at,
        image_urls=image_urls,
    )
