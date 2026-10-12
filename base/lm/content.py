"""The content-block shape an AIMessage(Chunk).content list carries.

langchain types `BaseMessage.content` as `str | list[str | dict[str, Any]]` —
the block dicts lose their shape at that boundary, so every consumer that walks
the list re-derives the same `.get("type")` / `.get("text")` / `.get("thinking")`
protocol by hand. `ContentBlock` names that shape once (all fields optional: a
block carries only the keys its `type` implies) and `content_blocks` is the
single seam that re-labels the list arm so those reads are typed.

The typed accessors for the other loosely-typed members of a LangChain message
(`message_content`, `message_addl_kwargs`, `message_response_metadata`) live here
too: the same narrowing, one level up.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict, cast

if TYPE_CHECKING:
    # Annotation-only: this leaf sits on the provider-registration import path,
    # which must stay off the LangChain message stack (exec-child boot, task
    # #3633; `_TYPE_CHECKING_ALLOWED`).
    from langchain_core.messages import BaseMessage


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


# ── Typed accessors for the loosely-typed LangChain message members ──
#
# LangChain types `content` blocks and `additional_kwargs` / `response_metadata`
# as bare `dict` (Unknown keys), which propagates Unknown into every reader. These
# three accessors are the single narrowing points — one scoped ignore each, so the
# call sites in timeline / stop / reasoning-compat / labeler stay annotation-clean.


def message_content(msg: BaseMessage) -> str | list[str | dict[str, Any]]:
    """`msg.content` with its content-list block dicts retyped `dict[str, Any]`."""
    # langchain types content-list blocks as bare dict, whose Unknown keys leak.
    return cast("str | list[str | dict[str, Any]]", msg.content)  # pyright: ignore[reportUnknownMemberType]


def message_addl_kwargs(msg: BaseMessage) -> dict[str, Any]:
    """`msg.additional_kwargs` as a plain `dict[str, Any]` (langchain types it bare)."""
    return cast("dict[str, Any]", msg.additional_kwargs)  # pyright: ignore[reportUnknownMemberType]


def message_response_metadata(msg: BaseMessage) -> dict[str, Any]:
    """`msg.response_metadata` as a plain `dict[str, Any]` (langchain types it bare)."""
    return cast("dict[str, Any]", msg.response_metadata)  # pyright: ignore[reportUnknownMemberType]
