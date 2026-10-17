---
type: doc
title: Plugin and hook registration
description: "The agent kernel's plugin registration surfaces: the recorded two-layer hook / sdk-wrap split and the built-in plugin metadata fixture."
tags: []
---

# Plugin and hook registration

`agent/hooks/__init__.py` is the public face of the graph-edge hook layer: it
re-exports `Hook`, `HookName` and `make_hook_runner` from `_registry`, and the
`BUILTIN_PLUGINS` constant beside them.

```meiva-contract
id = "plugin-hook-layers"
kind = "decision"
statement = """The plugin system has two layers: SDK wraps modify the behavior of the meiva namespace (declared as SdkWrap, applied through meiva/sdk_surface), and graph-edge hooks intervene in the agent execution pipeline (declared through PluginContributions, run by the hook container nodes)."""
code = ["agent/hooks/__init__.py:<module>"]
decision = "docs/decisions/agents/graph/2026-05-13-plugin-and-hook-layers.md"
```

```meiva-contract
id = "builtin-plugins-test-fixture"
kind = "invariant"
statement = """BUILTIN_PLUGINS names the meiva_builtins/plugins/ meiva_* built-ins only for the metadata test — each listed name must have a plugin directory with a parseable plugin.py; runtime loading discovers plugins by filesystem scan and never reads the constant."""
code = ["agent/hooks/__init__.py:BUILTIN_PLUGINS"]
tests = ["agent/hooks/tests/test_builtin_metadata.py::test_builtin_plugins_is_tuple_of_str", "agent/hooks/tests/test_builtin_metadata.py::test_known_plugin_has_entry_point"]
```

Parent: [[agent/hooks/docs/hooks.meiva.okf.md|Hooks]].
