"""Implementation of the `ava` SDK entry surface — what the agent sees and how —
plus the per-call plumbing every SDK namespace shares.

Each module holds one cohesive slice of the entry-point machinery:

- `const` — the `ava.const()` documented-value factory;
- `sdk_disable` — the `AVA_SDK_DISABLE` machinery (parse + sentinel swap);
- `discovery` — kind predicates, `_Constant`, and the module/class child
  walkers behind `agent_visible_names` (the single source of truth shared by
  help rendering, SDK-expand discovery, doc linting, and metering);
- `help` — the `ava.help()` renderer (stub-format docs for SDK targets);
- `install` — the one writer of the `ava` module: installs a registry's SDK
  surface (namespaces, members, expansions, wraps, skill sources, flags, config)
  and undoes it, producing one frozen `Installation` (wrap layers, skill
  providers, metering ledger, applied-disable set, faces flag) held in a single
  slot on the `ava` module — a change builds a new value and swaps the holder;
- `plugins` — the namespace / member install primitives and their exception
  hierarchy;
- `wraps` — the wrap layer primitive `install` applies
  (`ava/__init__.py` builds the curated `ava.extend` surface from it);
- `plugin_loader` — the plugin-by-path loader (`load_plugin_module`,
  `safe_load_plugin_module`) `agent.extensions.load_extensions` drives;
- `skill_sources` — plugin-contributed skill-root providers, carried by the
  installation and kept off the `ava.skills` namespace so the installer can write
  them even when `AVA_SDK_DISABLE` stubs that namespace out; `scoped` holds one
  extra for the duration of a call.

The per-call plumbing the namespaces (`ava.files`, `ava.web`, ...) share:

- `validation` — argument coercion (`coerce_str` / `coerce_typed`) at every SDK
  entry point, plugin namespaces included;
- `batch` — the bounded concurrent executor behind the SDK batch APIs
  (`ava.web.search` / `ava.web.fetch` / `ava.understand`);
- `metering` — the transparent per-call recorder that emits one `sdk_call`
  event per top-level `ava.*` call.

`ava/__init__.py` re-exports the framework entry points (`ava.help`, ...). The
agent kernel drives rendering by passing `help()` its render parameters, and reads
the installed surface through `install.installed()` / `install.expansions()` and the
`sdk_disable` entries. None of it is agent-facing: `help(ava)` lists only the
`__all_for_ava__` whitelist, and `AVA_SDK_DISABLE` refuses to disable a
framework module such as this package.
"""

import sys as _sys
from typing import Any, cast


def ava_module() -> Any:
    """The `ava` package module itself — reached through `sys.modules` so
    these private modules never create an import cycle during `import ava`."""
    return cast(Any, _sys.modules["ava"])
