"""The conversation excerpt a reply suggestion is judged and drafted from.

It holds the user's latest message and the text the agent wrote in its last turn, cut from the
end backwards to a token budget so the newest words always survive.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from base.agents.history.timeline import TimelineItem
from base.lm.catalog.decide.types import estimate_tokens

_USER = "User"
_ASSISTANT = "Assistant"
_ELLIPSIS = "…"
_SEPARATOR = "\n\n"


@dataclass(frozen=True)
class ConversationExcerpt:
    """`text` fits the token budget; `last_message_id` identifies the agent text it answers."""

    text: str
    last_message_id: str


def is_human_source(source: str | None) -> bool:
    """Whether an inbound came from a person: `user` or a UI page post.

    Mirrors `inboundKind` in `ui/web/src/components/timeline/model/runs.ts`.
    """
    return source is not None and (source == "user" or source.startswith("ui:page:"))


def build_excerpt(items: Sequence[TimelineItem], max_tokens: int) -> ConversationExcerpt | None:
    """The excerpt of `items`, or None when the agent wrote no text in its last turn.

    The last turn is everything after the newest inbound of any source; the user's message is
    the newest human inbound anywhere before the end. The estimate is `estimate_tokens`, the
    same conservative count the decision call checks its input with.
    """
    if max_tokens <= 0:
        raise ValueError(f"max_tokens must be positive, got {max_tokens}")
    agent_items = _last_turn_agent_text(items)
    if not agent_items:
        return None
    parts: list[tuple[str, str]] = []
    user_text = _latest_human_text(items)
    if user_text is not None:
        parts.append((_USER, user_text))
    parts.append((_ASSISTANT, _SEPARATOR.join(item.payload.strip() for item in agent_items)))
    text = _fit(parts, max_tokens)
    if not text:
        return None
    newest = agent_items[-1]
    identity = newest.source_message_id or f"{newest.item_id}@{newest.created_at}"
    return ConversationExcerpt(text, identity)


def _last_turn_agent_text(items: Sequence[TimelineItem]) -> list[TimelineItem]:
    last_inbound = max(
        (i for i, item in enumerate(items) if item.kind == "inbound_chat"), default=-1
    )
    return [
        item
        for item in items[last_inbound + 1 :]
        if item.kind == "agent_chat" and item.payload.strip()
    ]


def _latest_human_text(items: Sequence[TimelineItem]) -> str | None:
    for item in reversed(items):
        if item.kind == "inbound_chat" and is_human_source(item.source) and item.payload.strip():
            return item.payload.strip()
    return None


def _fit(parts: Sequence[tuple[str, str]], budget: int) -> str:
    """Join labelled parts newest-first into `budget` tokens; the first part that overflows is
    cut to its tail and ends the walk."""
    kept: list[str] = []
    remaining = budget
    for label, text in reversed(parts):
        header = f"[{label}]\n"
        room = remaining - estimate_tokens(header) - estimate_tokens(_SEPARATOR)
        if room <= 0:
            break
        cut = estimate_tokens(text) > room
        body = _ELLIPSIS + _tail(text, room - estimate_tokens(_ELLIPSIS)) if cut else text
        block = header + body
        kept.append(block)
        remaining -= estimate_tokens(block) + estimate_tokens(_SEPARATOR)
        if cut:
            break
    return _SEPARATOR.join(reversed(kept))


def _tail(text: str, tokens: int) -> str:
    """The longest suffix of `text` whose estimate is at most `tokens`."""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[-middle:]) <= tokens:
            low = middle
        else:
            high = middle - 1
    return text[len(text) - low :] if low else ""
