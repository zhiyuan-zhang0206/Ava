---
type: doc
title: Built-in plugins
description: Bundled plugin implementations and their component documentation.
tags: [extensions]
---

# Built-in plugins

These packages ship with Ava. The generic registration, contribution surfaces,
loading and manifest contracts belong to [[docs/plugins/plugins.ava.okf.md]].
Each implementation below owns its behavior and component documentation.

| Plugin | Responsibility |
|---|---|
| [[ava_builtins/plugins/ava_code/docs/ava_code.ava.okf.md]] | Coding workspace and context |
| [[ava_builtins/plugins/ava_fleet/docs/ava_fleet.ava.okf.md]] | Agent collaboration and tasks |
| [[ava_builtins/plugins/ava_memory/docs/ava_memory.ava.okf.md]] | Long-term memory |
| [[ava_builtins/plugins/ava_sdk_reminder/docs/ava_sdk_reminder.ava.okf.md]] | SDK usage reminders |
| [[ava_builtins/plugins/ava_silent_idle/docs/ava_silent_idle.ava.okf.md]] | Silent-turn continuation |
| [[ava_builtins/plugins/ava_syntax_fix/docs/ava_syntax_fix.ava.okf.md]] | Deterministic code corrections |

Skills and MCP packages have separate catalogs under Built-ins; plugin-owned
skills remain under their plugin's component tree.
