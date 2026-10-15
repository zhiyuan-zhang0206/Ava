---
type: doc
title: Compaction replacement
description: Summary request preservation, replacement and failure boundaries.
tags: [agent, compaction, contracts]
---

# Compaction replacement

## Summary request prefix

```ava-contract
id = "summary-request-prefix"
kind = "invariant"
statement = "Generated compaction appends the shared ava.self.compact instructions to the complete existing conversation prefix. Empty conversations fail before invocation and empty returned text fails validation; tool-only responses are not summaries."
code = [
  "agent/graph/hooks/compact.py:generate_summary",
]
tests = [
  "agent/tests/compact/test_compact_generate_summary.py::test_generate_summary_reuses_conversation_prefix_for_cache",
  "agent/tests/compact/test_compact_generate_summary.py::test_generate_summary_raises_on_empty_conversation",
  "agent/tests/compact/test_compact_generate_summary.py::test_generate_summary_raises_on_tool_use_only_block_content",
  "agent/tests/compact/test_compact_reminder_fires_in_band.py::test_compact_contract_reaches_request_without_resident_self",
]
```

Preserving the request prefix preserves its cache opportunity. Forced summarization cannot execute ordinary tools, so it receives the same summary contract even when the standing SDK reference omits `self`. The existing system prompt is reused for this request; `init_context` establishes the next standing head after replacement.

## Clean replacement

```ava-contract
id = "clean-replacement"
kind = "invariant"
statement = "Every applied compact clears the entire messages channel and parks its summary behind a newly established standing head through INIT_CONTEXT. Raw conversation is never retained as a tail; claim defers co-batched chats to pending and finalizes previously claimed history before replacement."
code = [
  "agent/graph/hooks/compact.py:build_compact_transition",
  "agent/graph/claim/_decide.py:_compact_outcome",
  "agent/graph/claim/_decide.py:_markers_only",
]
tests = [
  "agent/tests/test_compact.py::test_auto_compact_hook_clears_history_and_parks_summary",
  "agent/tests/claim/test_claim_compact.py::test_claim_compact_summary_with_chat_in_same_batch_defers_chat",
  "agent/tests/claim/test_claim_compact.py::test_claim_compact_summary_finalizes_claimed_history",
]
```

Framework lifecycle markers may follow the summary. A security note attached to a deferred chat goes with that chat; the fresh delivery scans it again. RemoveMessage operations are channel instructions, not summary content. History dumps are taken before the wipe and their notes go only into the fresh tail: inserting a note inside an AI/tool pair would break provider message ordering. Both paths stamp the summary message type and, when present, the live compact run id so timeline readers can join it to the running operation.

## Compact failure boundary

```ava-contract
id = "compact-failure-boundary"
kind = "invariant"
statement = "Automatic compaction retries explicitly recoverable empty/short summaries and retryable provider failures within its attempt bound. Exhaustion raises CompactionFailedError without replacing history; programming failures escape immediately. Cancellation wins over a pending compact in claim and closes its live run as replaced."
code = [
  "agent/graph/hooks/compact.py:auto_compact_summary",
  "agent/graph/hooks/compact.py:auto_compact_for_llm",
  "agent/graph/claim/_decide.py:_cancel_outcome",
]
tests = [
  "agent/tests/test_compact.py::test_auto_compact_hook_raises_when_summary_empty_every_attempt",
  "agent/tests/test_compact.py::test_auto_compact_hook_raises_when_summary_short_every_attempt",
  "agent/graph/hooks/tests/test_compact_fail_fast.py::test_compaction_programming_error_stops_once_without_history_change",
  "agent/tests/claim/test_claim_compact.py::test_claim_cancel_beats_pending_compact_and_closes_it_replaced",
]
```

`CompactionFailedError` is a turn-abort signal, not a process-death instruction. An interrupted automatic request leaves the pending command for claim without a version bump or replacement. Audit and history-dump failures are reported without aborting an otherwise successful compaction; failure must not strand a live run without its terminal signal.

## Retained boundary checkpoints

```ava-contract
id = "retained-boundary-checkpoints"
kind = "invariant"
statement = "Compaction stamps the newest pre-compaction checkpoint as the segment anchor rather than deleting checkpoints. A missing pool is a no-op; stamp failures are reported and do not abort compaction. A closing understanding chunk is anchored to the successful stamp."
code = [
  "agent/graph/hooks/compact.py:stamp_compact_boundary",
]
tests = [
  "base/agents/history/tests/test_compact_boundary.py::test_mark_compact_boundary_stamps_newest",
  "agent/graph/hooks/tests/test_compact_closing_chunk.py::test_stamp_enqueues_the_closing_chunk_against_the_stamped_checkpoint",
  "agent/graph/hooks/tests/test_compact_closing_chunk.py::test_failed_stamp_enqueues_no_closing_chunk",
]
```

The boundary retains the closing request metadata for reads and audits. The full pre-compaction snapshot remains recoverable; the old comments describing checkpoint trimming no longer describe this path.
