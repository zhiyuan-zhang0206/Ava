# Compaction redesign: forced / command / spontaneous compact

> Status: forced compact (#1099) + command compact (#1116) landed. The compaction
> contract (sections + how to write) is now single-sourced in the `ava.self.compact`
> docstring; the three triggers are short openers that defer to it (#1099). Parked
> items (model-owned compaction timing) remain below.

Compaction has three modes, ordered by how much say the agent has, each mapping
onto an entry point in `agent/hooks/compact.py` / `ava/self.py`.

| Mode | Trigger / entry point | Who writes the summary | Declinable |
|---|---|---|---|
| **forced compact** | `before_llm` hook over the ceiling -> `generate_summary` | the model, in a separate compaction LLM call | no — the hard backstop |
| **command compact** | a `/compact` command (prompt template `commands/compact.md`), typed or reminder-triggered -> housekeeping turns -> `ava.self.compact(summary)` | the agent, in its own turn | yes |
| **spontaneous compact** | the agent decides on its own to call `ava.self.compact(summary)` (guided only by that SDK docstring) | the agent | yes — its own choice |

Compaction always rebuilds the context as `[system prompt, summary]` — a full
REMOVE_ALL, no raw tail (#1099). After it the agent continues as **the same agent** — same id, same
identity — its next LM call simply has no memory of what was replaced, only the
summary. So every summary's reader is *this same agent on a context-cleared
turn*, never a different or successor agent. The framework frames each summary
with a fixed header (`COMPACT_SUMMARY_HEADER`) and the agent writes the body in
the **first person** ("I"), referring to others as "the user" / "agent N" — one
self-note convention shared by all three modes, so the header disambiguates the
first-person voice from the user even though the summary lands as a user-role
message.

The header carries one more job: it states **"your context was just compacted"**.
The compaction is the single event the post-compact context has no surviving
record of — REMOVE_ALL wipes the turn that ran it, and the exec node never
writes the compact call's output back in the first place (the `[system halt]
You just called ava.self.compact` ack survives only in the agent log). Without a
standing "just compacted" line, an agent that reads a wind-down command still
sitting in the summary's verbatim tail (a `/compact` the agent dutifully quoted there) re-reads
it as a pending order and runs it again — every turn, a self-compact loop until a
human interrupts (observed: agent 17, five self-compacts in 86s). Two defenses,
both pulling their weight: the header is the standing signal the wiped ack cannot
be, and the `ava.self.compact` contract tells the summary to **exclude the
wind-down command that triggered this compaction from the verbatim tail** —
writing the summary already carries it out, so quoting it back is exactly what
made it look pending. The header alone only *bounds* the loop (a live re-test on
the fixed build self-stopped after 3-4 redundant compacts instead of needing a
human); dropping the trigger command from the tail is what cuts it to a single
compaction.

## Triggers

- **forced**: a real-overflow ceiling. It is the hard backstop and stays armed at all
  times — including while the agent is mid-housekeeping for a command compact. If the agent's
  own housekeeping turns push the context past the ceiling, forced compact fires anyway.
- **command**: a **qualitative** reminder, injected by the compact hook (`agent/hooks/compact.py`, moved into core from the ava_compact plugin)
  when the estimated context crosses `soft_compact_tokens` (set *earlier* than the ceiling,
  leaving room for the housekeeping turns). It is the same hook as forced compact — the branch
  below the ceiling — so the two are mutually exclusive and never both write the turn. Agent-side
  only: no `InboundKind`, no nudge / heartbeat rail. It fires at most once per context window
  (re-arms via `compact.version`) and defers to ava_sdk_reminder's agent-reply note when both would
  write `messages` in one pass. The reminder does **not** report a remaining-token count.
- **spontaneous**: no trigger — the agent calls `ava.self.compact` whenever it judges its
  context should be reset. This already exists; it is distinct from the parked item below
  (which is about the *framework feeding the model its budget* so it decides timing).
- **Parked**: full model-owned "decide when to compact" autonomy.
  Rationale — *context anxiety*: Cognition's Devin rebuild on a budget-aware model found that
  feeding the model its remaining budget made it rush, cut corners, and leave tasks unfinished
  *even with ample context left*, while confidently mis-estimating how much it had. All four
  surveyed tools keep trigger timing in framework code. Ava is immune by default (its model has
  no context-awareness training and the SDK never exposes remaining budget) — keep it that way.
  Telling the agent its remaining budget (trigger "option a") is a flagged experiment, **off by
  default**; the shipping default is the qualitative reminder ("option b").

## Summary quality (one shared structure, all modes)

The current `COMPACTION_INSTRUCTION` is five loose bullets — a freeform "please summarize". That
is what let a 1326-message history collapse into 236 chars of conversational filler:
`generate_summary` only checks the reply is non-empty, never that it is a real summary.

Replace it with a structured, mandatory-section template. **Direction only here — the exact
prompt wording goes through the prompt render-review loop with the user, it is not authored
unreviewed in the implementing PR:**

- mandatory sections, each filled or explicitly `(none)`
- list **all** user messages — intent must never survive only as paraphrase
- quote the most recent turns **verbatim** — except the wind-down command that triggered this
  compaction (a `/compact`): the summary already carries it out, so quoting it back re-triggers a
  compact loop (see the header paragraph above)
- preserve exact paths / commands / error strings
- **anchored update**: a prior summary is updated in place, never nested (kills summary-of-summary decay)
- constraints quoted verbatim; never paste whole files (cite path + the decisive clause)
- session **runtime state** (todos, sub-agent handles, watchers) is side-channeled to files, not
  the summary — the agent-compact command guides writing it durably before compacting

The template **is** the quality defense — it leans on the model's instruction-following rather
than a downstream gate trying to recognize a bad summary. Deliberately **not** adding a
multi-signal `validate_summary` guard (length + conversational-opening + section presence
deciding accept/reject): that is machinery to manage an ambiguity the instruction should kill
outright, and the codebase prefers the stronger invariant. If the model genuinely ignores a
mandatory-section template, that is an instruction-following regression to surface, not to paper
over with a gate.

The one runtime safety the forced path keeps (no agent in the loop to notice a weak reply):
a **length-triggered retry**. The summary length is logged on every attempt as a monitoring
metric. A summary below `COMPACT_MIN_SUMMARY_CHARS` (or no text at all) is taken as "ignored the
template" and the cache-mostly request is retried up to `COMPACT_MAX_ATTEMPTS`; if every attempt
stays short, `auto_compact_before_llm` **raises** rather than overwrite history with a non-summary
(the agent-240 failure: 236 chars for a near-full window). One length signal used only to trigger
a retry — not a guard deciding accept/reject. Quality regressions show up in the length metric,
and the fix is to iterate on the **template**, not to grow the gate.

### One contract, one place (#1099)

"One shared structure" is now literal: the contract — the summary's mandatory sections and how to
write it — lives only in the `ava.self.compact` docstring. The three triggers no longer each carry
a copy:

- **spontaneous** — the agent reads the docstring in its own SDK and writes the summary.
- **command** (`commands/compact.md`) and the **reminder** nudge — short openers that wind the
  agent down and tell it to compact "as the docstring specifies"; the agent reads the contract
  when it acts.
- **forced** (`COMPACTION_INSTRUCTION`) — a short opener too. The contract is already in this
  request's leading prompt because the SDK reference renders `self` (the docstring is part of the
  rendered system prompt), so the model writing the summary sees it without it being restated.

This is why the triggers can be short and still produce a structured summary, and why a single
edit to the docstring changes every mode at once. A test pins both halves: the section headers are
present in the docstring **and** rendered into the system prompt (so the forced path's reference
resolves), and each trigger names `ava.self.compact`.

> Open follow-up (deferred): post-compact the agent can still mis-locate its own files when its
> working directory has drifted from its workspace — the docstring points at durable files but not
> by a path that survives a different cwd. Tracked separately from this consolidation.

## Self-overflow is already handled upstream

A single giant tool output cannot push the compaction request past the window: the exec node
caps each tool result at `exec_output_max_chars` (default 30K — keeps the head + tail, drops the
middle, writes the full text to a tmp file it reports). With no oversized blob in history and the
800K trigger leaving ~200K of headroom under the 1M window, the compaction request itself does
not overflow. No degradation ladder / circuit breaker is built — it would be defensive machinery
for a path the upstream cap already closes.

## Already done — NOT in scope

- **cache-riding**: done. `generate_summary` keeps the original `SystemMessage`, appends one
  trailing instruction, and binds `execute_code`, so the request is a strict cached prefix.
  (The 2026-06-10 research describing a position-0 system-prompt swap / cache miss is stale —
  the code was fixed afterward.)
- **no raw tail**: done. #1099 replaced the kept-tail design with a full REMOVE_ALL —
  compaction rebuilds `[system prompt, summary]` with nothing raw carried over, so there is no
  tail pair-invariant to maintain (`split_tail` is gone). The `ava.self.compact` `[system halt]`
  trigger pair is no longer written back at all — it would be wiped with the rest, so the ordering
  fix the original plan listed is moot — but the wipe still removes the only record that a
  compaction happened, which is why the header now states "just compacted" (see the header
  paragraph above; the loop it prevents was real).
- lighter pruning tiers / post-compact file re-reads / degradation monitoring: parked — pure
  machinery; revisit only if 800K-era compaction quality shows real problems.

## PR split

- **PR1 — forced compact (#1099)**: structured mandatory-section template (first-person, framed
  by `COMPACT_SUMMARY_HEADER`) + length-triggered retry (log length metric, retry-then-fail-fast
  under `COMPACT_MIN_SUMMARY_CHARS`) + `CompactDone` emit on the auto path + frontend refresh. No
  `validate_summary` guard and no self-overflow ladder — see the two sections above for why each
  was dropped. The shared header + first-person self-note convention also lands here (applied to
  the command/spontaneous `compact_summary` injection in the same pass).
- **PR2 — command compact (#1116)**: qualitative reminder as the compact hook's `before_llm`
  (new `soft_compact_tokens` threshold < ceiling, agent-side — no `InboundKind`, no nudge /
  heartbeat rail; once per window, defers to the agent-reply note) + `commands/compact.md`
  housekeeping template + `CompactDone` emit on the agent claim path. The trigger-pair ordering
  fix the original plan listed is moot — #1099's no-tail REMOVE_ALL wipes the `[system halt]`
  pair with the rest. Depends on PR1's template structure.
- **PR3 — single-source the contract (#1099)**: the section template, copied across
  `COMPACTION_INSTRUCTION`, the reminder note, and `commands/compact.md`, collapses into the
  `ava.self.compact` docstring; the three triggers become short openers that defer to it (see
  "One contract, one place" above). Pure consolidation — no behavior change to the modes. The CWD
  follow-up noted there is explicitly out of scope.
