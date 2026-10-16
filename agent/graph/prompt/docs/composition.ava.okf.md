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

```ava-contract
id = "capabilities-index"
kind = "invariant"
statement = "The `# Capabilities` section is the system prompt's single index of available skills and live MCP servers, rendering only non-empty halves and omitting the section when both are empty. Skill resolution shares one resolver across the index and preloaded skills, folding descendants under selected ancestor entry skills on wildcard selection while preserving full catalog membership for drift tracking and preloading. Untrusted skill descriptions are flattened and truncated to one 300-character line, and injection-flagged descriptions are withheld from the index and excluded from recall candidates along with preloaded skills. Disable checks honor both configured `Prompt.sdk_disable` and runtime eval-isolation removals while leaving unconfigured missing paths eligible for expansion diagnostics. Index drift diffs live membership in a single scan and caps additions at 20 lines while advancing the snapshot over the full membership. Prompt assembly records no skill attribution and requests no capability-selection report in final output."
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

`CAPABILITY_SURFACES` (`skills` and `mcps`) are capability indexes rather than ordinary SDK namespaces, so wildcard SDK expansion skips them to avoid rendering a duplicate index; an operator may still name a surface root explicitly in `AVA_SDK_EXPAND`, while naming a member inside a surface (`skills.gmail`) is refused because resolving it through `getattr` would inline the full `SKILL.md` body or MCP tool schema into every prompt and record a false `loaded` attribution.

`resolve_prompt_skills` resolves `*` to the whole loaded catalog and otherwise matches each configured entry by `.`-identifier first and bare frontmatter name second through `base.packages.skills.names.match_key`, logging a warning naming `config_field` and skipping unresolved entries. Under wildcard indexing, `_skill_index_lines` folds descendants only when an actual ancestor skill is selected, so namespace folders without a `SKILL.md` never hide leaf skills. `index_drift` takes a single live scan so `identifiers` and `added` describe the same instant; disappearing skills are dropped from the snapshot without a removal note because `ava.skills.<name>` already fails fast and dropping the identifier lets a later reinstall announce itself again.
