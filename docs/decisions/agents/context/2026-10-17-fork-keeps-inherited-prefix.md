# Fork keeps the inherited prefix byte-identical, and re-homes only its own workspace

## Context

Two fork behaviours surfaced while measuring what a fork actually costs and what
it inherits.

### The head rewrite made every first fork pay a full prefill

`fork_rebuild_prefix()` (issue #1320, 2026-08-21) rebuilt the inherited history
as `[RemoveMessage(__remove_all__), *history minus source-identity notes]`, so a
fork would not read the source's agent-id, per-agent-memory, inherited-memory
and preloaded-skills notes. The fork's own copies of those notes were grafted
after the fork marker.

The rebuild changes the message list at the first source-identity note — on a
freshly compacted thread, message 4 of 65. A provider prefix cache matches a
request byte-for-byte from position 0, so everything after the first divergent
token is a miss: a fork could never reuse its source's cached prompt, which is
the economic point of forking instead of spawning.

Measured from `telemetry_events_*.llm_usage`, first agent-scope call of each
forked agent (request start = event time − `latency_ms`):

| fork | source checkpoint | request start (UTC) | in | cached | hit |
|---|---|---|---|---|---|
| #6698 | — | 10-10 09:41 | 400,898 | 15,360 | 3.8% |
| #6706 | — | 10-12 10:08 | 374,021 | 15,360 | 4.1% |
| #6731 | — | 10-14 05:51 | 308,837 | 15,744 | 5.1% |
| #6713 | 6111 @1f1c6e01 | 10-13 08:29:02 | 196,844 | 15,488 | 7.9% |
| #6719 | 2697 @1f1c6f24 | 10-13 10:39:12 | 116,559 | 15,488 | 13.3% |
| #6715 | 6111 @1f1c6e32 | 10-13 08:51:28 | 350,555 | 194,816 | 55.6% |
| #6720 | 2697 @1f1c6f24 | 10-13 10:39:15 | 116,515 | 115,200 | 98.9% |
| #6714 | 6111 @1f1c6e01 | 10-13 08:29:21 | 196,806 | 194,944 | 99.1% |

Across 26 forks since 2026-09-01, 9 started below 50% first-turn cache; their
misses total ~2.4M input tokens (~$0.32 at the hit/miss price difference) plus
10–20 s of prefill on the one turn a caller waits on.

The hits are the tell. They always belong to the *second* fork of a checkpoint:
#6720's request started 3.2 s after #6719's and matched the blocks #6719's
prefill had just written; #6714 followed #6713 the same way. #6715's 55.6% is
exactly the prefix it shares with #6714's fork point (194,816 ≈ #6714's 196,806
tokens). The source's own cache never matches, because the rewrite diverges at
message 4 while the parent's prompt still carries those notes.

The rewrite also contradicted `base/agents/birth/transaction.py`, whose comment
already claimed the inherited prefix stays byte-identical — the copy preserves
rows, and only the claim rewrite broke the property.

### The fork inherited the source's workspace as its working directory

`meiva_code__cwd` is an ordinary graph channel, so `copy_checkpoint_chain` copies
it with the rest of `channel_values`. When the source's logical cwd was its own
workspace — the default — the fork spent its whole life writing into the
source's private scratch directory: #6720 ran with
`meiva_code__cwd = /Users/zyonzhang/.ava/workspaces/2697` in every checkpoint
(`.ava` is the pre-rename home — the same path is `~/.meiva/workspaces/2697`
today; the rename landed 2026-10-17 and does not change the mechanism).
The after-init validation let it through because the path exists and is a
directory; only a *missing* cwd was repaired.

### An interrupted tool call is not a third defect

A fork taken from a hard-cancelled process inherits a `tool_use` with no
`tool_result`. That is already repaired: `repair_dangling_tool_pairing` (a
framework `before_llm` hook) inserts a synthetic `[interrupted]` result on the
first turn — observed as `dangling_tool_pairing_repaired` and as the synthetic
`ToolMessage` at index 63 of #6719/#6720. Because the insertion lands at the
tail of the inherited prefix (right after the dangling call), it does not break
the cache match either. No fork-specific mechanism was added.

## Decision

A fork inherits the source's history **verbatim** and appends everything that is
its own: the fork marker (which now states that the identity, memory and skill
notes above belong to the source) followed by the new agent's own note copies.
Nothing in the inherited prefix is rewritten, re-ordered or re-emitted.

Identity that is *not* history — the logical working directory — is re-homed
when and only when it is another agent's workspace: `ValidateCwdAfterInitHook`
replaces a persisted cwd inside `<home>/workspaces/<other-id>` with the invoking
agent's own workspace, and leaves every other persisted cwd alone. A repo,
checkout or download directory the source chose deliberately describes the work,
not the agent, so the fork keeps it.

## Alternatives rejected

- **Keep the strip and accept the prefill.** The rewrite buys one thing: the
  fork's context does not contain a second agent's identity notes. A tail marker
  buys the same understanding for free; the cost is 10–20 s and a full-window
  prefill on every first fork of a checkpoint, forever.
- **Strip only when the provider has no cache entry.** The client cannot know
  that; and the second fork of the same checkpoint would then diverge from the
  first, making the hit rate depend on which fork ran first.
- **Reset the fork's cwd to its own workspace unconditionally.** Simple, and
  wrong for the common coding case: a fork whose source was working in a repo
  checkout must keep that checkout, or every fork would re-orient from scratch.
- **Filter `meiva_code__*` out of the checkpoint copy.** The copy is pure SQL
  over `channel_values`; special-casing one plugin's channels couples the history
  copier to a plugin, and the dedup state (`injected_paths`, `injected_hashes`)
  must stay consistent with the inherited messages.
- **Rewrite cwd in the fork claim handler.** The claim dispatcher would have to
  know a plugin's channel name — the same layering violation, moved.

## Consequences

- The first fork of a checkpoint rides the source's cache: expected ≈99% hit
  instead of 4–13%. Later forks of the same checkpoint behave as before.
- A fork's context holds the source's identity notes *and* its own. The marker
  must carry the distinction; the model is told explicitly that the notes above
  it belong to the source.
- The claim update for a fork is now a pure tail append (no `RemoveMessage`
  wipe), so the fork no longer rewrites the whole message channel on its first
  checkpoint.
- Existing forks keep their old cwd until their next process start; the
  after-init check is what repairs them.
