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
image blocks are dropped (thumbnails ride on `ava_image_urls`); an attach's
data-URI images ride on `images` with per-image captions. The system prompt is
item 0, `created_at=None`. A compact item prefixes the payload with the
wall-clock ts when the timestamp policy is on and carries the producing run as
`compact_id`. Signature-only / redacted thinking blocks render nothing.

## Window and identifiers

```ava-contract
id = "timeline-window-contract"
kind = "invariant"
statement = """The render is incremental-safe: `start` renders only messages[start:] while item_ids keep ABSOLUTE msg_idx (streaming SSE ids stay aligned); msg_count is ALWAYS the full len(messages) — the frontend's future-partial boundary depends on it; segment_prefix is reserved for cold-loaded compact history and prefixes local ids. An unrecognized message shape raises — fail-loud."""
code = ["base/agents/history/timeline.py:build_timeline_items"]
tests = ["gateway/tests/history/test_timeline_part2.py::TestBuildTimelineItemsStartOffset::test_start_offset_keeps_absolute_item_ids_and_full_msg_count", "gateway/tests/history/test_timeline.py::TestTimelineFailLoud::test_dispatch_error_propagates_as_500"]
```

## Note timestamp policy

```ava-contract
id = "note-timestamp-policy"
kind = "invariant"
statement = """system_marker.show_timestamp hides the ts for ambient context and the one-time guidance notes (compact reminder, SDK hint, agent reply, silent idle-continue, security, exec timeout, timezone, the to_user soft limit); heartbeats, lifecycle_* transitions, task notifications, impersonation and NEW_SKILLS keep it. NEW_SKILLS stays visible deliberately: the drift note is the only place that mid-window install is visible. The split is closed over NoteTag — a new member needs an explicit decision."""
code = ["base/agents/history/timeline.py:_NO_TIMESTAMP_NOTE_TAGS"]
tests = ["gateway/tests/history/timeline/test_ai_message_items.py::TestAiMessageItems::test_system_note_show_timestamp_by_tag"]
```

## Tool-call block offset

```ava-contract
id = "tool-call-block-offset"
kind = "invariant"
statement = """Code items come from the normalized tool_calls view — one provider-agnostic rule (anthropic tool_use content blocks; gemini / openai tool_calls-only shapes). block_idx = <distinct text/thinking block indices> + <ordinal in tool_calls>: a set, so a signature-only thinking block sharing a sibling's index does not inflate the offset. The streaming side computes the same offset (agent/graph/_callbacks.py); SSE ids equal committed snapshot ids."""
code = ["base/agents/history/timeline.py:ai_message_items", "base/agents/history/timeline.py:_tool_call_items"]
tests = ["gateway/tests/history/timeline/test_ai_message_items.py::TestAiMessageItems::test_gemini_multiple_tool_calls_render_distinct_code_blocks", "gateway/tests/history/timeline/test_ai_message_items.py::TestAiMessageItems::test_signature_only_thinking_block_skipped"]
```

## Reasoning timing

```ava-contract
id = "reasoning-timing"
kind = "invariant"
statement = """Each thinking block carries its own per-block duration (ava_reasoning_ms_by_block, keyed by block_idx); the turn-level reasoning tokens attach to the FIRST thinking item only; historical messages keep None rather than an invented value. Provider-native reasoning is folded to canonical thinking blocks on a copy — the stored message stays native."""
code = ["base/agents/history/timeline.py:ai_message_items", "base/agents/history/timeline.py:_content_block_item"]
tests = ["gateway/tests/history/timeline/test_ai_message_items.py::TestAiMessageItems::test_reasoning_ms_and_tokens_on_first_thinking_item", "gateway/tests/history/timeline/test_ai_message_items.py::TestAiMessageItems::test_per_block_reasoning_ms_each_thinking_block"]
```

## Attach caption pairing

```ava-contract
id = "attach-caption-pairing"
kind = "invariant"
statement = """Each image's caption is the text block immediately before it (modern interleaved attach pack) — structural pairing, no text parsing. Legacy attach messages (one caption block then all images) cannot recover per-image lines and fall back to the legacy all-text-then-all-images layout. Only data: URLs are exposed; other media never leaks into payload or images."""
code = ["base/agents/history/timeline.py:_attach_image_captions", "base/agents/history/timeline.py:_attach_images", "base/agents/history/timeline.py:_attach_item"]
tests = ["gateway/tests/history/timeline/test_attach_items.py::TestAttachItems::test_multiple_images_carry_aligned_image_captions", "gateway/tests/history/timeline/test_attach_items.py::TestAttachItems::test_legacy_single_caption_block_has_no_image_captions"]
```

## Human-message fallback

```ava-contract
id = "human-fallback-fail-loud"
kind = "invariant"
statement = """Tagged framework HumanMessages take explicit branches; anything left — pre-tag history, retired marker types like the old LLM-cancel — renders as a system_marker with source=None, which the frontend flags as the red UnknownMarkerChip. Fail-loud is deliberate: a red chip on rare historical rows beats a silent mis-render; an untagged system note is the same case."""
code = ["base/agents/history/timeline.py:_fallback_human_item", "base/agents/history/timeline.py:_system_note_item"]
tests = ["gateway/tests/history/test_timeline.py::TestAvaMsgTypeDispatch::test_untagged_human_message_hits_catch_all_fail_loud", "gateway/tests/history/timeline/test_ai_message_items.py::TestAiMessageItems::test_system_note_renders_as_system_marker"]
```

## Window unit and readers

```ava-contract
id = "timeline-window-unit"
kind = "invariant"
statement = """display.timeline_default_limit (AVA_TIMELINE_DEFAULT_LIMIT, default 50) counts timeline ITEMS and is read per call through timeline_default_limit() — both the gateway and the agent import this module, so consumers must not capture the value at import. tail_window returns the newest limit items plus has_more; limit <= 0 or a list no longer than the limit returns everything with has_more=False."""
code = ["base/agents/history/timeline.py:timeline_default_limit", "base/agents/history/timeline.py:tail_window"]
tests = ["gateway/tests/history/timeline/test_timeline_skips_inbound_without_langgraph_state.py::TestSystemPromptInColdLoad::test_default_window_comes_from_display_config", "gateway/tests/history/timeline/test_timeline_skips_inbound_without_langgraph_state.py::TestTimelineWindowing::test_tail_window_trims_to_newest_and_flags_more"]
```
