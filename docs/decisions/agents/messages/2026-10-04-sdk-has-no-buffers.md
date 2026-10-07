# The SDK keeps no buffers: a finding goes to the graph state, an audit fact goes to the event path

## Context

Two process-level containers lived in the `ava` package. `ava/security.py::_pending_findings` held
the prompt-injection findings `scan_content` raised while agent code ran, until the exec child drained
them with `take_findings()` into a `findings` field of its result envelope; the exec node re-validated
each entry and merged a SECURITY note into its messages delta. `ava/skills/__init__.py::_recorded_skill_invocations`
was a `(agent, skill)` set that deduplicated the `skill_invoked` audit events a body consumption writes.

The skill set only suppressed writes: each event was already written to `audit_events` at the call.
The findings list was real state with a drain protocol, shared with the host through a side field of
the envelope that bypassed the state update every plugin delta already travels in.

## Decision

1. **A finding that must reach the model is written into the turn's state update the moment it is
   found.** `ava.security` appends it to `ava.state_update["security_findings"]`, a base channel
   `BaseAgentState.security_findings` with an `operator.add` reducer. The exec node commits it like
   any plugin delta (`SDK_WRITTEN_BASE_FIELDS` lets this one framework channel through
   `_validate_plugin_state_keys`). The framework's after_exec hook `agent/hooks/security.py` reads
   the graph state, writes one SECURITY note per entry into `messages` and resets the channel with
   `Overwrite([])`. `take_findings`, the envelope's `findings` field and the exec node's findings
   merge are gone; `SecurityFindingEntry` moved to `base/agents/messages/security_finding.py` because
   the SDK and the agent state now share it.
2. **An audit fact is written where it happens and nothing is remembered.** `_record_skill_invoked`
   emits one `skill_invoked` event per body consumption through `record_audit_reported` (the
   `audit_events` row, then the unified stream; a failed write is an error log with traceback plus an
   `audit_write_failed` event, never a raise and never silent). The dedup set is deleted rather than
   moved: the consumer (`ava_self_evolution`'s `_skills_touched`) reads the set of skills, and the old
   set was scoped to one exec child, i.e. one `execute_code`, so it never deduplicated a run.
3. Both entries leave `scripts/structure/baseline`; the baseline only shrinks.

## Alternatives rejected

- **Keep findings in the exec node: pop `security_findings` out of the update and merge the notes in
  the same delta, as plugin messages are.** No new hook and the notes would stay ahead of the plugin's
  context notes, but the finding would live only in the node's local variables. The channel is part of
  the checkpoint, so a finding committed with the tool result survives a crash before delivery, and
  every consumer reads graph state the way the hooks decision (2026-10-04) requires.
- **Write the SECURITY note itself into `ava.state_update["messages"]`.** Needs a plugin to declare
  `messages` (the validation rejects an undeclared base write) and the SDK to import the agent's
  message constructors, which it may not.
- **Keep a dedup for skill events, in the database** (`INSERT ... WHERE NOT EXISTS`). It needs a custom
  insert outside the audit primitives (`lint_audit_record`), adds a read per consumption, and the
  boundary it would dedup over ("one run") has no stored definition any more.

## Consequences

- A repeated read of one SKILL.md inside one `execute_code` writes a row each time; consumers that
  count rows instead of skills would see the difference.
- An exec-child SECURITY note now arrives from the after_exec hook, behind the tool results and behind
  the plugin's context notes (it used to precede them). Both are in front of the next LLM call.
- A compacting exec drops its findings and clears earlier ones of the batch together with the history
  they annotate.
- `scan_content` outside an exec turn still drops its finding, now with a logged warning: there is no
  state update to carry it (the claim node's inbound scan returns its finding to its caller instead).
- An external (impersonation) attachment now carries findings through its plugin delta journal instead
  of leaking them into a process-global list nobody drained.
