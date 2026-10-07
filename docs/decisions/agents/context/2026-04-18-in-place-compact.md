# In-place context compaction

## Context

A long-running agent's thread grows until it no longer fits the model's context window.
Compaction replaces a full thread with a short summary plus a small live tail, in a new
thread that points back at its parent. The forcing constraints:

- The summary must be **model-generated**, not a mechanical text-join — only an LLM can
  decide what to keep.
- There are two distinct triggers with different ownership: the **framework** can compact
  a thread it doesn't reason inside (run a compaction LLM out-of-band), and the **agent**
  can compact itself by writing its own summary mid-turn. These are mutually exclusive
  modes, not one mode with a flag.
- Agent-initiated compaction must be a first-class SDK call available at any time
  (`ava.compact(summary)`), routed through `help(ava)` discovery rather than crammed into
  the system prompt fed every turn.
- A new thread's opening state must be installed **without** triggering a model call (the
  summary is already written; invoking the graph would burn a turn).
- Subprocess (where the agent runs code) and the agent loop must coordinate through
  durable state, not fragile out-of-band signals.

## Decision

**One `pending` table, dual `kind`.** `pending` rows carry `kind IN ('chat','compact')`.
`chat` rows feed the agent loop's FIFO; `compact` rows are a per-thread signal the loop
drains. Same table — pending is just "queued events" with different consumers, so it shares
status/timestamp columns and the existing LISTEN/NOTIFY trigger. Split indexes:
cross-thread FIFO claim for chat, per-thread lookup for compact. `wait_for_pending` and
the cross-thread claim filter `kind='chat'`; compact is checked immediately after a chat
turn completes, never waited on.

**Two compaction entry points, both running a real LLM + graph.**

- `framework_compact(...)` — runs the compaction LLM itself, builds the new thread,
  installs initial state, and marks the parent **compacted**. Fully synchronous: the HTTP
  caller gets back `new_thread_id`.
- `agent_compact(...)` — the agent wrote the summary; this only builds the new thread and
  installs initial state, leaving the parent **active**. Reached asynchronously: the
  endpoint just enqueues a prompt; the new thread doesn't exist until the agent runs and
  the loop takes over.

Both share `create_compacted_thread` (one transaction: build new thread, optionally mark
parent compacted via a bool param) and both open the new thread with
`[Human(summary), ...tail]` (a small fixed tail of recent messages). Initial state is
written with `graph.update_state`, **not** `graph.invoke` — `update_state` writes straight
to the checkpointer and the `add_messages` reducer appends into the empty channel, with no
model call. This is why every process that installs thread state (the web/ops process and
the agent loop) holds a compiled graph instance even though only the loop ever invokes it.

The compaction LLM uses its **own** system prompt — it is not the agent and must emit a
**text** summary, never Python (no `ava.chat.send` / `ava.stop`). The guidelines are
repeated as a trailing `HumanMessage` because a system prompt plus a tail human message
reinforce each other and produce more stable output than either alone.

**Agent-initiated compaction, end to end.** `ava.compact(summary)`:
1. INSERT a `kind='compact'` pending into the current thread with the summary as content.
2. Publish a `compact_request` event (UI progress feedback).
3. Raise `SystemHalt` (exit 43).

`SystemHalt` is distinct from `AgentHalt` (exit 42): `AgentHalt` means "I'm done with this
turn"; `SystemHalt` means "I executed an operation that changes system state" — after
compaction the agent logically migrates to a new thread. `exec_node` treats **both** exit
codes as a normal halt (`halted=True`, turn ends); only an unexpected non-zero exit is fed
back as an error. The exit code only ends the turn — **the compaction action is driven
entirely by the `compact` pending in the DB**, not by the exit code. After the loop
finishes a chat turn and marks it done, it drains any `compact` pending for that thread,
runs `agent_compact`, publishes `compact_done`, and marks the pending done. Subprocess and
loop thus communicate only through the DB; the exit code carries no out-of-band compaction
semantics.

**Auto-trigger by rough token estimate.** After each chat turn (post `mark_pending_done`,
post compact handling), the loop estimates the thread's tokens and, if over the threshold,
enqueues the agent-compaction prompt as a `chat` pending. The agent picks it up FIFO,
reviews the context on its own reasoning chain, writes a summary, and calls `ava.compact` —
no forced path. Token count is `total_chars // 4`, deliberately coarse: it overestimates,
so it triggers early, which is the safe direction. Ordering matters — compact handling runs
before the token recount, so if the agent just compacted (new messages now flow to the new
thread) the recount sees the drop and won't re-trigger; if it didn't, the recount is the
fallback. A dedup guard (`has_pending_compact_prompt`, exact content match on an unhandled
`chat` prompt) prevents re-enqueuing every turn while the agent is still organizing.

