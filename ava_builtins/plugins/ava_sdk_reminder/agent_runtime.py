"""Agent-runtime face of the ava_sdk_reminder plugin — state fields and hooks.

Loaded only in the agent process: `agent.extensions` imports this module after
`plugin.py` on the full path (host boot / graph build). The plugin has no
SDK-visible surface — everything it does is agent-runtime behavior — so the
surface module carries only the description (task #3633).
"""

from __future__ import annotations

import builtins
import keyword
import re
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph.tool_calls import first_tool_call_code
from agent.hooks import Hook
from agent.hooks.compact import auto_compact_will_fire
from agent.messages import NoteTag, system_note_message, tail_has_agent_inbound
from agent.state import AgentState
from base.agents.context import AvaContext
from base.agents.messages.kwargs import message_content
from base.host.env.agent_slices import Cadence
from base.log import logger
from base.packages.plugins.extensions import PluginContributions

from ._state import (
    AGENT_REPLY_CATEGORY,
    AGENT_REPLY_HINT,
    AvaSdkReminderState,
    detect_categories,
    hint_for,
    mentions_watcher,
)

# Channel keys for this plugin's state fields (prefixed by the framework from the
# declared state class). A hook node reads/writes these directly on `state`
# because the in-turn handle (state_handle.read/update) is not wired up here.
_REMINDED_FIELD = "ava_sdk_reminder__reminded"
_BOOKMARK_FIELD = "ava_sdk_reminder__last_seen_compact"
_NAMEERROR_CATEGORY_PREFIX = "nameerror:"
_NAMEERROR_RE = re.compile(r"(?:^|\n)NameError: name '([^'\n]+)' is not defined\b")


def _assumed_persistence_name(
    previous_messages: list[AnyMessage], out_msg: ToolMessage
) -> str | None:
    """Return the undefined name only when it appeared in an earlier code cell."""
    output = message_content(out_msg)
    if not isinstance(output, str):
        return None
    match = _NAMEERROR_RE.search(output)
    if match is None:
        return None

    name = match.group(1)
    if not name.isidentifier() or keyword.iskeyword(name) or name in vars(builtins):
        return None

    whole_name = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
    for message in reversed(previous_messages):
        if not isinstance(message, AIMessage) or not message.tool_calls:
            continue
        for call in message.tool_calls:
            if call["name"] == "execute_code" and whole_name.search(first_tool_call_code([call])):
                return name
    return None


def _nameerror_persistence_hint(name: str) -> str:
    return (
        f"NameError: '{name}' appeared in an earlier execute_code call, "
        "but each call runs in a fresh interpreter — variables do not persist "
        "between calls. Re-define it here, or carry state via files or shell sessions."
    )


def _rearmed_reminded(state: AgentState) -> tuple[set[str], int]:
    """Return (reminded, new_bookmark) with the compaction re-arm already applied.

    compact bumps its version counter by 1 on each successful compaction.
    When it has advanced past the stored bookmark, the messages that carried
    earlier hints have been summarized away, so the reminded set is cleared and
    the bookmark catches up.
    the field does not exist, so the version stays 0 <= bookmark 0 and the
    reset never fires.

    The clear-and-advance only happens on a path that records a fresh category
    (the caller persists the returned bookmark in the same update that adds the
    category, whether or not that category also emits a hint); a turn that
    matches nothing returns without touching state, so a bookmark advance never
    lands without a fresh category beside it.
    """
    bookmark: int = getattr(state, _BOOKMARK_FIELD)
    compact_v: int = state.compact.version
    if compact_v > bookmark:
        return set(), compact_v
    return set(getattr(state, _REMINDED_FIELD)), bookmark


def _select_hints(
    code: str,
    matched: list[str],
    reminded: set[str],
    nameerror_name: str | None,
    cadence: Cadence,
) -> tuple[list[str], set[str], str | None]:
    """Choose unsuppressed categories and a once-per-name persistence hint."""
    # A cell that sleeps while already naming `watcher` is the agent working
    # with the watcher primitive itself — the wait hint would be noise. Mark
    # that category seen without emitting its line, so it fires neither now nor
    # later this context window.
    silent: set[str] = {"wait"} if "wait" in matched and mentions_watcher(code) else set()

    if cadence == "every_time":
        hinted = [cat for cat in matched if cat not in silent]
    else:
        hinted = [cat for cat in matched if cat not in reminded and cat not in silent]
    newly_seen = set(hinted) | (silent - reminded)
    nameerror_hint = None
    if nameerror_name is not None:
        nameerror_category = f"{_NAMEERROR_CATEGORY_PREFIX}{nameerror_name}"
        if nameerror_category not in reminded:
            newly_seen.add(nameerror_category)
            nameerror_hint = _nameerror_persistence_hint(nameerror_name)
    return hinted, newly_seen, nameerror_hint


