---
type: doc
title: Prompt composition contracts
description: Behavioral constraints retained alongside their implementation and tests.
tags: [agent]
---

# Prompt composition contracts

```ava-contract
id = "context-order"
kind = "invariant"
statement = "Framework and plugin notes are stably sorted by cache rank, placing stable settings before changing memory indexes. Agent identity and concrete workspace paths live in re-graftable notes rather than the shared system prompt, so forks do not inherit source identity. The timezone IANA name remains authoritative when an offset becomes stale across DST. Fork skill additions append at the tail to preserve the inherited prefix."
code = ["agent/graph/prompt/context_notes.py:<module>"]
tests = ["agent/graph/tests/prompt/test_timezone_note.py::test_sits_in_the_stable_cache_band", "agent/graph/tests/prompt/test_agent_id_note.py::test_workspace_clause_carries_the_concrete_path", "agent/tests/history/test_fork_notes.py::test_fork_tail_grafts_delta_skills_from_inbound_payload"]
```

```ava-contract
id = "sdk-expansion"
kind = "invariant"
statement = "Plugin expansions lead the deduplicated configured expansion list. Wildcards recursively discover public namespaces while excluding disabled entries and capability surfaces; explicitly naming a capability member is refused to avoid injecting whole skill bodies or tool schemas and falsely attributing them as loaded. Media filtering is per rendering call, and installed extensions are visible before rendering captures the SDK overview. The system prompt can be rebuilt when an empty context window is initialized."
code = ["agent/graph/prompt/system_prompt.py:<module>"]
tests = ["agent/graph/tests/prompt/test_system_prompt.py::test_member_of_a_capability_surface_is_refused", "agent/graph/tests/prompt/test_system_prompt.py::test_wildcard_respects_sdk_disable", "agent/graph/tests/prompt/test_system_prompt.py::test_plugin_expansions_lead_the_wildcard", "agent/graph/tests/prompt/test_system_prompt.py::test_section_hides_attach_for_text_only_model"]
```
