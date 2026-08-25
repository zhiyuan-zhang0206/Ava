---
type: decision
title: Page compacted timeline history by checkpoint identity
description: Timeline history reads one retained compact boundary at a time; checkpoint ids are authoritative cursors, rank is presentation only, gateway depth bounds reachability, and the browser retains at most 6000 items.
tags: [timeline, checkpoints, compaction, pagination, frontend]
date: 2026-08-25
status: accepted
---

# Page compacted timeline history by checkpoint identity

## Context

Compaction replaces the live `messages` channel but retains the last
pre-compaction checkpoint as a `compact_boundary`. Those checkpoints contain the
original messages users need when scrolling above the compact summary. A thread
may retain dozens of boundaries, and the largest observed stitched transcript
was large enough that materializing every segment in one gateway request would
turn a cold timeline read into a disproportionate memory spike.

The browser also needs one identity that survives another compact while an old
page request is in flight. A rank such as `s1` does not: every existing rank
shifts when a new boundary becomes newest.

## Decision

`GET /api/agents/{id}/timeline` pages compact history one retained boundary at a
time. Current items keep the streaming-compatible `msg.block` id. Historical
items use `s<rank>.<boundary_checkpoint_id>.<msg>.<block>`: the checkpoint id
selects content, while the recomputed rank only orders the response for display.
An unknown, non-boundary, damaged, or depth-ineligible checkpoint id returns a
terminal empty window instead of guessing another segment.

The backend enforces `AVA_TIMELINE_COMPACT_HISTORY`: `0` keeps compact history
disabled, positive `N` exposes the newest `N` boundaries, and `-1` exposes all
retained boundaries. At a segment head, paging moves to the next older
checkpoint only after the caller already holds the segment's re-attached prompt
or compact-summary context.

The frontend orders `sN … s1 … current`, renders segment dividers without
inserting synthetic items into timeline state, and stops older paging once the
retained list reaches 6000 items. If one fetched page crosses that cap, the
farthest-back items are discarded so current conversation content stays held.

## Alternatives considered

**Load `load_checkpoint_messages_full()` and window after stitching.** Rejected:
it deserializes every retained checkpoint before discarding nearly all rendered
items, making request memory scale with lifetime history rather than one page.

**Use rank-only ids (`sK.msg.block`).** Rejected: a concurrent compact changes
what rank `K` means and can return plausible but incorrect content. Returning no
content for a stale identity is safer than guessing.

**Evict arbitrary rendered items with an LRU.** Rejected for V1: eviction can
remove the user's current reading position and couples cache policy to scroll
anchoring. A hard oldest-history cutoff is predictable and terminal.

## Consequences

- Historical pagination remains a cold-load path; live SSE ids and the current
  segment's future-partial merge rule are unchanged.
- Unlimited backend depth is still bounded in the browser. Reaching 6000 items
  deliberately ends scroll-up even if older checkpoints remain.
- A tab that misses the compact reset may briefly display the same checkpoint
  under an old and recomputed rank. The exact checkpoint id prevents wrong
  content; reconnect/reset removes the display duplicate.
