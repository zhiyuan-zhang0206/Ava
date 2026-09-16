"""SDK reminder plugin — surface the matching SDK primitive when the
agent reaches for a native-Python equivalent.

Three reminder families share one `reminded` set. Each hint surfaces as its own
system-styled note (`system_note_message`) injected into the conversation, not
spliced onto the agent's own output — so the agent reads it as a framework
aside rather than mistaking it for the code cell's stdout:
- Four code-cell categories (shell/wait/files/http): when an executed code
  cell uses a native idiom that has a smoother SDK primitive
  (subprocess/os.system, time.sleep loops, open()/pathlib/shutil
  file-content ops, requests/httpx/urllib), the after_exec hook injects a note pointing
  at the primitive (`ava.shell.run` / `ava.watcher` / `ava.files` / `ava.web`)
  after the cell's output, leaving that output untouched.
- Assumed-persistence NameErrors: when an undefined non-builtin identifier
  appeared in an earlier execute_code cell, the after_exec hook explains that
  each cell uses a fresh interpreter. Each name fires at most once per context
  window, and `sdk_nameerror_hint_enabled` can disable the family.
- One inbound category (agent_reply): when a message from another agent
  arrives, the agent tends to answer in plain text, which the other agent
  never sees. The before_llm hook injects a note pointing at
  `ava.agents.send_message` before the agent produces its reply (a text reply
  runs no code, so after_exec would never see it).

The four code categories' shared cadence is config-driven
(`turn_settings.agent.sdk_code_reminder_cadence`): `once_per_compaction` (at
most once per category per context window, re-armed on compaction, default) or
`every_time` (every matching code cell). The agent_reply category has its own
cadence (`turn_settings.agent.agent_reply_reminder_cadence`) with the same two
values.

Mechanics:
- Native-idiom detection + hint tables + the state schema live in `_state.py`
  (side-effect-free, independently importable for tests). The stateful
  cross-cell NameError scan lives beside the after_exec hook in this module.
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
  racing the replacement. The after_exec reminder families never collide with
  compaction.
"""

from __future__ import annotations

__description__ = "Surface matching ava SDK primitives, explain cross-cell NameErrors caused by fresh interpreters, and point plain-text agent replies at ava.agents.send_message"

# This module is the plugin's SDK **surface** — and it is deliberately empty of
# registrations: everything this plugin does is agent-runtime behavior (state
# fields, after_exec / before_llm hooks), so children do not need any of it.
# The registrations live in `agent_runtime.py`, imported only on the full path
# (see `agent/_extensions.py`; task #3633).
