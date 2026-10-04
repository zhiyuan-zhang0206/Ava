# Hooks operate on the graph state; the SDK's exec slot exists only in the exec child

## Context

`ava.state` / `ava.state_update` are the bridge between agent code and LangGraph: the exec node
snapshots the graph state into the exec child, SDK functions read it and write a delta, the child
returns the delta in its result envelope, and the node commits it through the reducers. That
bridge was kept; what was loose was who could touch it.

- `ava.state` was a module attribute that read `None` everywhere except an exec child. The agent
  host, which serves many agents' turns, could read it and get `None` without an error.
- The ava_code `after_exec` hook did exactly that: it read its plugin state through
  `PluginStateHandle.read()`, which reads `ava.state`. In the host that raised
  `PluginStateOutsideTurnError`, a `suppress` swallowed it, and the hook returned nothing. The
  cwd-change note and the project-skills listing were never injected, and `cwd_note` was never
  cleared. Nothing noticed: no test ran the hook.
- `ava.state is not None` doubled as "am I in an exec turn" in four ava_code wraps and the security
  findings buffer, and one wrap reached into `ava.state.compact.version` directly.

## Decision

1. **Outside an exec turn `ava.state` / `ava.state_update` do not exist.** Reading either raises
   `PluginStateOutsideTurnError` (an `AttributeError`) saying it is available only inside
   `execute_code`. Assigning `ava.state = None` is a `TypeError`. The exec child binds the slot by
   assignment as before; `ava.unbind_exec_turn()` ends a turn (an external attachment's detach, a
   test). Nothing in the child resets the slot: it is discarded with the process.
2. **"Inside an exec turn" is `ava.in_exec_turn()`**, one explicit predicate. The wraps, the
   findings buffer and the project-skill source ask it; none probes `ava.state` or catches the
   error to find out. The compaction counter an exec-side wrap needs comes from
   `agent.state.compact_version()`.
3. **`PluginStateHandle` has two sides.** Host side, pure: `view(state)` builds the typed snapshot
   from the graph `state` a hook receives (no revalidation, no copy of declared `messages`), and
   `delta({...})` turns plugin-local fields into the prefixed update dict the hook returns for the
   reducer. Exec side: `read()` / `update()` on the slot, as before.
4. **A hook module imports no `ava`.** A module under `agent/hooks/`, a plugin's `agent_runtime.py`
   face, or any module defining a `Hook` subclass may not import `ava` or `ava.*`, including inside
   a function. `scripts/lint/plugins/no_ava_in_hooks.py` enforces it with no allowlist; the existing
   violations were cleared in the same change (the capabilities hook's skill identifiers moved into
   `agent/graph/prompt/capabilities.py`; ava_code's state class and prompt sections moved out of
   `agent_runtime.py` into `_state.py` and `_prompt_sections.py`).
5. The ava_code hook now reads `view(state)` and returns `delta(...)`, so the notes it was written
   to inject reach the agent for the first time.

## Alternatives rejected

- **Delete `ava.state`.** The exec child has no LangGraph runtime, and `get_runtime()` /
  `ToolRuntime` ride a contextvar that does not cross the process boundary. Some projection of the
  graph state into the child is needed; this one already carries the checkpointed fields and the
  reducer-correct delta.
- **Move the slot out of the `ava` package** into an exec-runtime holder. It would remove the
  attribute from the public module, but changes nothing the host could mis-read; the fail-loud
  attribute does that at a fraction of the churn.
- **Keep `None` outside a turn and fix only the ava_code hook.** The next host-side read of the slot
  would be silent again. The error is the guard; the lint stops the import that leads to it.

## Consequences

- Agent-visible: after `ava.cwd.set(...)`, the next step shows the "Working directory set to ..."
  note and, in a repo with project skills, the skills listing (once per compaction).
- `ava.state` reads in any process but an exec child (a bare script, the host, a test that did not
  bind a slot) now raise instead of returning `None`.
- An exec turn cannot attach an external controller.
- Plugins outside the repo: the hooks of `claude_usage`, `codex_usage` and `deepseek_balance`
  (macmini, ubuntu, wsl) import no `ava` and use neither the handle nor `ava.state`; `ava_grok` has
  no hook. None needs migrating.
