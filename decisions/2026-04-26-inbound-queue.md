# Inbound Message Queue

## Context

Every trigger that drives the agent — text typed in a UI, a kernel-injected
compact prompt, a UI compact instruction, and eventually cron / webhook — needs
a durable, single entry point. The web UI could only observe user input it had
itself produced ("user input is naturally known in the browser"); the moment a
second channel (terminal, chat bridge, scheduler) exists, that assumption breaks.

Constraints:

- Durability: a UI-issued control action (e.g. trigger a compact) must survive a
  crash, not vanish.
- Thread isolation: the conversation history is a per-thread checkpointed message
  list. Mixing two threads' triggers into one processing batch corrupts it.
- One kernel actor: the kernel runs a single main loop. Adding a second
  long-lived control actor to handle "instructions" is the cost to avoid.
- Cancel is special: while the loop blocks on a running turn it cannot also poll
  for an interrupt, yet cancel demands immediate response.

## Decision

A single table, `inbound_messages`, is the one durable entry point for every
external trigger. Rows carry a `kind`; the main loop dispatches on it:

- `chat` / `cron` / `webhook` — wrap in a channel envelope, feed the LLM.
- `compact_prompt` — feed the LLM verbatim (kernel holds the prompt literal).
- `agent_compact` — rewrite as a `compact_prompt` row and re-enqueue (picked up
  next round).
- `framework_compact` — call the kernel routine directly; never feed the LLM.

Control instructions thus ride the *same* queue path as chat; the only
difference is that on those kinds the loop triggers kernel-internal behavior
instead of an LLM call. Control durability is therefore the table's job, the
main loop stays single, and no separate control actor exists.

The loop is strictly **one batch = one thread**: first pick a thread that has
pending rows (FIFO by earliest pending time across threads), then claim only
*that* thread's rows (`WHERE thread_id = $1 AND status = 'pending' FOR UPDATE
SKIP LOCKED`). Status is a three-state machine `pending → claimed → done`; `done`
rows are kept forever for history replay. A startup sweep flips orphaned
`claimed` rows back to `pending`.

**Cancel / stop is the one bypass.** It does *not* go through the table — it
travels over a pub/sub control channel to an independent watcher task (necessary
because the blocked loop cannot also select on the channel). The payload carries
`thread_id`; the kernel keeps a per-thread `cancel_event` registry. The watcher
sets the event for the matching thread, or drops the signal if no event is
registered (that thread isn't running a turn). Cancel is ephemeral; if it lands
when nothing matches, the user presses again.

**Subprocess output (`code_output`) is not an inbound.** It is the previous
node's execution result, appended as a `HumanMessage` directly into the
checkpointed message list — the framework's own thread-scoped history mechanism.
The agent's own code is likewise mined from checkpointed `AIMessage` content, not
a separate table.

## Alternatives rejected

- **Direct fix "make the web UI show user input."** Surface-level. The real gap
  is that all channels need one observable, durable trigger abstraction; patching
  the symptom leaves the next channel broken.

- **Batch-fetch then post-hoc consume (drop the claim/lock model).** Loses
  claim/lock safety — a crash mid-processing leaves rows in an ambiguous state
  with no recovery story. Rejected in favor of keeping `SKIP LOCKED` claim plus
  the explicit `claimed` state and startup recovery sweep.

- **Compaction as an ephemeral, non-durable UI action.** Makes compaction a lossy
  action: the instruction can be dropped on crash. Rejected — compact triggers
  are durable rows like any other inbound.

- **A separate control actor / separate signal path for all control.** Splitting
  control signals out of the DB into their own actor is architectural cost paid
  for a hypothetical. Folding control kinds into the one queue keeps a single
  main loop. Only cancel — which has a hard immediate-response constraint — gets
  the bypass, and that is an exception forced by blocking semantics, not a design
  preference.

- **Global (un-scoped) cancel broadcast.** A cancel issued from a view of thread
  A could kill a turn running thread B. Rejected: the signal carries `thread_id`
  and is filtered by the per-thread event registry.

- **`code_output` as an inbound row + an intra-graph exec self-loop preserved as
  an atomic invariant.** Writing execution output back through the inbound table
  created a crash window where output is persisted twice; and treating the
  `code → output → code` self-loop as inviolable shields the agent from
  mid-flight correction. Both rejected: output lives in the checkpointed message
  list (root-causing the double-persist away), and the self-loop is deliberately
  broken (see below).

- **Separate tables for the agent's code / for thread-scoped history.** Redundant
  with the checkpointer, maintained only to hedge a hypothetical framework swap.
  The framework is the infrastructure assumption; no shadow tables.

## Consequences

- **The graph degrades to one entry → one LLM → one subprocess → exit.** The
  intra-graph self-loop is removed; each code segment returns to the main loop
  for a fresh claim. This is intentional: every LLM call becomes a full decision
  point, so an inbound that arrived mid-flight (a user "wait, do X first," a cron
  fire) is in the message history at the next call and the agent can re-decide.
  The agent is interruptible *by reasoning*, not just by binary cancel. Cost: no
  "look at output, immediately write the next segment" auto-chaining inside the
  graph; each segment is one extra loop round (negligible in-process).

- **Crash atomicity is resolved by dedup, not one big transaction.** The
  checkpoint commit and the `mark done` commit are separate. Each inbound's
  envelope embeds its `inbound_id`; the entry node scans the recent message tail
  and skips any inbound already present. So "checkpoint committed, crashed before
  mark done" recovers by re-feeding the batch and idempotently skipping the
  re-apply — no duplicate in the LLM's view.

- **At-least-once side effects.** Dedup protects LLM *state* only. Subprocess side
  effects (sending a message, writing a file, calling an external API) are *not*
  protected — a recovered re-run re-executes them. Same semantics as before; no
  regression.

- **The table grows unbounded.** `done` rows are kept for replay; long threads
  accumulate. Archival / partitioning is deferred.

- **Migration is irreversible in practice** (table + trigger rename, new status
  value): a wrong new model rolls back wholesale, not by local patch.

- **Cancel scope is per-thread, not per-view.** Multiple clients on the same
  thread still interrupt each other's single active turn (whoever presses first
  wins). Acceptable under one-active-turn-per-thread; finer per-view scoping would
  need a turn identifier.

- **Architecture is pre-allocated for new channels.** A new UI is a plugin that
  writes inbound rows on input and subscribes to its own output channel; new
  trigger sources (cron, webhook) are just new `kind`s reusing the chat path.
  Native UIs hold no privileged status.
