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

A pure computation over a stitched history (`FullHistory`) — no DB, no API. Every LLM request is one AIMessage and every request re-sends the previous turn's output (thinking included), so for adjacent requests of one segment `input_{j+1} - input_j = output_j + the real tokens of the non-AI messages between`.

## Core responsibilities

- `segment_tokens(head, body)` / `history_segment_tokens(history)` — one `MessageTokens(context_tokens, generation_tokens, source)` per message, per segment; `history_message_tokens` flattens to align with `history.messages`.
- `source`: `exact` (lone message in the interval, an AIMessage with `output_tokens`, a lone head), `split` (several messages share a real total in proportion to the fitted token estimator in `base/agents/messages/text_chars.py`), `estimated` (that estimator alone).
- Fallbacks to `estimated`: negative difference (crash repair, rewritten history), different `model_name` across the interval, an AIMessage without usage on either side, the tail after a segment's last request.
- Segment head (SystemMessage + messages before the first request) is anchored by the first request's `input_tokens` and split by estimate; it absorbs fixed request overhead such as tool schemas. Each segment restarts its anchor — never chained across a compaction.
- `summarize_segments` — per-segment totals (`context_tokens` incl. head, `generation_tokens`, tokens/messages by source, `last_input_tokens`).

- Estimator (`text_chars.py`): `a*CJK chars + b*other non-space chars` plus a per-message framing overhead (AI vs non-AI); whitespace is free. Coefficients are named constants with their fit data and date (agent 9, 2026-10-08); on the held-out half median |error| is 6.6% against 34% for chars/4.
- Exactness marking (the frontend appends "(estimated)" when set): every `MessageTokens` carries `source`. An aggregate (`TokenTotal`, `SegmentSummary.estimated` / `exact_fraction`) is `estimated` if ANY part is `split` or `estimated`; `exact_fraction` is the exact share of its tokens. `total_of(records)` applies the rule to any bucket (Context Breakdown categories reuse it).
- Parts inside one message (`ai_message_parts`: reasoning / output / tool_call; `split_parts`: e.g. system-prompt sections) are always `split` (or `estimated` when the whole is), summing exactly to the whole; a lone part keeps the whole's source. `apportion` is the exact-sum splitter.

## Entry points

- `base/agents/history/message_tokens.py:history_segment_tokens`
- `base/agents/history/message_tokens.py:summarize_segments`
