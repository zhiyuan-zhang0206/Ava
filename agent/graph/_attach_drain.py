"""Turn-boundary packing of checkpointed attachments into one HumanMessage."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agent.messages import attach_message
from agent.state import AttachState, BaseAgentState
from shared.config.turn_view import turn_settings
from shared.context import AvaContext
from shared.lm.attach import AttachEntry, pack_attachments


def build_attach_drain(state: BaseAgentState, ctx: AvaContext) -> dict[str, Any] | None:
    """Build a tail-message update for files pending at a completed turn boundary."""
    if not state.attach.pending:
        return None
    model = getattr(ctx.llm, "model_name", None) or turn_settings.lm.llm_model
    entries = [AttachEntry(path=entry.path, label=entry.label) for entry in state.attach.pending]
    pack = pack_attachments(model, entries)
    if pack is None:
        return None
    message = attach_message(blocks=pack.blocks, text=pack.text, created_at=datetime.now(UTC))
    return {"messages": [message], "attach": AttachState()}
