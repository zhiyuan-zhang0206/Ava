---
type: doc
title: Built-ins
description: Bundled commands, plugins, instruction skills and MCP integrations shipped with Ava.
tags: []
---

# Built-ins

`ava_builtins/` contains packages shipped with the repository. These are
concrete implementations and instruction packages, separate from the generic
extension mechanisms.

- `ava_builtins/commands/` — bundled Composer prompt templates (`compact`, `recap`),
  discovered by `ava/skills/composer_commands.py` in source and wheel installs.
  User templates in `$AVA_HOME/commands/` may override them. Planning and scope
  alignment use the existing `ava-workflow` skill; no built-in `plan` template remains.
- [[ava_builtins/plugins/docs/plugins.ava.okf.md]] — bundled plugin implementations.
- [[ava_builtins/skills/docs/skills.ava.okf.md]] — bundled instruction catalog.
- [[ava_builtins/mcps/docs/mcps.ava.okf.md]] — bundled MCP integrations.

Shared package wiring is described by [[ava_builtins/docs/extensions.ava.okf.md]].
Mechanism contracts live in [[docs/plugins/plugins.ava.okf.md]],
[[docs/skills/skills.ava.okf.md]] and [[docs/mcps/mcps.ava.okf.md]].
