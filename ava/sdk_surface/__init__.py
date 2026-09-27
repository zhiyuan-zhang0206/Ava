"""Implementation of the `ava` SDK entry surface — what the agent sees and how.

Each module holds one cohesive slice of the entry-point machinery:

- `const` — the `ava.const()` documented-value factory;
- `sdk_disable` — the `AVA_SDK_DISABLE` machinery (parse + sentinel swap);
- `discovery` — kind predicates, `_Constant`, and the module/class child
  walkers behind `agent_visible_names` (the single source of truth shared by
  help rendering, SDK-expand discovery, doc linting, and metering);
- `help` — the `ava.help()` renderer (stub-format docs for SDK targets);
- `plugins` — the plugin registration API (`register_namespace` family,
  registries, exception hierarchy);
- `wraps` — the wrap registration primitive behind `ava.extend.wrap`
  (`ava/__init__.py` builds the curated `ava.extend` surface from it);
- `plugin_loader` — the plugin-by-path loader (`load_plugin_module`,
  `safe_load_plugin_module`, `scan_and_load`) the agent kernel drives at host
  boot and graph build.

`ava/__init__.py` re-exports the plugin-author entry points (`ava.help`,
`ava.register_namespace`, ...). The agent kernel drives rendering through the
public controls here (`discovery.hidden_surface_members`,
`help.compact_classes`, `plugins.REGISTERED_SDK_EXPANSIONS`, the
`sdk_disable` entries). None of it is agent-facing: `help(ava)` lists only the
`__all_for_ava__` whitelist, and `AVA_SDK_DISABLE` refuses to disable a
framework module such as this package.
"""

import sys as _sys
from typing import Any, cast


def ava_module() -> Any:
    """The `ava` package module itself — reached through `sys.modules` so
    these private modules never create an import cycle during `import ava`."""
    return cast(Any, _sys.modules["ava"])
