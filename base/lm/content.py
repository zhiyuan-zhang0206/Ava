"""The content-block shape an AIMessage(Chunk).content list carries.

langchain types `BaseMessage.content` as `str | list[str | dict[str, Any]]` —
the block dicts lose their shape at that boundary, so every consumer that walks
the list re-derives the same `.get("type")` / `.get("text")` / `.get("thinking")`
protocol by hand. `ContentBlock` names that shape once (all fields optional: a
block carries only the keys its `type` implies) and `content_blocks` is the
single seam that re-labels the list arm so those reads are typed.
"""

from __future__ import annotations

from typing import Any, TypedDict


class ContentBlock(TypedDict, total=False):
    """One provider content block of an AIMessage(Chunk).content list, as the LLM
    layer sees it after langchain parsing.

    Every field is NotRequired: a block carries only the keys its `type` implies
    — a text block has `text`; a thinking block has `thinking` and (on a
    signature_delta) `signature` but no `thinking`; an openai reasoning block has
    `summary`; a tool_use block has `id`/`name`/`input` or a streaming
    `partial_json`. `index` places the block in the content-block offset that
    aligns tool-call code items with the committed snapshot.
    """

    type: str
    text: str
    thinking: str
    signature: str
    index: int
    summary: list[dict[str, Any]]
    id: str
    name: str
    input: dict[str, Any]
    partial_json: str


def content_blocks(content: list[Any]) -> list[str | ContentBlock]:
    """Re-label the list arm of an AIMessage(Chunk).content as `str | ContentBlock`
    so a `for b in content_blocks(content): if isinstance(b, dict): b.get(...)`
    walk reads typed fields. Identity at runtime — langchain already hands us the
    str / dict blocks; this only attaches the `ContentBlock` shape to the dicts.
    """
    return content
