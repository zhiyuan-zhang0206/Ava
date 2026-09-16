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

# This module is the plugin's SDK **surface** — deliberately empty of
# registrations: everything this plugin does is agent-runtime behavior
# (a before_llm hook), so children do not need any of it. The registrations live
# in `agent_runtime.py`, imported only on the full path (see
# `agent/_extensions.py`; task #3633).
