---
type: doc
title: Message metadata contracts
description: "The cross-stack wire contracts of the message metadata layer: the closed NoteTag set and the compact summary header."
tags: []
---

# Message metadata contracts

The load-bearing wire contracts of `base/agents/messages/kwargs.py` — the
places the `ava_*` metadata vocabulary leaves that module and other components
(agent, gateway, insights, frontend) depend on it.

```ava-contract
id = "notetag-closed-set"
kind = "invariant"
statement = """NoteTag is a closed cross-stack set: a new member needs its frontend dispatch branch (markers.tsx) and its gateway timestamp classification, or the UI renders the loud unrecognized marker instead of a generic note. The value set itself is a wire contract, locked by value-set tests against silent renames."""
code = ["base/agents/messages/kwargs.py:NoteTag"]
tests = ["tests/contracts/test_lint_marker_contract.py::test_note_tag_and_frontend_dispatch_sets_agree", "base/agents/messages/tests/test_kwargs.py::test_note_tag_value_set", "gateway/tests/history/timeline/test_ai_message_items.py::TestAiMessageItems::test_system_note_show_timestamp_by_tag"]
```

```ava-contract
id = "compact-summary-header-roles"
kind = "invariant"
statement = """COMPACT_SUMMARY_HEADER is prepended to every replacement compact summary and is the read side's classifier for the untagged summary HumanMessage; it is also the standing "a compaction just happened" signal, without which an agent re-reads a /compact order in the summary's verbatim tail and runs it again every turn. The string lives in the leaf message-contract module so gateway and insights callers import it without agent.graph."""
code = ["base/agents/messages/kwargs.py:COMPACT_SUMMARY_HEADER"]
tests = ["gateway/tests/history/test_context_breakdown.py::test_bucket_messages_categorizes_every_kind", "gateway/tests/history/test_context_breakdown.py::test_bucket_messages_works_without_agent_graph"]
```

Parent: [[base/agents/messages/docs/kwargs.ava.okf.md|Message Metadata Contract]].
