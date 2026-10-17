---
type: doc
title: Prompt composition contracts
description: Behavioral constraints retained alongside their implementation and tests.
tags: [agent]
---

# Prompt composition contracts

```meiva-contract
id = "context-order"
kind = "invariant"
statement = "Framework and plugin notes are stably sorted by cache rank, placing stable settings before changing memory indexes. Agent identity and concrete workspace paths live in re-graftable notes rather than the shared system prompt, so forks do not inherit source identity. The timezone IANA name remains authoritative when an offset becomes stale across DST. Fork skill additions append at the tail to preserve the inherited prefix."
code = ["agent/graph/prompt/context_notes.py:<module>"]
tests = ["agent/graph/tests/prompt/test_timezone_note.py::test_sits_in_the_stable_cache_band", "agent/graph/tests/prompt/test_agent_id_note.py::test_workspace_clause_carries_the_concrete_path", "agent/tests/history/test_fork_notes.py::test_fork_tail_grafts_delta_skills_from_inbound_payload"]
```

```meiva-contract
id = "sdk-expansion"
kind = "invariant"
statement = "Plugin expansions lead the deduplicated configured expansion list. Wildcards recursively discover public namespaces while excluding disabled entries and capability surfaces; explicitly naming a capability member is refused to avoid injecting whole skill bodies or tool schemas and falsely attributing them as loaded. Media filtering is per rendering call, and installed extensions are visible before rendering captures the SDK overview. The system prompt can be rebuilt when an empty context window is initialized. Delegation checks give the exact `meiva.help(meiva.skills.meiva_workflow)` invocation syntax for non-trivial work."
code = ["agent/graph/prompt/system_prompt.py:<module>"]
tests = ["agent/graph/tests/prompt/test_system_prompt.py::test_member_of_a_capability_surface_is_refused", "agent/graph/tests/prompt/test_system_prompt.py::test_wildcard_respects_sdk_disable", "agent/graph/tests/prompt/test_system_prompt.py::test_plugin_expansions_lead_the_wildcard", "agent/graph/tests/prompt/test_system_prompt.py::test_section_hides_attach_for_text_only_model", "agent/graph/tests/prompt/test_capabilities_index.py::test_delegation_check_routes_nontrivial_work_without_forcing_methods"]
```

```meiva-contract
id = "codeact-batching"
kind = "invariant"
statement = "The CodeAct section (`codeact_section`) is owned as its own module so `system_prompt.py` stays within its line ceiling while preserving explicit registration order. When `prompt_codeact_enabled` is on, it directs the agent to batch known operations into one `execute_code(code: str)` call when no intermediate review is needed."
code = ["agent/graph/prompt/_codeact.py:<module>"]
tests = [
  "agent/graph/tests/prompt/test_system_prompt_sections.py::test_codeact_section_defaults_to_on",
  "agent/graph/tests/prompt/test_system_prompt_sections.py::test_codeact_section_gating",
  "agent/graph/tests/prompt/test_system_prompt_sections.py::test_codeact_section_urges_batching",
]
```

```meiva-contract
id = "conversation-policy"
kind = "invariant"
statement = "Core reply routing, language, and milestone progress rules in `user_reply_section` remain active across every `COMMUNICATION_STYLE_SECTIONS` setting (`off`, `oriented`, `concise`, `silent`). Whenever the first turn on a human request calls `execute_code`, it must include a brief substantive opening reply before the first tool call; intermediate turns call `execute_code` with no assistant text unless a milestone is reached. `COMMUNICATION_STYLE_SECTIONS` keys match the non-`off` `agent_communication_style` Literal members so an unknown style raises at render time."
code = ["agent/graph/prompt/conversation.py:<module>"]
tests = [
  "agent/graph/tests/prompt/test_conversation_prompt.py::test_initial_human_response_is_present_without_plugins",
  "agent/graph/tests/prompt/test_conversation_prompt.py::test_reply_routing_and_handoff_policy_survive_every_style",
  "agent/graph/tests/prompt/test_conversation_prompt.py::test_language_policy_survives_optional_guidance",
  "agent/graph/tests/prompt/test_conversation_prompt.py::test_user_facing_text_policy_survives_every_style",
  "agent/graph/tests/prompt/test_conversation_prompt.py::test_milestone_policy_preserves_results_and_questions",
  "agent/graph/tests/prompt/test_system_prompt_sections.py::test_every_narrating_style_renders_a_distinct_section",
]
```

```meiva-contract
id = "capabilities-index"
kind = "invariant"
statement = "The `# Capabilities` section is the system prompt's single index of available skills and live MCP servers, rendering only non-empty halves and omitting the section when both are empty. It directs the agent to match every task against the index before starting so established playbooks and domain capabilities are not skipped or rebuilt. Skill resolution shares one resolver across the index and preloaded skills, folding descendants under selected ancestor entry skills on wildcard selection while preserving full catalog membership for drift tracking and preloading. Untrusted skill descriptions are flattened and truncated to one 300-character line, and injection-flagged descriptions are withheld from the index and excluded from recall candidates along with preloaded skills. Disable checks honor configured `Prompt.sdk_disable` and runtime eval-isolation removals while leaving unconfigured missing paths eligible for expansion diagnostics. Index drift diffs live membership in one scan and caps additions at 20 lines while advancing the snapshot over full membership. Prompt assembly records no skill attribution and requests no capability report in final output."
code = ["agent/graph/prompt/capabilities.py:<module>"]
tests = [
  "agent/graph/tests/prompt/test_capabilities_index.py::test_inject_default_is_the_full_catalog",
  "agent/graph/tests/prompt/test_capabilities_index.py::test_default_index_lists_every_loaded_skill",
  "agent/graph/tests/prompt/test_capabilities_index.py::test_wildcard_groups_descendants_under_real_entry_skills",
  "agent/graph/tests/prompt/test_capabilities_index.py::test_index_line_is_one_line_however_the_description_was_written",
  "agent/graph/tests/prompt/test_capabilities_index.py::test_header_only_promises_the_halves_that_rendered",
  "agent/graph/tests/prompt/test_capabilities_index.py::test_default_index_does_not_request_a_capability_report",
  "agent/graph/tests/prompt/test_capabilities_index.py::test_empty_index_renders_nothing",
  "agent/graph/tests/prompt/test_capabilities_index.py::test_delegation_check_drops_the_index_step_when_there_is_no_index",
  "agent/graph/tests/prompt/test_capabilities_index.py::test_building_the_prompt_records_no_skill_attribution",
  "agent/graph/tests/prompt/test_capabilities_index.py::test_flagged_description_withheld_from_index",
  "agent/graph/tests/prompt/test_capabilities_index.py::test_runtime_removed_surface_renders_nothing",
  "agent/graph/tests/prompt/test_capabilities_index.py::test_applied_disable_registry_marks_runtime_removed_path",
  "agent/graph/tests/prompt/test_capabilities_index.py::test_unregistered_missing_path_is_not_sdk_disabled",
]
```
