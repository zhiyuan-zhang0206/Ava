---
type: doc
title: Plugin Module Loading
description: How `_load_extensions()` turns an enabled plugin's `plugin.py` into a live module — path import under a dotted name, `sys.modules` registration before execution, and reload-in-place so a plugin module's identity is stable for the life of the process.
tags:
- plugins
---

# Plugin Module Loading

## Path import under a dotted name
`agent/graph/_build.py:_load_extensions()` imports every enabled plugin's
`plugin.py` itself, **by path** — built-in and external in one loop, no
delegation to `ava._extend.scan_and_load` (that loader is external-only and is
called once from `agent/loop.py`). Each import runs inside
`with PluginContext(name):`, which is what attributes the `register_*` calls
the import triggers.

The name given to `importlib.util.spec_from_file_location` is **dotted** —
`ava_builtins.plugins.<name>.plugin` for a built-in, `plugins.<name>.plugin`
for an external one — so importlib sets `__package__` and a `from . import x`
inside `plugin.py` resolves. Discovery decides which of the two prefixes
applies: a plugin directory under `shared/paths.py:repo_plugins_dir()` is
built-in, anything else is external.

Load order is the order of the `config.plugins` dict (alphabetical, from
`shared/plugins_config.py`), imported one by one — **no dependency declaration
and no topological sort**. Configs are bound uniformly by `bind_from_disk()`
only after every import has completed, so a hook firing later always finds
`ava._settings.plugins.<n>` populated.

## Registration precedes execution
The module object is placed in `sys.modules` **before** `exec_module` runs, not
after. A `BaseModel` defined inside a plugin triggers Pydantic's
`__init_subclass__`, which calls `get_type_hints` to resolve the string
annotations of `Annotated[T, reducer]` fields, and `get_type_hints` reads the
eval globals out of `sys.modules[cls.__module__].__dict__`. With no entry
there, the annotation stays a `ForwardRef`, and the later
`StateGraph(schema)` build raises `NameError` far from the cause.

## Reload in place, never replace
A repeat `_load_extensions()` **re-executes the module object already
registered for that file** — `importlib.reload` semantics — rather than binding
a second one. Module identity is therefore stable for the life of the process:
a reference captured before a load and a `sys.modules` lookup after it are the
same object, backed by one `__dict__`.

That matters because the two diverge silently otherwise. Replacing the entry
leaves whoever imported the plugin earlier — a module-level `from ...plugin
import hook`, a hook instance built at import time — holding the old object,
while `mock.patch` and every dotted-path `getattr` resolve the new one. A patch
then lands on a module that nothing under test is running. Issue #147 is the
worked example: two `tests/agent/test_syntax_fix.py` cases passed alone and
failed whenever a plugin-loading sibling ran first in the same xdist worker.

Reuse is keyed on the **file**, not merely the dotted name. A different
`plugin.py` claiming a name an earlier load registered (synthetic plugins under
tmp directories in tests) is a different module and gets a fresh object, so the
dead file's globals never leak into the live one.

## What a reload does not undo
Reload is not a lifecycle. `clear_plugin_registrations()` at the top of the
load clears the framework-side registries (hooks, state fields, prompt
contributors, namespaces, the attribution ledger) and `ava._extend.clear_wraps`
restores every wrapped target to its captured original, so registration starts
from a pristine core each time. Anything a plugin allocated for itself at
import time — a connection, a thread, a file handle — is simply re-created;
disposing it is the plugin-spec-v2 S4 dispose contract, which is not
implemented. This is also not in-process hot reload: the reload boundary stays
the agent process's `self.restart`
([plugin-spec-v2](../../conventions/plugin-spec-v2.md)).

## Key Dependencies
- [[okf/plugins/plugins.ava.okf.md]] — the injection surfaces the import registers into
- [[agent/graph/graph.ava.okf.md]] — `build_graph()` calls the loader before wiring nodes
- [[extensions.ava.okf.md]] — the `ava.extend.wrap` layer a reload re-installs from a pristine core
