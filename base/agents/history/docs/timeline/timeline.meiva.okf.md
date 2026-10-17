---
type: doc
title: Timeline rendering
description: "The one render rule both the agent and the gateway run over state.messages to build TimelineItem lists (`base/agents/history/timeline.py`)."
tags:
- base
- library
- agent-history
---

# Timeline rendering

`build_timeline_items` renders `state.messages` into `TimelineItem` items. The
agent (from in-memory state at each node enter) and the gateway
(`GET /api/agents/{id}/timeline`, from the checkpoint) call the same rule; the
module sits in `base/` (leaf) so both import it without an agent <-> gateway
cycle. Building the snapshot in the agent removed the race where the gateway
re-read the checkpoint before an async commit landed and missed the
just-claimed inbound. `TimelineItem` lives in `timeline_item.py` (file line
budget); this module re-exports it.

Inbound and attach messages render text only: a multimodal inbound's base64
image blocks are dropped (thumbnails ride on `meiva_image_urls`); an attach's
data-URI images ride on `images` with per-image captions. The system prompt is
item 0, `created_at=None`. A compact item prefixes the payload with the
wall-clock ts when the timestamp policy is on and carries the producing run as
`compact_id`. Signature-only / redacted thinking blocks render nothing. The note timestamp split is its own node: [[base/agents/history/docs/timeline/note-timestamp-policy.meiva.okf.md]].

## Tool-call block offset

Code items come from the normalized tool_calls view — one provider-agnostic rule (anthropic tool_use content blocks; gemini / openai tool_calls-only shapes). block_idx = <distinct text/thinking block indices> + <ordinal in tool_calls>: a set, so a signature-only thinking block sharing a sibling's index does not inflate the offset. The streaming side computes the same offset (agent/graph/_callbacks.py); SSE ids equal committed snapshot ids.

## Reasoning timing

Each thinking block carries its own per-block duration (meiva_reasoning_ms_by_block, keyed by block_idx); the turn-level reasoning tokens attach to the FIRST thinking item only; historical messages keep None rather than an invented value. Provider-native reasoning is folded to canonical thinking blocks on a copy — the stored message stays native.

## Attach caption pairing

Each image's caption is the text block immediately before it (modern interleaved attach pack) — structural pairing, no text parsing. Legacy attach messages (one caption block then all images) cannot recover per-image lines and fall back to the legacy all-text-then-all-images layout. Only data: URLs are exposed; other media never leaks into payload or images.

## Human-message fallback

Tagged framework HumanMessages take explicit branches; anything left — pre-tag history, retired marker types like the old LLM-cancel — renders as a system_marker with source=None, which the frontend flags as the red UnknownMarkerChip. Fail-loud is deliberate: a red chip on rare historical rows beats a silent mis-render; an untagged system note is the same case.

## Window unit and readers

display.timeline_default_limit (MEIVA_TIMELINE_DEFAULT_LIMIT, default 50) counts timeline ITEMS and is read per call through timeline_default_limit() — both the gateway and the agent import this module, so consumers must not capture the value at import. tail_window returns the newest limit items plus has_more; limit <= 0 or a list no longer than the limit returns everything with has_more=False.
