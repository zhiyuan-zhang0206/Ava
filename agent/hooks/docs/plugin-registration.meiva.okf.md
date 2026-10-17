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

Parent: [[agent/hooks/docs/hooks.meiva.okf.md|Hooks]].
