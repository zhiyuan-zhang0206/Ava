"""In-memory system-note injection for the exec node (user ruling 2026-08-11).

AGENTS.md / CLAUDE.md context notes and exec-turn prompt-injection findings
are delivered inside the exec's own messages delta, not through a side-channel
file read by a later hook. Two in-memory sources feed the delta:

1. Security findings: ava.security buffers them in the exec child during the
   turn (scan_content runs inside the agent's SDK calls) and the child drains
   them into its result envelope; the exec node reads them from there. The
   agent host keeps no findings buffer — findings on inbound content are the
   claim node's, appended in claim's own delta behind the flagged message.
2. Plugin-contributed messages: ava_code (AGENTS.md/CLAUDE.md context notes)
   writes them to the base `messages` channel via PluginStateHandle during
   the turn; the exec node pops them out of the plugin state update (a dict
   **spread would otherwise let them clobber the exec ToolMessage) and merges
   them into the same delta.

Both land AFTER the exec-result ToolMessage on purpose: the Anthropic-compat
wire contract requires an AIMessage's tool_use to be immediately followed by
its tool_result, so a note sandwiched between the AIMessage and the
ToolMessage is rejected with a 400 (empirically verified against the DeepSeek
anthropic endpoint, 2026-08-11: "tool_use ids were found without tool_result
blocks immediately after") and would also trip the dangling-tool_use repair
hook (agent/hooks/repair.py) into synthesizing a fake [interrupted] result
every turn. Security warnings precede the context-file notes they annotate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from agent.messages import security_note_message
from ava.security import SecurityFindingEntry
from base.config import settings


def merge_exec_notes(
    state_messages_update: list[AnyMessage],
    plugin_messages: list[AnyMessage] | None,
    findings: list[SecurityFindingEntry],
) -> list[AnyMessage]:
    """Merge in-memory system notes into the exec's messages delta.

    Security-warning notes (when scanning is enabled) precede the plugin's
    context notes. The exec node defers notes until all tool results commit,
    preserving tool_use -> tool_result adjacency (see module docstring).

    Args:
        state_messages_update: this call's exec-result ToolMessage, or notes
            already deferred in `pending_exec_notes` by earlier calls.
        plugin_messages: messages the plugin wrote to the base `messages`
            channel this turn (context-file notes), or None.
        findings: security findings the exec child drained from ava.security's
            in-memory buffer (result envelope), or [].
    """
    if findings and settings.agent.security_scan_enabled:
        notes = [
            security_note_message(
                source=entry.source, triggers=entry.triggers, created_at=datetime.now(UTC)
            )
            for entry in findings
        ]
        # cast: add_messages declares list[MessageLikeRepresentation]
        # (invariant); the deltas are list[AnyMessage], which is what the
        # checkpoint channel actually holds.
        state_messages_update = cast(
            list[AnyMessage],
            add_messages(cast(Any, state_messages_update), cast(Any, notes)),
        )
    if plugin_messages is not None:
        state_messages_update = cast(
            list[AnyMessage],
            add_messages(cast(Any, state_messages_update), cast(Any, plugin_messages)),
        )
    return state_messages_update
