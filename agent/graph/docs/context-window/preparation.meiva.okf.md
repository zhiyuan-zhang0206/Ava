---
type: doc
title: Soft compact preparation
description: Reminder delivery, current-window attribution and reply opportunity.
tags: [agent, compaction, contracts]
---

# Soft compact preparation

## Soft preparation delivery

```meiva-contract
id = "soft-preparation-delivery"
kind = "invariant"
statement = "Soft preparation fires once per compact version, only above the soft threshold and at or below the hard ceiling with conversation to preserve. Enabled plugin builders run in declaration order; blank text opts out, non-string results fail before the bookmark is committed, and peer traffic does not starve delivery."
code = [
  "agent/graph/hooks/compact.py:compact_reminder_update",
  "agent/graph/hooks/compact.py:_reminder_content",
]
tests = [
  "agent/graph/hooks/tests/test_compact_preparation.py::test_contributors_run_once_in_order_and_rearm_in_the_next_window",
  "agent/graph/hooks/tests/test_compact_preparation.py::test_contributors_do_not_run_below_soft_or_above_hard",
  "agent/graph/hooks/tests/test_compact_preparation.py::test_invalid_contributor_result_does_not_mark_preparation_as_shown",
  "agent/graph/hooks/tests/test_compact_preparation.py::test_soft_reminder_composes_with_another_message_writer",
]
```

## Occupancy and reply opportunity

```meiva-contract
id = "occupancy-and-reply-opportunity"
kind = "invariant"
statement = "Both compaction gates use the latest provider input_tokens, with chars/4 only before a measurement exists. A committed summary remains exempt from another automatic compact or reminder until a later AIMessage is committed."
code = [
  "agent/graph/hooks/compact.py:_context_occupancy",
  "agent/graph/hooks/compact.py:_summary_awaits_reply",
]
tests = [
  "agent/tests/test_compact.py::test_auto_compact_triggers_on_real_input_tokens_not_chars",
  "agent/tests/test_compact.py::test_auto_compact_skips_when_input_tokens_below_ceiling_despite_chars",
  "agent/tests/test_compact.py::test_auto_compact_falls_back_to_chars_before_first_call",
  "agent/graph/hooks/tests/test_compact_preparation.py::test_committed_summary_gets_an_ordinary_reply_before_preparation_rearms",
]
```

The measurement describes the previous model request, so the completion buffer must absorb its one-turn lag. The reply exemption is derived from checkpointed messages; cancellation, restart and new inbound notes do not erase it.

## Completed window attribution

```meiva-contract
id = "completed-window-attribution"
kind = "invariant"
statement = "compaction_completed describes an applied replacement, not a request. It counts one completion, measures conversation characters excluding the standing head, omits a ratio for empty history, and attributes reminder_shown only to the version being replaced."
code = [
  "agent/graph/hooks/compact.py:emit_compaction_monitoring",
]
tests = [
  "agent/graph/hooks/tests/test_compact_preparation.py::test_completion_attributes_only_the_current_windows_reminder",
  "agent/tests/test_compact.py::test_auto_compact_hook_clears_history_and_parks_summary",
]
```
