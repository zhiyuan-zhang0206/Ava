---
type: doc
title: Built-ins
description: Bundled commands, plugins, instruction skills and MCP integrations shipped with Meiva.
tags: []
---

# Built-ins

`meiva_builtins/` contains packages shipped with the repository. These are
concrete implementations and instruction packages, separate from the generic
extension mechanisms.

- `meiva_builtins/commands/` — bundled Composer prompt templates (`compact`, `recap`),
  discovered by `base/packages/composer_commands.py` in source and wheel installs.
  User templates in `$MEIVA_HOME/commands/` may override them. Planning and scope
  alignment use the existing `meiva-workflow` skill; no built-in `plan` template remains.
- [[meiva_builtins/plugins/docs/plugins.meiva.okf.md]] — bundled plugin implementations.
- [[meiva_builtins/skills/docs/skills.meiva.okf.md]] — bundled instruction catalog.
- [[meiva_builtins/mcps/docs/mcps.meiva.okf.md]] — bundled MCP integrations.

Shared package wiring is described by [[meiva_builtins/docs/extensions.meiva.okf.md]].
Mechanism contracts live in [[docs/plugins/plugins.meiva.okf.md]],
[[docs/skills/skills.meiva.okf.md]] and [[docs/mcps/mcps.meiva.okf.md]].
