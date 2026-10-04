"""In-memory system-note injection for the exec node (user ruling 2026-08-11).

AGENTS.md / CLAUDE.md context notes are delivered inside the exec's own messages delta, not
through a side-channel file read by a later hook: ava_code writes them to the base `messages`
channel via PluginStateHandle during the turn; the exec node pops them out of the plugin state
update (a dict **spread would otherwise let them clobber the exec ToolMessage) and merges them
into the same delta.

Prompt-injection findings are not merged here. The exec child appends them to the state update
(`state.security_findings`) and the framework's after_exec hook delivers them
(`agent/hooks/security.py`); findings on inbound content are the claim node's, appended in
claim's own delta behind the flagged message.

The notes land AFTER the exec-result ToolMessage on purpose: the Anthropic-compat wire contract
requires an AIMessage's tool_use to be immediately followed by its tool_result, so a note
sandwiched between the AIMessage and the ToolMessage is rejected with a 400 (empirically verified
against the DeepSeek anthropic endpoint, 2026-08-11: "tool_use ids were found without tool_result
blocks immediately after") and would also trip the dangling-tool_use repair hook
(agent/hooks/repair.py) into synthesizing a fake [interrupted] result every turn.
"""

from __future__ import annotations

from typing import Any, cast

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


def merge_exec_notes(
    state_messages_update: list[AnyMessage],
    plugin_messages: list[AnyMessage] | None,
) -> list[AnyMessage]:
    """Merge the plugin's in-memory system notes into the exec's messages delta.

    The exec node defers notes until all tool results commit, preserving
    tool_use -> tool_result adjacency (see module docstring).

    Args:
        state_messages_update: this call's exec-result ToolMessage, or notes
            already deferred in `pending_exec_notes` by earlier calls.
        plugin_messages: messages the plugin wrote to the base `messages`
            channel this turn (context-file notes), or None.
    """
    if plugin_messages is not None:
        state_messages_update = cast(
            list[AnyMessage],
            add_messages(cast(Any, state_messages_update), cast(Any, plugin_messages)),
        )
    return state_messages_update