class _SdkReminderAfterExecHook(Hook):
    """Inject an SDK-primitive or interpreter-persistence note as its own
    system-styled message after the matching execution output.

    The note is a separate `system_note_message`, not text appended to the
    execution output: an appended line reads as the cell's own stdout, which
    the agent takes for normal program output, whereas a system note reads as a
    framework aside. The exec-output message is left untouched.

    The wait category is special-cased: a cell that sleeps while already naming
    `watcher` is the agent working with the watcher primitive itself, so the
    wait hint is suppressed (marked seen without emitting) rather than nagging.

    No-op (returns None) when the message tail does not match the
    assistant-call + execution-output shape, when neither a native idiom nor a
    qualifying NameError matches, or when every match is suppressed or already
    recorded under its once-per-compaction cadence.
    """

    async def __call__(
        self, state: AgentState, runtime: Runtime[AvaContext], config: RunnableConfig, /
    ) -> dict[str, Any] | None:
        """Match each result by ID, retaining the per-compaction hint cadence."""
        ai_index = next(
            (
                i
                for i in range(len(state.messages) - 1, -1, -1)
                if isinstance(state.messages[i], AIMessage)
            ),
            None,
        )
        if ai_index is None:
            return None
        ai = state.messages[ai_index]
        assert isinstance(ai, AIMessage)  # noqa: S101
        outputs = {
            msg.tool_call_id: msg
            for msg in state.messages[ai_index + 1 :]
            if isinstance(msg, ToolMessage)
        }
        prior = state.messages[:ai_index]
        update: dict[str, Any] = {}
        notes: list[AnyMessage] = []
        for call in ai.tool_calls:
            output = outputs.get(call["id"] or "")
            if output is None:
                continue
            single = ai.model_copy(update={"tool_calls": [call]})
            view = state.model_copy(update={**update, "messages": [*prior, single, output]})
            delta = await self._for_call(view, runtime, config)
            if delta:
                notes.extend(delta.pop("messages", []))
                update.update(delta)
            prior = [*prior, single, output]
        if notes:
            update["messages"] = notes
        return update or None

    async def _for_call(
        self,
        state: AgentState,
        runtime: Runtime[AvaContext],
        _config: RunnableConfig,
        /,
    ) -> dict[str, Any] | None:
        if len(state.messages) < 2:
            return None
        ai_msg = state.messages[-2]
        out_msg = state.messages[-1]
        if not isinstance(ai_msg, AIMessage) or not isinstance(out_msg, ToolMessage):
            return None

        # tool_calls is a pydantic field on AIMessage (always present, empty list
        # when the model called nothing); first_tool_call_code returns "" for an
        # empty list or a non-string/absent code arg.
        code = first_tool_call_code(ai_msg.tool_calls)
        if not code:
            return None

        reminders = runtime.context.require_agent().sdk_reminders
        matched = detect_categories(code)
        nameerror_name = (
            _assumed_persistence_name(state.messages[:-2], out_msg)
            if reminders.sdk_nameerror_hint_enabled
            else None
        )
        if not matched and nameerror_name is None:
            return None

        reminded, new_bookmark = _rearmed_reminded(state)

        hinted, newly_seen, nameerror_hint = _select_hints(
            code, matched, reminded, nameerror_name, reminders.sdk_code_reminder_cadence
        )
        if not newly_seen:
            # Every once-scoped match is already seen this window (or silently
            # suppressed and already marked). The bookmark only advances on a path
            # that records a fresh key, and a re-arm clears `reminded` (making every
            # once-scoped match fresh again) — so this no-op never strands a pending
            # bookmark advance.
            return None

        update: dict[str, Any] = {
            _REMINDED_FIELD: reminded | newly_seen,
            _BOOKMARK_FIELD: new_bookmark,
        }
        if hinted or nameerror_hint is not None:
            hint_lines = [hint_for(cat) for cat in hinted]
            if nameerror_hint is not None:
                hint_lines.append(nameerror_hint)
            hints = "\n".join(hint_lines)
            # Emit the hint as its own system-styled note appended after the
            # exec-output message (a fresh message with no id, so the reducer adds
            # rather than replaces). Splicing it onto out_msg.content would read as
            # the cell's own stdout; a system_note reads as a framework aside, the
            # same surface the agent_reply / compact-reminder notes use. The
            # exec-output message keeps carrying only the agent's real output.
            update["messages"] = [
                system_note_message(
                    content=hints, tag=NoteTag.SDK_HINT, created_at=datetime.now(UTC)
                )
            ]
        return update


