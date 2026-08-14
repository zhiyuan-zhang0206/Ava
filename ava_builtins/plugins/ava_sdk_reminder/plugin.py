"""SDK reminder plugin — gently surface the matching SDK primitive the first time
the agent reaches for a native-Python equivalent.

Five one-time hints, one shared `reminded` set. Each hint surfaces as its own
system-styled note (`system_note_message`) injected into the conversation, not
spliced onto the agent's own output — so the agent reads it as a framework
aside rather than mistaking it for the code cell's stdout:
- Four code-cell categories (shell/wait/files/http): when an executed code
  cell uses a native idiom that has a smoother SDK primitive
  (subprocess/os.system, time.sleep loops, open()/shutil/os file ops,
  requests/httpx/urllib), the after_exec hook injects a one-line note pointing
  at the primitive (`ava.shell.run` / `ava.watcher` / `ava.files` / `ava.web`)
  after the cell's output, leaving that output untouched.
- One inbound category (agent_reply): when a message from another agent
  arrives, the agent tends to answer in plain text, which the other agent
  never sees. The before_llm hook injects a note pointing at
  `ava.agents.send_message` before the agent produces its reply (a text reply
  runs no code, so after_exec would never see it).

The four code categories each fire at most once per context window; a
compaction re-arms them. The agent_reply category's cadence is config-driven
(`settings.agent.agent_reply_reminder_cadence`): `once_per_compaction` (the same
once-per-window re-arm, default) or `every_time` (every agent inbound).

Mechanics:
- Detection + hint tables + the state schema live in `_state.py`
  (side-effect-free, independently importable for tests).
- Both hooks are graph-edge nodes that run outside the exec turn, so they read
  their own plugin fields directly off `state` (the prefixed attrs
  `ava_sdk_reminder__reminded` / `__last_seen_compact`) and return deltas as a
  plain prefixed-key dict — the state plumbing that backs `state_handle` inside
  an exec turn is not available here.
- Re-arm: read compact.version directly off state
  (built-in core sub-state, Issue #1284, always present)
  and never triggers a reset); when it advances past `last_seen_compact`, clear
  `reminded` and catch the bookmark up, mirroring ava_code's lazy reset.
- before_llm clobber-safety: auto-compact is also a before_llm hook. `messages`
  carries the add_messages reducer, so two hooks co-writing it MERGE (the runner
  only fail-louds on a reducerless key). But auto-compact's full-history
  REMOVE_ALL replacement is order-sensitive and would drop a note appended in
  the same pass. So the agent_reply hook defers (returns None, does not mark) on
  any turn where auto-compact would fire, skipping this inbound rather than
  racing the replacement. The 4 code categories ride on after_exec, which never
  collides with compaction.
"""

from __future__ import annotations

__description__ = "Surface the matching ava SDK primitive the first time the agent uses a native-Python equivalent (subprocess / time.sleep / file ops / http) or replies to another agent in plain text"

from datetime import UTC, datetime

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph._context import AvaContext
from agent.graph._tool_calls import first_tool_call_code
from agent.hooks import Hook, register_after_exec, register_before_llm
from agent.hooks.compact import auto_compact_will_fire
from agent.messages import NoteTag, system_note_message, tail_has_agent_inbound
from agent.state import AgentState, register_plugin_state
from shared.config import settings
from shared.log import logger

from ._state import (
    AGENT_REPLY_CATEGORY,
    AGENT_REPLY_HINT,
    AvaSdkReminderState,
    detect_categories,
    hint_for,
    mentions_watcher,
)

register_plugin_state(AvaSdkReminderState)

# Channel keys for this plugin's state fields (prefixed by the framework's
# register_plugin_state). A hook node reads/writes these directly on `state`
# because the in-turn handle (state_handle.read/update) is not wired up here.
_REMINDED_FIELD = "ava_sdk_reminder__reminded"
_BOOKMARK_FIELD = "ava_sdk_reminder__last_seen_compact"


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


class _SdkReminderAfterExecHook(Hook):
    """Inject a one-time SDK-primitive note as its own system-styled message
    when the cell used a native-Python equivalent.

    The note is a separate `system_note_message`, not text appended to the
    execution output: an appended line reads as the cell's own stdout, which
    the agent takes for normal program output, whereas a system note reads as a
    framework aside. The exec-output message is left untouched.

    The wait category is special-cased: a cell that sleeps while already naming
    `watcher` is the agent working with the watcher primitive itself, so the
    wait hint is suppressed (marked seen without emitting) rather than nagging.

    No-op (returns None) when the message tail does not match the
    assistant-call + execution-output shape, when the code matched nothing,
    or when every matched category has already been hinted (or silently
    suppressed and marked) this context window.
    """

    async def __call__(
        self,
        state: AgentState,
        _runtime: Runtime[AvaContext],
        _config: RunnableConfig,
        /,
    ) -> dict | None:
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

        matched = detect_categories(code)
        if not matched:
            return None

        reminded, new_bookmark = _rearmed_reminded(state)

        # A cell that sleeps while already naming `watcher` is the agent working
        # with the watcher primitive itself — the wait hint would be noise. Mark
        # that category seen without emitting its line, so it fires neither now nor
        # later this context window.
        silent = {"wait"} if "wait" in matched and mentions_watcher(code) else set()

        hinted = [cat for cat in matched if cat not in reminded and cat not in silent]
        newly_seen = set(hinted) | (silent - reminded)
        if not newly_seen:
            # Every matched category is already seen this window (or silently
            # suppressed and already marked). The bookmark only advances on a path
            # that records a fresh category, and a re-arm clears `reminded` (making
            # every match fresh again) — so this no-op never strands a pending
            # bookmark advance.
            return None

        update: dict = {
            _REMINDED_FIELD: reminded | newly_seen,
            _BOOKMARK_FIELD: new_bookmark,
        }
        if hinted:
            hints = "\n".join(hint_for(cat) for cat in hinted)
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
register_after_exec(sdk_reminder_after_exec)


def _agent_reply_note() -> HumanMessage:
    """The system-styled note pointing at `ava.agents.send_message`, stamped now."""
    return system_note_message(
        content=AGENT_REPLY_HINT, tag=NoteTag.AGENT_REPLY, created_at=datetime.now(UTC)
    )


class _SdkReminderAgentReplyHook(Hook):
    """Append the note pointing at the agent->agent delivery primitive when the
    incoming batch holds a message from another agent.

    Runs before the reply is produced (a plain text reply runs no code). The
    firing cadence is `settings.agent.agent_reply_reminder_cadence`:
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
        _runtime: Runtime[AvaContext],
        _config: RunnableConfig,
        /,
    ) -> dict | None:
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
        if auto_compact_will_fire(state):
            logger.info(
                "[sdk-reminder] defer: auto-compact predicted, skipping agent-inbound hint this turn"
            )
            return None

        # The Literal config validates at Settings construction (an unknown value
        # fails fast there), so the match is exhaustive — a new cadence added to the
        # Literal turns this into a static non-exhaustive error rather than a silent
        # fall-through.
        match settings.agent.agent_reply_reminder_cadence:
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
register_before_llm(sdk_reminder_agent_reply_before_llm)
