# Single `execute_code` tool as the agent's wire form

## Context

The agent loop is structurally ReAct: `user → reasoning + action → action result → ...`.
But the wire form was a hacky deviation from it: the assistant emitted raw Python as
`AIMessage.content` with no `tool_calls`, exec output came back wrapped in a `HumanMessage`
envelope, and the previous round's `reasoning_content` / thinking blocks were never sent
back into the next round's input.

Mainstream reasoning models are trained on tool-augmented multi-turn traces
(`user → reasoning + tool_call → tool_result → ...`). Deviating from that distribution
costs us two ways:

- **Re-reasoning each round.** With prior reasoning not fed back, the model can't see what
  it already thought and re-derives it from scratch every turn — pure output-token waste that
  compounds on long agent / research chains. A controlled probe (5-round task) cut output
  reasoning tokens ~11.6x and total cost ~25%; reasoning stabilized at a handful of tokens
  from turn 2 in the aligned form, versus rebounding in the status quo. Longer reasoning
  chains amplify the saving.
- **Quality drift.** Inference off the training distribution plausibly degrades quality
  (vendor cookbooks quantify a few percent on agent benchmarks). Not directly measured here —
  treated as reasonable inference, not a proven conclusion.

## Decision

The agent's sole tool is `execute_code(code: str)`, which runs a Python snippet in the
sandbox and returns stdout/stderr.

- Each LLM call binds exactly this one tool.
- Each output is an `AIMessage` with optional `content` (text "spoken" to the user) plus
  optional `tool_calls=[execute_code(code=...)]`.
- Exec output returns as a `ToolMessage` paired by `tool_call_id`, not a `HumanMessage`
  envelope.
- **Critical invariant:** when serializing the assistant turn back to the API, the prior
  round's `reasoning_content` / thinking blocks (with signature) must be injected back into
  the input. This reuse is the entire revenue of the design — without it, the model re-reasons.
- No `tool_calls` is a first-class signal, not an error to recover from: it means "stop the
  turn" and auto-wait for the next inbound. Routing keys off `tool_calls` presence.

The model still writes Python; Python is now the tool's argument instead of message content.
The agent's mental model is nearly unchanged — capabilities remain exposed through the
`ava.*` Python namespace inside the tool body.

SDK surface collapses accordingly: "speaking" is just `AIMessage.content` (delete the
explicit send call); "stop turn" is empty `tool_calls` (delete the explicit idle call).
Explicit lifecycle actions (terminate / restart / compact) stay — the agent invokes them
from inside an `execute_code` body.

## Alternatives rejected

- **Status quo (raw Python as message content, exec output as `HumanMessage` envelope).**
  The disease itself: off-distribution wire form, no reasoning reuse, re-reasoning every turn.
- **Cache-hit improvement as the justification.** An early read of the probe assumed the
  single-tool form would lift input cache-hit rate. Corrected measurement showed wire format
  barely moves prefix cache hit, and can be slightly negative — each round's added
  `reasoning_content` + `tool_calls` form a dynamic tail that breaks prefix-tail stability.
  Cache is explicitly *not* the argument; reasoning reuse is.
- **`tool_choice="required"` to force a tool call.** Rejected — not supported by the target
  reasoner, and it would break the feature where the agent deliberately does nothing by
  emitting empty `tool_calls`. The system prompt instead states that a missing
  `execute_code` call is treated as stopping the turn, letting the model self-decide.
- **Fallback to the old raw-content form when `tool_calls` is absent.** Rejected for
  fail-fast: absence of `tool_calls` means "stop", and a malformed turn should raise, not be
  silently reinterpreted as legacy code.
- **Migration script for old conversation history.** A migration would have to rewrite
  historical `AIMessage(content=code)` into `tool_calls`, preserve `tool_call_id` pairing,
  convert envelope output to `ToolMessage`, and handle orphans — complexity exceeding the
  value. Chosen instead: fresh start — old threads go read-only and non-resumable, new
  threads use the new schema.

## Consequences

- **Reasoning preservation is load-bearing and provider-specific.** Each backend needs its
  own plumbing to round-trip prior-round reasoning verbatim (a serialization override where
  the SDK lacks native support; signature-carrying thinking blocks elsewhere). Get this wrong
  and the core benefit silently evaporates while everything still "works."
- **Paired-message integrity becomes an invariant.** Every `tool_call` must keep its matching
  `ToolMessage`. Compaction (summary replacing messages) must preserve this pairing — an
  orphan `tool_call` confuses the model or hard-errors the API.
- **Routing simplifies.** A `halted` state field is removed; `tool_calls` presence is the
  single source of truth for continue-vs-stop.
- **Old checkpoints are abandoned, not migrated.** A pre-cutover assistant tail (raw content,
  no `tool_calls`) cannot resume and raises by design. Acceptable because no production
  history was worth preserving.
- **Streaming splits into three channels** — reasoning, assistant text, and tool-call code —
  each surfaced as its own event so the UI can fold reasoning (noise to end users, useful to
  developers), render text, and highlight code independently.
- **Observability gains a dimension** — output reasoning tokens are tracked as the primary
  metric of this design, since reasoning reuse (not cache hit) is what it buys.
