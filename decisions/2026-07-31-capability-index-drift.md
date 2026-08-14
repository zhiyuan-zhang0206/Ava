# The capability index is a snapshot, so the framework records what it snapshotted

## Context

[`# Capabilities`](2026-07-31-one-universal-skill-index.md) became the agent's
complete listing of what it can already do, and the delegation check — the
prompt's one mandatory-flagged process — orders the agent to match every task
against it before starting. That makes the index load-bearing in a way a
best-effort listing is not: an agent is told to treat absence from it as evidence
a capability does not exist.

But the index is rendered into the SystemMessage, and `init_context` lays that
message down only when a context window is established — an agent's first wake,
and the turn after each compaction. `ava.skills._names()` underneath it is an
uncached filesystem scan, re-read on every call. The two age differently and
nothing compared them, so a skill installed mid-window was reachable by name and
absent from the listing the agent had been told to trust, until a compaction
happened to rebuild the prompt.

The universal-index work did not touch this — it changed *what* the snapshot
covers, not that it is one. The closed skill-recall issue did not either: its
`corpus()` short-circuited on a `*` config, treating the wildcard as
covered-**forever** rather than covered-**as-of-last-build**.

Installs mid-session are rare and a restart self-heals, so the cost of the bug is
low. The reason to close it anyway is that the prompt makes a promise about
completeness that the system had no mechanism to keep.

## Decision

Make "as of last build" something the system records. `init_context` writes the
index membership it rendered into `state.capabilities.indexed`, and a
framework-owned `before_llm` hook diffs live membership against that record
before each LLM call — the moment the agent reads its index. Anything that
appeared is named in one system note using the index's own line shape, and the
snapshot advances in the same update, so one install is named once.

Drift is the trigger. There is no timer, no subscription, and no notion of who
installed the skill.

## Alternatives rejected

**Rebuild the system prompt every turn.** The obvious fix, and it makes the
staleness impossible rather than detectable. Rejected on two counts. The
SystemMessage is the stable prefix of every LLM call, so rebuilding it per turn
invalidates prompt caching on every turn to reflect a change that is not there
approximately always. And `init_context` is the *sole owner* of the standing
head — one node, one condition (an empty `messages`), which is what collapsed
four hand-maintained copies of the establishment order into one. A head that
mutates per turn gives that ownership back up to buy a property the diff already
provides.

**Emit an event from the install site.** `ava skill install` knows exactly when
the catalog moved, so have it tell the agents. Rejected: the installer is almost
never the agent's process — another agent on the box, an operator's shell, a
plugin's `scaffold()` during converge — so this needs a new cross-process channel
to every live agent, and it still misses membership that moves without an install
at all (a provider root appearing as the agent changes cwd, a configured name
that starts resolving). Keying on the agent's own snapshot makes the correction
provenance-independent and needs no new plumbing.

**Solve it as recall over an uncovered corpus.** The direction the closed
skill-recall issue was headed: semantically retrieve from the skills the index
does not cover. Rejected as the answer to *this* gap — recall addresses breadth
("what might be relevant that you were never shown"), which starts to matter when
a federated catalog outgrows the prompt. Staleness is a freshness question with
an exact answer, and an exact diff should not be paid for with a model call. The
breadth analysis stays relevant on its own timeline.

**Rescan on a timer, or in a background task.** Rejected: the turn boundary is
already the only moment the answer is consumed, so a poll can only be more work
for a staler result.

**Announce removals as well as additions.** Rejected: a vanished skill leaves the
standing index over-promising, and `ava.skills.<name>` already fails fast on it —
which is the fail-fast answer. Membership is replaced rather than unioned, so a
skill removed and reinstalled does announce again.

**Put it behind a settings toggle.** Every comparable layer (passive recall, the
SDK reminder) is removable. Rejected here because a default-off toggle does not
close the gap and a default-on toggle is a switch with no one to flip it: an
index that silently under-reports is a correctness problem in a promise the
prompt already makes, not an optional enhancement.

## Consequences

- One uncached skill scan per LLM call. Measured at roughly 25 ms on a
  57-skill / 2.1 MB catalog, against a multi-second model call. A cluster whose
  catalog grows by an order of magnitude should re-measure rather than assume.
- `indexed` is `set[str] | None`, and the `None` is load-bearing: it means no
  snapshot exists for this window, which is what every agent alive across the
  rollout looks like. Those agents get the live catalog adopted as their baseline
  **silently** — what their standing SystemMessage lists is unknowable from the
  hook, and announcing the whole catalog as newly installed is a louder wrong
  answer than none. They start hearing about installs from their next one.
- `prompt_injected` attribution can now be written mid-session, for a skill that
  drifted in. That is correct — the note does inject it — but the depth no longer
  means strictly "listed at boot".
- The note is not guaranteed delivery; the index is. A compaction landing in the
  same `before_llm` pass makes the check **defer** — it returns `None` and writes
  nothing that turn, gated on the same `auto_compact_will_fire(state)` predicate
  the reminder plugins use. The reducer is why deferring is mandatory rather than
  tidy: `add_messages` applies compaction's `REMOVE_ALL` and *then* the append, so
  a note written in that pass is the sole survivor of the wipe, and
  `init_context` — whose only trigger is an empty `messages` — would read it as
  an intact history, drop the parked summary, and never lay down the
  SystemMessage. Nothing is lost by staying quiet: the compaction routes through
  `init_context`, which rebuilds `# Capabilities` from the catalog that now
  contains those skills. The invariant being kept is "the agent's index is not
  stale", not "every install produces a visible message".
- Deliberately-narrowed agents are served by the same diff with no special case,
  because membership is defined as whatever the configured list resolves to. A
  name an overlay listed before the skill existed resolves later, and that is
  drift.
