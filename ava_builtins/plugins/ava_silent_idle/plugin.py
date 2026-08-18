"""Silent-idle continue nudge — push the agent off a reasoning-only turn.

A "silent idle" is a turn where the model produced reasoning (thinking blocks /
reasoning tokens) but emitted no text and no tool_call: the agent appears stuck
at reasoning. The kernel (agent/graph/_llm.py) handles such a turn by keeping
the reasoning in context and looping straight back to the LLM
(halted=False -> claim's multi-step continue path) instead of wasting tokens on
a blind re-stream, bounded by a per-process consecutive-count guard.

This plugin contributes the *nudge*: a before_llm hook that, when the message
tail is the reasoning-only AIMessage the kernel just committed, injects one
system_note_message tagged SILENT_IDLE_CONTINUE telling the agent to produce
text or a tool_call this turn (or state completion in text).

Why message-tail detection (no plugin state):
- The kernel does halted=False ONLY for a silent idle, and that is the only
  path that loops back to before_llm with a no-text / no-tool_call AIMessage as
  the tail. Every other path that could leave such a message either appends a
  ToolMessage (exec output) or halts to idle and appends an inbound before the
  next before_llm. So a tail AIMessage with neither text nor tool_calls, seen
  at before_llm, uniquely means "the previous turn was a silent idle".
- It is naturally one-shot: after this hook appends the nudge HumanMessage, the
  tail is no longer that AIMessage, so it does not re-fire. A second silent idle
  commits a fresh reasoning AIMessage as the new tail and earns its own nudge.

Removability: disabling this plugin removes only the nudge text. The kernel
still continue-loops and still guard-halts after the cap; the agent just relies
on seeing its own reasoning to act, with no explicit prompt.

compact clobber-safety: auto-compact is also a before_llm hook. `messages`
carries the add_messages reducer, so co-writing it merges rather than
fail-louding; but auto-compact's full-history REMOVE_ALL replacement is
order-sensitive and would swallow a note appended in the same pass. So when
auto-compact would fire this same turn, this hook defers (returns None) rather
than racing the history replacement; the kernel's continue-loop and guard still
apply, and the nudge is simply skipped for that turn.
"""

from __future__ import annotations

__description__ = "Inject a Continue nudge after a silent-idle turn (model reasoned but emitted no text and no tool_call)"

from datetime import UTC, datetime

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph._context import AvaContext
from agent.hooks import Hook, register_before_llm
from agent.hooks.compact import auto_compact_will_fire
from agent.messages import NoteTag, system_note_message
from agent.state import AgentState
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
