"""Implementation of the `meiva` SDK entry surface — what the agent sees and how —
plus the per-call plumbing every SDK namespace shares.

Each module holds one cohesive slice of the entry-point machinery:

- `const` — the `meiva.const()` documented-value factory;
- `sdk_disable` — the `MEIVA_SDK_DISABLE` machinery (parse + sentinel swap);
- `discovery` — kind predicates, `_Constant`, and the module/class child
  walkers behind `agent_visible_names` (the single source of truth shared by
  help rendering, SDK-expand discovery, doc linting, and metering);
- `help` — the `meiva.help()` renderer (stub-format docs for SDK targets);
- `installation` — the frozen `Installation` value (wrap layers, skill
  providers, metering ledger, applied-disable set, faces flag), the single slot
  on the `meiva` module that holds it, and `installed()`, the read every other SDK
  piece shares;
- `install` — the one writer of the `meiva` module: installs a registry's SDK
  surface (namespaces, members, expansions, wraps, skill sources, flags, config)
  and undoes it, producing one `Installation` — a change builds a new value and
  swaps the holder;
- `process_context` — the process-local SDK entry: the context and exec-turn state slots behind
  `meiva.context` / `meiva.state` / `meiva.state_update`, the shared-host posture and the loaded code
  image (`meiva/boot.py` builds the contexts and clients and loads plugins above it);
- `plugins` — the namespace / member install primitives and their exception
  hierarchy;
- `wraps` — the wrap layer primitive `install` applies
  (`meiva/__init__.py` builds the curated `meiva.extend` surface from it);
- `extensions` — `load_extensions`: asks `base.packages.plugins.loading` (plugin discovery, the
  by-path loader, the registry of what plugins declare) to import the enabled plugins' faces and
  hands the result to `install`;
- `skill_sources` — plugin-contributed skill-root providers, carried by the
  installation and kept off the `meiva.skills` namespace so the installer can write
  them even when `MEIVA_SDK_DISABLE` stubs that namespace out; `scoped` holds one
  extra for the duration of a call.

The per-call plumbing the namespaces (`meiva.files`, `meiva.web`, ...) share:

- `validation` — argument coercion (`coerce_str` / `coerce_typed`) at every SDK
  entry point, plugin namespaces included;
- `batch` — the bounded concurrent executor behind the SDK batch APIs
  (`meiva.web.search` / `meiva.web.fetch` / `meiva.understand`);
- `metering` — the transparent per-call recorder that emits one `sdk_call`
  event per top-level `meiva.*` call.

`meiva/__init__.py` re-exports the framework entry points (`meiva.help`, ...). The
agent kernel drives rendering by passing `help()` its render parameters, and reads
the installed surface through `installation.installed()` / `install.expansions()` and the
`sdk_disable` entries. None of it is agent-facing: `help(meiva)` lists only the
`__all_for_meiva__` whitelist, and `MEIVA_SDK_DISABLE` refuses to disable a
framework module such as this package.
"""

import sys as _sys
from typing import Any, cast


def meiva_module() -> Any:
    """The `meiva` package module itself — reached through `sys.modules` so
    these private modules never create an import cycle during `import meiva`."""
    return cast(Any, _sys.modules["meiva"])
