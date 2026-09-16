---
type: doc
title: Plugin Module Loading
description: How a plugin's `plugin.py` (and its optional `agent_runtime.py` face) becomes a live module — one by-path loader contract shared by host boot, graph build, and the agent-launched child (dotted name, `sys.modules` registration before execution, reload-in-place), fail-soft containment of a broken plugin, and the inventory errors that stay fail-closed.
tags:
- plugins
---

# Plugin Module Loading

## One loader contract, three production call sites
`agent/_extensions.py:load_extensions()` imports every enabled plugin's
`plugin.py` (plus its optional `agent_runtime.py` face on the full form) by
path, and `ava._extend.scan_and_load()` does the same for the external plugins
at host boot (`agent/_process_boot.py:load_process_extensions`). The
agent-launched child enters through the same loader (`ava._ensure_plugins_loaded`);
its stateless form loads surfaces only ([[okf/plugins/module-loading/two-faces.ava.okf.md]]).
All drive the same primitives (`ava/_extend.py`), so a plugin sees the same
module name, `__package__`, `sys.modules` identity, and containment whichever
production path imports it (issue #2161 — before unification the boot loader
exec'd plugins under a top-level name and relative imports crashed the agent
host). Each import runs inside `with PluginContext(name):`, which attributes
the `register_*` calls it triggers.

The name given to `importlib.util.spec_from_file_location` is **dotted**
(`ava_builtins.plugins.<name>.plugin` built-in, `plugins.<name>.plugin`
external), so importlib sets `__package__` and a `from . import x` inside
`plugin.py` resolves. A directory under `shared/paths.py:repo_plugins_dir()`
is built-in; anything else is external.

Load order is the `config.plugins` dict order (alphabetical), one by one —
**no dependency declaration, no topological sort**. Configs are bound uniformly by `bind_from_disk()`
only after every import has completed, so a hook firing later always finds
`ava._settings.plugins.<n>` populated.

## Two faces: `plugin.py` surface, `agent_runtime.py` face
A plugin loads in up to two faces, and each load form picks which run: the
child's stateless boot loads surfaces only; the full form (graph build, host
boot, a stateful child's upgrade) adds the optional `agent_runtime.py` face
carrying the agent-side registrations (state fields, hooks, prompt sections).
Per-face containment, the face's dotted name, and the load forms in full:
[[okf/plugins/module-loading/two-faces.ava.okf.md]].

## Disabled means never imported
The enable set comes from the per-machine `plugins_config.json`, read by both
loaders through `shared/plugins_config`: host boot via `load_for_runtime()`
(a long-lived consumer — a dangling entry is warned and skipped), the graph
loader via `load()`, falling back to `load(allow_dangling=True)` after
reporting each dangling name through the canonical reporter. A plugin with
`enabled: false` is imported by neither `plugin.py` loader (issue #2161: the
boot loader imported every directory on disk regardless of config, so `ava
plugins disable` changed nothing about startup). Host boot passes the enabled
set of the *external* plugins it scans; the graph build adds the built-ins.
Machine-level roster paths sit outside the enable plane by design: the
`services.py` roster and the shipped-`metrics.py` scan key on presence, not
enable-state (`ops/spec.py:_plugin_services`).

## The external `plugins` prefix is registered, not resolved from sys.path
`register_plugin_parent_packages` (`ava/_extend.py`, applied by the loader for
external plugins) registers `plugins` over `$AVA_HOME/plugins` and
`plugins.<name>` over the plugin's directory before executing `plugin.py`. It
does not rely on `$AVA_HOME` being on `sys.path`: the exec child boots with
`cwd=$AVA_HOME/source` under `python -I`, where `import plugins` resolves to
a legacy checkout `plugins/` dir or to nothing, so `from . import _sibling`
inside `plugin.py` used to raise `ModuleNotFoundError` and take `import ava`
down with it (2026-08-28 ava_ledger incident). Existing `sys.modules` entries are left untouched, and
built-ins are excluded: they resolve through the real `ava_builtins.plugins`
package.

## Fail-soft: a broken plugin is skipped, never fatal
A plugin whose `plugin.py` raises at import (missing sibling, syntax error,
top-level exception) degrades to a **skip with a loud report**, never a
blocked `import ava` / host boot / graph build: `safe_load_plugin_module`
drops the half-executed module from `sys.modules`, and
`shared/plugin_load_report.py:report_plugin_load_failure` emits a loguru ERROR
carrying the traceback plus one `plugin_load_failed` telemetry event (anomaly
tier). The remaining enabled plugins keep loading; a config entry whose
plugin directory is gone (`DanglingPlugin`) is reported and treated as
disabled. `KeyboardInterrupt` / `SystemExit` still propagate — cancellation is
not a plugin failure.

The same containment applies at the other plugin-code load sites, each
reporting through the one reporter: a plugin's `provider.py`
(`shared/lm/_plugin_providers.py`), `services.py` (`ops/spec.py`), `setup.py`
(`cli/commands/_converge_plugins.py`), a built-in plugin's `metrics.py`
(`gateway/routers/_plugin_metrics.py`), the gateway plugin inspector's
`inspector.py` (`gateway/routers/_plugin_inspector.py`), and the launched
child's `import ava` self-load (`ava._ensure_plugins_loaded`, plus a stderr
line — a child usually has no log sink). One contained site stays off that
reporter: `default_config.py` images surface as `error`-status entries on the
plugin-update result (`shared/plugins_config.py:update_all_disk_images`).

## Semantics boundary: what stays fail-closed
Containment covers *code* that fails to load; inventory and contract conflicts
stay hard instead of guessing at the operator's intent (duplicate plugin name,
malformed `plugins_config.json`, config schema drift, provider
registration-contract violations, post-load revalidation) — and the release
probe re-raises the contained failures of the faces it exercises (the
`plugin.py` loader, dangling entries included; the `services.py` roster;
provider registration). See
[[okf/plugins/module-loading/fail-closed-boundaries.ava.okf.md]].

## Registration precedes execution
The module object is placed in `sys.modules` **before** `exec_module` runs, not
after. A `BaseModel` defined inside a plugin triggers Pydantic's
`__init_subclass__`, which resolves `Annotated[T, reducer]` string annotations
via `get_type_hints` reading `sys.modules[cls.__module__].__dict__`. With no
entry there, the annotation stays a `ForwardRef`, and the later
`StateGraph(schema)` build raises `NameError` far from the cause.

## Reload semantics
A repeat load re-executes the module object already registered for that file
(`importlib.reload` semantics — module identity stays stable per process), and
reload is not a lifecycle: the reset covers the framework registries only.
In full: [[okf/plugins/module-loading/reload-semantics.ava.okf.md]].

## Key Dependencies
- [[okf/plugins/plugins.ava.okf.md]] — the injection surfaces the import registers into
- [[agent/graph/graph.ava.okf.md]] — `build_graph()` calls the loader (`agent/_extensions.load_extensions`) before wiring nodes
- [[extensions.ava.okf.md]] — the `ava.extend.wrap` layer a reload re-installs from a pristine core
