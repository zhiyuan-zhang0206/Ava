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
- `source` has two values. `exact`: the provider's own number (an AIMessage's `output_tokens`, a lone message in an interval, a lone head). `estimated`: a provider-reported total shared among several messages by the fitted estimator in `base/agents/messages/token_estimate.py`, summing exactly to the total. `source` is None exactly when `context_tokens` is None.
- `generation_tokens`: an AIMessage's `output_tokens` (exact), None without usage.
- Boundaries (all re-anchor on a later provider total):
  - Segment head (SystemMessage + messages before the first request): the first request's `input_tokens`, shared by estimate. Each segment restarts its anchor.
  - AIMessage without usage: not an anchor; its interval merges forward into the next request with usage.
  - Negative difference: the offending anchor is dropped and the interval merges; with no consistent earlier anchor the later request re-anchors the whole context before it.
  - Model switch (`model_name` differs): every message keeps the value it was read with; the messages first read by the new model's first request get their estimated share of that request's whole-context input (two tokenizers cannot be subtracted), later intervals subtract as usual. A context drawn after the switch (`context_through`, used by `/run-timeline/context?at=` and the breakdown) re-splits what preceded it against the new total (estimated).
  - Tail after the last request: a sealed segment's closing request (`ClosingRequest`: the compaction LLM call that read the whole segment, minus `extra_tokens` for the compaction instruction -- non-zero makes the tail estimated) anchors it. Without one the tail was never read by any LLM: `context_tokens` is None ("not in context yet", not 0, not estimated). A segment with no request at all is entirely None.
- Where the closing request lives: the compaction that made an LLM call (`compact_kind` `auto` / `compact_request`) stamps its input tokens, model and instruction size into the boundary checkpoint's metadata (`compact_anchor`, written by `mark_compact_boundary` from `agent/hooks/compact.py:stamp_compact_boundary`; `generate_summary` returns a `SummaryText` that remembers the call). `load_checkpoint_history_full` hands it back per segment as `FullHistory.segment_closings`. Agent-written summaries (`compact_summary`), the no-LLM fallback and every boundary written before the anchor existed have none, and their segment's tail stays None. The metadata is on the checkpoint row only; no message or request changes.
- Estimator (`token_estimate.py`): `a*CJK chars + b*other non-space chars` plus a per-message framing overhead (AI vs non-AI); whitespace is free. Coefficients are named constants with their fit data and date (agent 9, 2026-10-08).
- Exactness marking (the frontend appends "(estimated)" when set): an aggregate (`TokenTotal`, `SegmentSummary.estimated` / `exact_fraction`; `total_of(records)` for any bucket, Context Breakdown categories included) is `estimated` if ANY counted part is estimated; `exact_fraction` is the exact share of its tokens. None messages are not counted (`SegmentSummary.unread_messages`).
- Parts inside one message (`ai_message_parts`: reasoning / output / tool_call; `split_parts`: e.g. system-prompt sections) are always `estimated`, summing exactly to the whole; a lone part keeps the whole's source. `apportion` is the exact-sum splitter.
- Consumers: `gateway/agents/history/context_breakdown.py` (categories are sums of message counts; only the inside of a message is split), `gateway/run_timeline/tokens.py` (unit blocks), the requests list and `gateway/agents/history/understanding.py` (sessions list). The build cost estimate (`hierarchy/build.py`) already prices from provider-reported `input_tokens` and needs no estimator.
- `summarize_segments` -- per-segment totals (`context_tokens` incl. head, `generation_tokens`, tokens/messages by source, `unread_messages`, `last_input_tokens`).

## Entry points

- `base/agents/history/message_tokens.py:history_segment_tokens`
- `base/agents/history/message_tokens.py:summarize_segments`
