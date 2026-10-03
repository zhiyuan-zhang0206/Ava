# Dangling tool-result recovery (task #2228)

## Decision

Crash recovery treats tool-use pairing as a bidirectional invariant. An
AIMessage tool use without its following ToolMessage receives one synthetic
`[interrupted]` result. A ToolMessage whose `tool_call_id` has no tool use in
any preceding AIMessage is dropped.

The repair is always a single `REMOVE_ALL_MESSAGES` rebuild. This puts the
original AIMessage and its synthetic result in the same messages-channel value
and checkpoint blob, preventing a kill or throttled-checkpoint race from
persisting the result without the use.

The missing AIMessage is deliberately not synthesized. Testing against the
Anthropic-compatible endpoint showed LangChain normalization strips that
synthetic tool-use block from the outgoing payload, leaving the provider
request invalid. Dropping the orphan is semantically honest: without a
carrying tool use, the model cannot observe that result or resume its turn.

In claim's normal fallthrough, a non-overflow circuit-breaker heartbeat keeps
a heartbeat-only batch parked at `claim`, but it cannot park a co-batched
chat. A committed chat routes to `before_llm`, where the repair hook and a new
LLM call can heal the breaker.

## Consequences

- Startup and before-LLM recovery converge invalid histories without manual
  checkpoint deletion.
- A single chat arriving with an open-breaker heartbeat is answered instead of
  silently committed and buried.
- The deliberately narrow orphan detector does not repair tool-use/result
  non-adjacency; that provider-specific concern remains outside this change.

## Update: nullable tool-call IDs

The repair detectors normalize a nullable tool-call id to the empty string,
matching the exec path. This recovery boundary must not turn a malformed
provider message into an unhandled agent-process exit: an empty-id ToolMessage
pairs with that normalized use, while a non-empty result remains orphaned and
is dropped.

## Update: global exactly-one-result pairing

The earlier narrow conclusion was insufficient: treating tool-use pairing as
adjacent while accepting results globally can synthesize a second result when a
pending real result materializes after an intervening message. Recovery now
keeps exactly one result for each tool-call id anywhere after its tool use. For
duplicates it retains the last non-synthetic result (or the last synthetic one
when no real result exists), and only synthesizes when no kept result exists.

Physical adjacency remains the placement rule for newly synthesized results;
an existing non-adjacent result is valid and is not moved. This avoids both the
duplicate-result provider rejection and its permanent repair-loop recurrence.
