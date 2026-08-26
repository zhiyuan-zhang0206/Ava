"""Packing of pending attachments into one HumanMessage.

Two call sites share the pack:

- the exec node (`agent/graph/_exec.py`) drains the attachments registered
  during the just-finished ``execute_code`` call **immediately**, so the media
  message lands right after the exec-output ToolMessage in the same turn and
  the model can use the files on its very next step (user ruling 2026-08-26);
- the claim node keeps a fallback drain for edge paths that skip the exec
  update (compact halt, a crashed exec), appending at the turn boundary as
  before.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import HumanMessage

from agent.messages import attach_message
from agent.state import AttachState, BaseAgentState
from shared.config.turn_view import turn_settings
from shared.context import AvaContext
from shared.lm.attach import AttachEntry, pack_attachments


def build_attach_message(pending: AttachState, model: str) -> HumanMessage | None:
    """Pack one attach message from pending attachments, or None when empty.

    The message's first content block is the caption text (listing every file,
    delivered or skipped); following blocks carry the provider-native media.
    """
    if not pending.pending:
        return None
    entries = [AttachEntry(path=entry.path, label=entry.label) for entry in pending.pending]
    pack = pack_attachments(model, entries)
    if pack is None:
        return None
    return attach_message(blocks=pack.blocks, text=pack.text, created_at=datetime.now(UTC))


def build_attach_drain(state: BaseAgentState, ctx: AvaContext) -> dict[str, Any] | None:
    """Build a tail-message update for files pending at a completed turn boundary.

    Fallback path only: the exec node normally drains attachments in its own
    update (see module docstring); this survives edge paths where the exec
    update never ran (compact halt / crashed exec).
    """
    model = getattr(ctx.llm, "model_name", None) or turn_settings.lm.llm_model
    message = build_attach_message(state.attach, model)
    if message is None:
        return None
    return {"messages": [message], "attach": AttachState()}
