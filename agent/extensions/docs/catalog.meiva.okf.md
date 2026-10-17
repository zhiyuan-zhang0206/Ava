---
type: doc
title: Extension catalog
description: Live extension signatures and per-machine declared contribution inspection.
tags: [contracts]
---

# Extension catalog

## Catalog reports load facts

```meiva-contract
id = "catalog-reports-load-facts"
kind = "invariant"
statement = "The catalog renders declaration signatures from live objects and reads enabled plugins through the production loader. A disabled plugin has enable-state but no imported declarations. Reload does not accumulate contributions, and unknown plugin names fail with the installed names."
code = [
  "agent/extensions/catalog.py:build_catalog",
  "agent/extensions/catalog.py:entry_point_signature",
  "agent/extensions/catalog.py:Catalog.plugin",
]
tests = [
  "agent/tests/plugins/test_plugin_catalog.py::test_every_surface_entry_point_renders_its_live_signature",
  "agent/tests/plugins/test_plugin_catalog.py::test_a_disabled_plugin_reports_no_declarations",
  "agent/tests/plugins/test_plugin_catalog.py::test_a_reload_does_not_accumulate_contributions",
  "agent/tests/plugins/test_plugin_catalog.py::test_unknown_plugin_fails_fast_and_names_the_installed_ones",
]
```

Run catalog construction in a short-lived inspection process: imports install SDK namespaces/wraps and bind plugin config just as boot does. Data faces are loaded to describe their declarations; their serving processes build separate registries. Deferred annotations render as source text to avoid both quoted annotations and unreadably expanded resolved types. The catalog keeps live declaration objects so moving a type fails on import instead of leaving a stale signature.

## Manifest diff scope

```meiva-contract
id = "manifest-diff-scope"
kind = "invariant"
statement = "Declared-vs-registered comparison covers surfaces with both a manifest key and a runtime registry. Install-only keys and undeclarable surfaces are reported separately, not mislabeled as drift. Config compares presence rather than the private config class name."
code = [
  "agent/extensions/catalog.py:declared_vs_registered",
  "agent/extensions/catalog.py:_declared_identifiers",
]
tests = [
  "agent/tests/plugins/test_plugin_catalog.py::test_declared_vs_registered_reports_both_directions",
  "agent/tests/plugins/test_plugin_catalog.py::test_install_time_manifest_keys_have_no_runtime_registry",
]
```

Declaration-only keys are the complement of mapped keys, so adding a manifest key cannot silently remove it from inspection. These are registration facts; firing frequency comes from `plugin_activation` and is never inferred from a source scan or contribution count.
