"""Validated upload references shared by pending and impersonation timelines."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

# Suffix -> MIME for image uploads that can be inlined as native model content.
# Mirrors the image row of `meiva._understand`'s media map; a suffix not listed
# here is not treated as an inlineable image (the message endpoint 422s a
# reference to it, the claim node never base64-inlines it).
IMAGE_MIME: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def parse_upload_url(url: str) -> tuple[int, str] | None:
    """Split an upload url into (agent_id, name), or None if it is not the
    ``/api/agents/<int>/uploads/<name>`` shape. Does not check the file exists
    or that `name` is safe — callers pair this with `resolve_upload_path`."""
    prefix = "/api/agents/"
    if not url.startswith(prefix):
        return None
    rest = url[len(prefix) :]
    agent_part, sep, name = rest.partition("/uploads/")
    if not sep or not name or not agent_part.isdigit():
        return None
    return int(agent_part), name


def image_mime_for(name: str) -> str | None:
    """MIME type for an image upload name, or None if the suffix is not an image."""
    return IMAGE_MIME.get(Path(name).suffix.lower())


def inbound_image_urls(agent_id: int, payload: dict[str, Any] | None) -> list[str] | None:
    """Image reference urls of a stored multimodal inbound, or None.

    The stored `content_blocks` mirror the POST body; a block counts as a
    renderable image only when it passes the same gate `_validate_image_ref`
    (gateway/agents/state.py) applied at write time — one of this
    agent's upload urls carrying a recognized image suffix. That keeps the
    strip's thumbnails on the exact url contract the timeline uses
    (`meiva_image_urls`), never a path the browser cannot load. Malformed
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
