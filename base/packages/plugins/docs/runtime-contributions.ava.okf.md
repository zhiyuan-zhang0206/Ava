---
type: doc
title: Plugin runtime contributions
description: Declaration ownership, load boundaries and activation attribution.
tags: [contracts]
---

# Plugin runtime contributions

## Immutable declarations

```ava-contract
id = "immutable-declarations"
kind = "invariant"
statement = "Plugin faces return frozen PluginContributions values. Merging preserves tuple declaration order and rejects multiple config owners; each ExtensionRegistry represents one enabled plugin set, and reload builds a new registry."
code = [
  "base/packages/plugins/extensions.py:PluginContributions",
  "base/packages/plugins/extensions.py:ExtensionRegistry",
]
tests = [
  "base/packages/plugins/tests/test_compact_contributions.py::test_merged_runtime_declarations_keep_reminder_order",
  "agent/graph/tests/test_build_with_extensions.py::test_build_registry_holds_what_each_enabled_plugin_declares",
]
```

`plugin.py` owns the SDK surface, `default_config.py` owns config, and `agent_runtime.py` owns hooks, state, prompt sections and compact preparation. Metrics, inspector and provider faces serve their own processes. There is no import-time global contribution ledger. The explicit model catalog travels into section builders. State fields are namespaced by plugin except the shared `messages` reducer; security findings, attachments and skill reads are SDK-owned core writes. Context notes may opt out with `None`; stable rank order puts lower ranks nearest the head, and `on_fork` is for notes whose inherited form would be wrong. SDK wraps stack in load then declaration order, with later wraps outermost. These consuming contracts are described in [plugin spec v2](../../../../docs/conventions/extensions/plugin-spec-v2.md).

## Runtime face admission

```ava-contract
id = "runtime-face-admission"
kind = "invariant"
statement = "The load gate compares manifest and provided contributions in both directions for the loaded faces. A failed declaration, manifest or state validation excludes that plugin and reports the failure without excluding other plugins. Declaring state without a supplied state gate is a caller error."
code = [
  "base/packages/plugins/loading/registry.py:declarations",
  "base/packages/plugins/loading/registry.py:build_registry",
]
tests = [
  "agent/tests/plugins/test_plugin_catalog.py::test_a_plugin_whose_manifest_and_contribute_disagree_on_hooks_is_excluded",
  "agent/tests/plugins/test_plugin_catalog.py::test_a_plugin_without_a_manifest_is_excluded",
  "agent/tests/plugins/test_plugin_catalog.py::test_an_invalid_state_declaration_excludes_the_plugin",
  "base/packages/plugins/tests/test_compact_contributions.py::test_manifest_must_match_compact_preparation",
]
```

Child surface-only loads must stay off the agent runtime and LM stack; heavyweight state validation is injected only by callers that run agent code. `loaded_state_classes` reads only the runtime faces already present in this process for serializer allowlisting. A broken face reports a sink failure and contributes no classes rather than crashing serialization. No discovery or import is needed on the per-payload read.

## Activation attribution

```ava-contract
id = "activation-attribution"
kind = "invariant"
statement = "A runtime activation joins its declared contribution on the same plugin, surface and identifier, carrying the current model. Framework contributions with no plugin attribution emit nothing. record contains ordinary telemetry exceptions while BaseException cancellation continues to propagate."
code = [
  "base/packages/plugins/activation.py:record",
  "base/packages/plugins/contributions.py:Contribution",
]
tests = [
  "base/tests/test_activation.py::test_records_an_attributed_firing",
  "base/tests/test_activation.py::test_unattributed_firing_records_nothing",
  "base/tests/test_activation.py::test_event_carries_the_model_in_force",
  "base/tests/test_activation.py::test_emit_failure_is_swallowed",
]
```

The registry says what was declared; activation events say what fired. Hooks count nonempty updates, SDK wraps count control-flow changes (inner called other than once), prompt sections count nonempty prompt-build output, and compact sections count nonempty text included in a soft reminder. State writes are already covered by hooks and SDK calls have their own metering. Count every firing, not once per run: removal decisions need per-model frequency. `detail` is human-readable context, never a parsed protocol; `emit` is unguarded and production callers use `record`.