**Mode-symmetric surfaces.** The HTTP endpoint branches on `?mode=framework|agent`; the
terminal mirrors it with positional subcommands `/compact framework` / `/compact agent`
(not a flag — the modes are mutually exclusive and this maps 1:1 to the HTTP route).
Manual agent-mode trigger reuses the same `insert_compact_prompt_pending` as auto-trigger
but **skips dedup** (an explicit re-click should always re-fire — the prior attempt may
have failed). Framework mode switches the terminal's thread immediately on the synchronous
response; agent mode prints `enqueued`, then switches when the tailer receives
`compact_done`. The tailer owns `state.thread_id`; on `compact_done` it reassigns it
(atomic attribute write, no lock), so a line typed during the switch lands in the new
thread — the intended behavior.

**SDK shape.** `ava.compact` and `ava.thread_messages` are flat top-level functions backed
by underscore-prefixed internal modules (`_compact.py`, `_history.py`) — `compact` is a
function name and can't collide with a module name. The system prompt lists only the
high-frequency calls (`ava.chat.send`, `ava.stop`) and points everything else at
`help(ava)` / `help(ava.X)` (explicitly: don't `import inspect`). Each level carries its
own docstring; `__all__` marks the public set. This keeps the per-turn system prompt small
and stops it growing with the SDK.

## Alternatives rejected

- **Mechanical text-join for framework compaction.** Concatenating message text is not a
  summary; only a model can decide what survives. Framework mode runs a real compaction
  LLM.
- **Web side mutating the DB to compact on the agent's behalf (the prior manual-endpoint
  design).** Agent-mode compaction's essence is the **agent actively** calling it from the
  SDK. The endpoint only enqueues a prompt; the agent does the organizing on its own
  reasoning chain. The old design's three-step `compact_commit` (build thread + insert
  pending + mark parent) didn't fit and was dropped.
- **Stuffing every SDK API into the system prompt.** It bloats the per-turn prompt and
  grows with the SDK. Only the hot calls stay inline; the rest is discovered via `help()`.
- **`graph.invoke` to seed the new thread.** It triggers a model call for a summary that's
  already written. `graph.update_state` installs initial state directly via the
  checkpointer with no model call.
- **Distinguishing the compaction action by exit code.** Tried having exit 43 itself carry
  "do the compaction" meaning. Rejected: subprocess↔loop coordination must be durable, so
  the DB `compact` pending drives the action and the exit code only ends the turn. (Both 42
  and 43 halt identically.)
- **Splitting `pending` into separate chat/compact tables.** Duplicates status/timestamp
  columns and the LISTEN/NOTIFY trigger for no gain — they're the same queue with different
  consumers. One table, two kinds, split indexes.
- **A precise tokenizer for the auto-trigger.** `tiktoken` is OpenAI's BPE and inaccurate
  here; the provider's token-count API is a per-tick round-trip, too expensive. The
  `chars/4` estimate overestimates and triggers early — the safe direction — so no new
  state field (e.g. a stored `last_input_tokens`) is warranted.
- **`/compact --agent` as a flag.** The two modes are mutually exclusive, not one mode plus
  an option; a positional subcommand expresses that and maps cleanly onto `?mode=...`, and
  future modes add a subcommand instead of a flag combination.
- **A hard lower-bound (`<150K`) rejecting compaction.** Manual rejection was dropped; with
  the agent compacting essentially only under auto-trigger guidance, no hard validation is
  needed.

## Consequences

- **Compaction always costs a real model call** (framework: the compaction LLM; agent: a
  full agent turn to organize). The summary quality is the price of correctness.
- **The parent thread's fate differs by mode** — framework marks it compacted, agent leaves
  it active. After an agent compaction the parent can keep shrinking as new traffic routes
  to the child, which the token recount accounts for.
- **Multiple processes must hold a compiled graph** purely to call `update_state`; each
  calls `saver.setup()` (idempotent, versioned migrations table).
- **Coordination is fully DB-mediated.** No out-of-band signal beyond the durable `compact`
  pending; the exit code is a turn-ending convenience only.
- **The dedup guard is exact-content text matching.** A version bump to the compaction
  prompt silently breaks dedup until reconciled — an accepted limit at this stage.
- **Token accounting is intentionally imprecise**, biased to trigger early; threshold
  semantics tolerate being tens of thousands of tokens off.
- **`SystemHalt` (exit 43) is a permanent control-flow vocabulary item** distinct from
  `AgentHalt` (exit 42), reserved for agent operations that change system state.
- **End-to-end behavior is unverified at decision time** — the pieces are wired statically;
  live testing follows.
