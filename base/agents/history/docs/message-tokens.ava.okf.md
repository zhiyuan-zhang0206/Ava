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
- `source`: `exact` (lone message in the interval, an AIMessage with `output_tokens`, a lone head), `split` (several messages share a real total in proportion to the chars/4 estimate from `base/agents/messages/text_chars.py`, the same estimate the context breakdown uses), `estimated` (chars/4 fallback).
- Fallbacks to `estimated`: negative difference (crash repair, rewritten history), different `model_name` across the interval, an AIMessage without usage on either side, the tail after a segment's last request.
- Segment head (SystemMessage + messages before the first request) is anchored by the first request's `input_tokens` and split by estimate; it absorbs fixed request overhead such as tool schemas. Each segment restarts its anchor — never chained across a compaction.
- `summarize_segments` — per-segment totals (`context_tokens` incl. head, `generation_tokens`, tokens/messages by source, `last_input_tokens`).

## Entry points

- `base/agents/history/message_tokens.py:history_segment_tokens`
- `base/agents/history/message_tokens.py:summarize_segments`