sdk_reminder_after_exec = _SdkReminderAfterExecHook()


def _agent_reply_note() -> HumanMessage:
    """The system-styled note pointing at `ava.agents.send_message`, stamped now."""
    return system_note_message(
        content=AGENT_REPLY_HINT, tag=NoteTag.AGENT_REPLY, created_at=datetime.now(UTC)
    )


class _SdkReminderAgentReplyHook(Hook):
    """Append the note pointing at the agent->agent delivery primitive when the
    incoming batch holds a message from another agent.

    Runs before the reply is produced (a plain text reply runs no code). The
    firing cadence is `agent_reply_reminder_cadence`:
    - `once_per_compaction` (default): fire at most once per context window; a
      compaction re-arms it (the shared `reminded` set / bookmark, same as the
      code categories).
    - `every_time`: fire on every agent inbound (for agents that keep
      forgetting to use the SDK). The category does not join `reminded`.

    No-op (returns None) when the new tail has no agent-sourced inbound, when
    auto-compact would fire this same turn (either cadence — the note would be
    clobbered by compaction's message replacement; skip this inbound, leaving
    the category unmarked so the next agent inbound still qualifies), or, in
    the once cadence, when agent_reply was already hinted this window.
    """

    async def __call__(
        self,
        state: AgentState,
        runtime: Runtime[AvaContext],
        _config: RunnableConfig,
        /,
    ) -> dict[str, Any] | None:
        if not tail_has_agent_inbound(state.messages):
            return None

        # Defer if auto-compact will replace messages this same turn (both
        # cadences). `messages` carries the add_messages reducer, so co-writing it
        # merges rather than fail-louding; but compaction's full-history REMOVE_ALL
        # replacement is order-sensitive and would drop a note appended here. So
        # when compaction is predicted this turn, skip this inbound entirely: the
        # tail scan next turn stops at the agent's own AIMessage (this inbound is
        # then behind that boundary, already past), so the reminder effectively waits
        # for the *next* agent inbound. Leave AGENT_REPLY_CATEGORY unmarked so a
        # future inbound still qualifies.
        agent = runtime.context.require_agent()
        if auto_compact_will_fire(state, agent):
            logger.info(
                "[sdk-reminder] defer: auto-compact predicted, skipping agent-inbound hint this turn"
            )
            return None

        # The Literal config validates at Settings construction (an unknown value
        # fails fast there), so the match is exhaustive — a new cadence added to the
        # Literal turns this into a static non-exhaustive error rather than a silent
        # fall-through.
        match agent.sdk_reminders.agent_reply_reminder_cadence:
            case "every_time":
                return {"messages": [_agent_reply_note()]}
            case "once_per_compaction":
                reminded, new_bookmark = _rearmed_reminded(state)
                if AGENT_REPLY_CATEGORY in reminded:
                    # Already hinted, and the re-arm only clears `reminded` (which would
                    # drop AGENT_REPLY_CATEGORY out of this set), so a bookmark advance
                    # and "still reminded" cannot co-occur — nothing to persist, no-op.
                    return None
                reminded.add(AGENT_REPLY_CATEGORY)
                return {
                    "messages": [_agent_reply_note()],
                    _REMINDED_FIELD: reminded,
                    _BOOKMARK_FIELD: new_bookmark,
                }


sdk_reminder_agent_reply_before_llm = _SdkReminderAgentReplyHook()


def contribute() -> PluginContributions:
    """What this plugin declares for the agent runtime."""
    return PluginContributions(
        before_llm=(sdk_reminder_agent_reply_before_llm,),
        after_exec=(sdk_reminder_after_exec,),
        state=(AvaSdkReminderState,),
    )
