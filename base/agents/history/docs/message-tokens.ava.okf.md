---
type: doc
title: Per-message true tokens
description: '`base/agents/history/message_tokens.py` — recovers each history message''s provider-tokenizer token weight from adjacent requests'' `input_tokens`, with estimate fallbacks.'
tags:
- base
- library
- agent-history
---

# Per-message true tokens

## What it is

A pure computation over a stitched history (`FullHistory`) -- no DB, no API. Every LLM request is one AIMessage and every request re-sends the previous turn's output (thinking included), so for adjacent requests `input_b - input_a = output_a + the provider-tokenizer weight of the messages between`. Every value is anchored to a provider-reported total; there is no unanchored estimate.

## Core responsibilities

- `segment_tokens(head, body, closing)` / `history_segment_tokens(history, closings)` -- one `MessageTokens(context_tokens, generation_tokens, source)` per message, per segment; `history_message_tokens` flattens to align with `history.messages`.
- `source` has two values. `exact`: the provider's own number (an AIMessage's `output_tokens`, a lone message in an interval, a lone head). `estimated`: a provider-reported total shared among several messages by the fitted estimator in `base/agents/messages/text_chars.py`, summing exactly to the total. `source` is None exactly when `context_tokens` is None.
- `generation_tokens`: an AIMessage's `output_tokens` (exact), None without usage.
- Boundaries (all re-anchor on a later provider total):
  - Segment head (SystemMessage + messages before the first request): the first request's `input_tokens`, shared by estimate. Each segment restarts its anchor.
  - AIMessage without usage: not an anchor; its interval merges forward into the next request with usage.
  - Negative difference: the offending anchor is dropped and the interval merges; with no consistent earlier anchor the later request re-anchors the whole context before it.
  - Model switch (`model_name` differs): the first request on the new model re-anchors the whole context before it (new tokenizer total, estimated); later intervals subtract as usual.
  - Tail after the last request: a sealed segment's closing request (`ClosingRequest`: the compaction LLM call that read the whole segment, minus `extra_tokens` for the compaction instruction -- non-zero makes the tail estimated) anchors it. Without one the tail was never read by any LLM: `context_tokens` is None ("not in context yet", not 0, not estimated). A segment with no request at all is entirely None.
- Where the closing request's usage lives: the compaction LLM call's `llm_usage` telemetry event (`telemetry_events`, `attributes.in_total` / `out_total`, `usage_kind='agent'`), emitted by `generate_summary` immediately before the `compaction_completed` event (`attributes.compact_kind`). Only `compact_kind` `auto` and `compact_request` make an LLM call; `compact_summary` (agent-written via `ava.self.compact`) makes none, and its segment already ends on the AIMessage that issued it, so there is no unread tail. The event is not labelled as a compact call: find it by order (the last `llm_usage` before `compaction_completed`); the caller computes `extra_tokens` from the instruction text.
- Estimator (`text_chars.py`): `a*CJK chars + b*other non-space chars` plus a per-message framing overhead (AI vs non-AI); whitespace is free. Coefficients are named constants with their fit data and date (agent 9, 2026-10-08).
- Exactness marking (the frontend appends "(estimated)" when set): an aggregate (`TokenTotal`, `SegmentSummary.estimated` / `exact_fraction`; `total_of(records)` for any bucket, Context Breakdown categories included) is `estimated` if ANY counted part is estimated; `exact_fraction` is the exact share of its tokens. None messages are not counted (`SegmentSummary.unread_messages`).
- Parts inside one message (`ai_message_parts`: reasoning / output / tool_call; `split_parts`: e.g. system-prompt sections) are always `estimated`, summing exactly to the whole; a lone part keeps the whole's source. `apportion` is the exact-sum splitter.
- `summarize_segments` -- per-segment totals (`context_tokens` incl. head, `generation_tokens`, tokens/messages by source, `unread_messages`, `last_input_tokens`).

## Entry points

- `base/agents/history/message_tokens.py:history_segment_tokens`
- `base/agents/history/message_tokens.py:summarize_segments`
