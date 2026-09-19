"""Agent-runtime face of the ava_silent_idle plugin — the silent-idle continue hook.

A before_llm hook that injects a Continue nudge after a silent-idle turn.
Loaded only in the agent process: `agent._extensions` imports this
module after `plugin.py` on the full path (host boot / graph build); the
surface module carries only the description (task #3633).
"""

from __future__ import annotations

from datetime import UTC, datetime

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.hooks import Hook, register_before_llm
from agent.hooks.compact import auto_compact_will_fire
from agent.messages import NoteTag, system_note_message
from agent.state import AgentState
from shared.context import AvaContext
from shared.log import logger

_NUDGE = "The previous turn produced reasoning but no output. You must now produce either text or a tool call. If your task is complete, state so in text — do not end a turn with reasoning alone."


def _tail_is_silent_idle(messages: list) -> bool:
    """True when the last message is a reasoning-only AIMessage — no text and no
    tool_call. That is exactly the message the kernel commits on a silent-idle
    continue-loop (see module docstring for why the tail check is sufficient)."""
    if not messages:
        return False
    last = messages[-1]
    return isinstance(last, AIMessage) and not last.text and not last.tool_calls


class _SilentIdleContinueHook(Hook):
    """Inject a one-time Continue note when the previous turn was a silent idle.

    No-op (returns None) when the message tail is not a reasoning-only AIMessage,
    or when auto-compact would replace messages this same turn (deferring avoids
    two before_llm hooks writing `messages` in one pass).
    """

    async def __call__(
        self,
        state: AgentState,
        _runtime: Runtime[AvaContext],
        _config: RunnableConfig,
        /,
    ) -> dict | None:
        if not _tail_is_silent_idle(state.messages):
            return None

        if auto_compact_will_fire(state):
            logger.info(
                "[{label}] {body}",
                label="silent-idle",
                event="silent_idle",
                body="defer: auto-compact predicted, skipping continue nudge this turn",
            )
            return None

        return {
            "messages": [
                system_note_message(
                    content=_NUDGE,
                    tag=NoteTag.SILENT_IDLE_CONTINUE,
                    created_at=datetime.now(UTC),
                )
            ]
        }


silent_idle_continue_before_llm = _SilentIdleContinueHook()
register_before_llm(silent_idle_continue_before_llm)
