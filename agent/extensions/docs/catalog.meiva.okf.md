---
type: doc
title: Extension catalog
description: Live extension signatures and per-machine declared contribution inspection.
tags: [contracts]
---

# Extension catalog

## Catalog reports load facts

The catalog renders declaration signatures from live objects and reads enabled plugins through the production loader. A disabled plugin has enable-state but no imported declarations. Reload does not accumulate contributions, and unknown plugin names fail with the installed names.

Run catalog construction in a short-lived inspection process: imports install SDK namespaces/wraps and bind plugin config just as boot does. Data faces are loaded to describe their declarations; their serving processes build separate registries. Deferred annotations render as source text to avoid both quoted annotations and unreadably expanded resolved types. The catalog keeps live declaration objects so moving a type fails on import instead of leaving a stale signature.

## Manifest diff scope

Declared-vs-registered comparison covers surfaces with both a manifest key and a runtime registry. Install-only keys and undeclarable surfaces are reported separately, not mislabeled as drift. Config compares presence rather than the private config class name.

Declaration-only keys are the complement of mapped keys, so adding a manifest key cannot silently remove it from inspection. These are registration facts; firing frequency comes from `plugin_activation` and is never inferred from a source scan or contribution count.
