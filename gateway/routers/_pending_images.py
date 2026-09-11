"""Image reference urls for the pending strip (gateway/routers/agents_state.py).

The strip above the composer renders thumbnails for queued multimodal
messages; deciding which references are renderable is a pure function of the
stored content-block payload. Split out of agents_state.py so the router
module stays under the 800-line budget (same shape as _roster_rows.py), and
so the reference gate lives in one named place.
"""

from __future__ import annotations

from typing import Any, cast

from shared.uploads import image_mime_for, parse_upload_url


def pending_image_urls(agent_id: int, payload: dict[str, Any] | None) -> list[str] | None:
    """Image reference urls of a queued multimodal inbound, or None.

    The stored `content_blocks` mirror the POST body; a block counts as a
    renderable image only when it passes the same gate `_validate_image_ref`
    (gateway/routers/agents_state.py) applied at write time — one of this
    agent's upload urls carrying a recognized image suffix. That keeps the
    strip's thumbnails on the exact url contract the timeline uses
    (`ava_image_urls`), never a path the browser cannot load. Malformed
    blocks are skipped, not fatal: a queue read degrades a row rather than
    failing the whole strip.
    """
    raw_blocks = payload.get("content_blocks") if payload else None
    if not isinstance(raw_blocks, list):
        return None
    urls: list[str] = []
    for raw_block in cast("list[Any]", raw_blocks):
        if not isinstance(raw_block, dict):
            continue
        block = cast("dict[str, Any]", raw_block)
        if block.get("type") != "image_url":
            continue
        image_url = block.get("image_url")
        if not isinstance(image_url, dict):
            continue
        url = cast("dict[str, Any]", image_url).get("url")
        if not isinstance(url, str):
            continue
        parsed = parse_upload_url(url)
        if parsed is None:
            continue
        ref_agent, name = parsed
        if ref_agent != agent_id or image_mime_for(name) is None:
            continue
        urls.append(url)
    return urls or None
