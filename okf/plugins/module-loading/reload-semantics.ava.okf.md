---
type: doc
title: Plugin Load Reload Semantics
description: A repeat load re-executes the already-registered module object (identity stays stable per file), what a reload does not undo, and why the child's surface form never resets.
tags:
- plugins
---

# Plugin Load Reload Semantics

## Reload in place, never replace
A repeat load — either loader — **re-executes the module object already
registered for that file** (`importlib.reload` semantics) rather than binding
a second one, so module identity is stable for the life of the process: the
boot load and the graph build share one module object per plugin.

Replacing the entry instead leaves whoever imported the plugin earlier — a
module-level `from ...plugin import hook` — holding the old object, while
`mock.patch` and every dotted-path `getattr` resolve the new one; a patch then
lands on a module nothing under test is running. Issue #147: two
`tests/agent/test_syntax_fix.py` cases passed alone and failed whenever a
plugin-loading sibling ran first in the same xdist worker.

Reuse is keyed on the **file**, not merely the dotted name: a different
`plugin.py` claiming a registered name (synthetic test plugins under tmp dirs)
is a different module and gets a fresh object, so the dead file's globals
never leak into the live one.

## What a reload does not undo
Reload is not a lifecycle. `clear_plugin_registrations()` at the top of the
graph-build load clears the framework-side registries (hooks, state fields,
prompt contributors, namespaces, the attribution ledger) and
`ava._extend.clear_wraps` restores every wrapped target to its captured
original, so registration starts from a pristine core each time. The surface
form never resets: a child loads once, and importing `agent.state` for the
reset would defeat the split (task #3633). Anything a
plugin allocated at import time (a connection, a thread, a file handle) is
re-created; disposing it is the plugin-spec-v2 S4 dispose contract, not
implemented. This is also not in-process hot reload:
the reload boundary stays the agent process's `self.restart`
([plugin-spec-v2](../../../conventions/plugin-spec-v2.md)).
