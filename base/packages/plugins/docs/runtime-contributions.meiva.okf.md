---
type: doc
title: Plugin runtime contributions
description: Declaration ownership, load boundaries and activation attribution.
tags: [contracts]
---

# Plugin runtime contributions

## Immutable declarations

Plugin faces return frozen PluginContributions values. Merging preserves tuple declaration order and rejects multiple config owners; each ExtensionRegistry represents one enabled plugin set, and reload builds a new registry.

`plugin.py` owns the SDK surface, `default_config.py` owns config, and `agent_runtime.py` owns hooks, state, prompt sections and compact preparation. Metrics, inspector and provider faces serve their own processes. There is no import-time global contribution ledger. The explicit model catalog travels into section builders. State fields are namespaced by plugin except the shared `messages` reducer; security findings, attachments and skill reads are SDK-owned core writes. Context notes may opt out with `None`; stable rank order puts lower ranks nearest the head, and `on_fork` is for notes whose inherited form would be wrong. SDK wraps stack in load then declaration order, with later wraps outermost. These consuming contracts are described in [plugin spec v2](../../../../docs/conventions/extensions/plugin-spec-v2.md).

## Runtime face admission

The load gate compares manifest and provided contributions in both directions for the loaded faces. A failed declaration, manifest or state validation excludes that plugin and reports the failure without excluding other plugins. Declaring state without a supplied state gate is a caller error.

Child surface-only loads must stay off the agent runtime and LM stack; heavyweight state validation is injected only by callers that run agent code. `loaded_state_classes` reads only the runtime faces already present in this process for serializer allowlisting. A broken face reports a sink failure and contributes no classes rather than crashing serialization. No discovery or import is needed on the per-payload read.

## Activation attribution

A runtime activation joins its declared contribution on the same plugin, surface and identifier, carrying the current model. Framework contributions with no plugin attribution emit nothing. record contains ordinary telemetry exceptions while BaseException cancellation continues to propagate.

The registry says what was declared; activation events say what fired. Hooks count nonempty updates, SDK wraps count control-flow changes (inner called other than once), prompt sections count nonempty prompt-build output, and compact sections count nonempty text included in a soft reminder. State writes are already covered by hooks and SDK calls have their own metering. Count every firing, not once per run: removal decisions need per-model frequency. `detail` is human-readable context, never a parsed protocol; `emit` is unguarded and production callers use `record`.
