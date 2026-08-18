"""Private implementation modules behind the `ava` SDK entry surface.

Each module holds one cohesive slice of the entry-point machinery that used
to live in `ava/__init__.py`:

- `const` — the `ava.const()` documented-value factory;
- `sdk_disable` — the `AVA_SDK_DISABLE` machinery (parse + sentinel swap);
- `discovery` — kind predicates, `_Constant`, and the module/class child
  walkers behind `agent_visible_names` (the single source of truth shared by
  help rendering, SDK-expand discovery, doc linting, and metering);
- `help` — the `ava.help()` renderer (stub-format docs for SDK targets);
- `plugins` — the plugin registration API (`register_namespace` family,
  registries, exception hierarchy).

`ava/__init__.py` re-exports their public names with redundant aliases, so the
external API (`import ava.X`, `ava.help`, `ava.register_namespace`, ...) is
byte-identical to before the split. Nothing here is part of the agent-facing
SDK: the whole package carries the underscore prefix, so `help(ava)` never
lists it and `AVA_SDK_DISABLE` cannot target it.
"""

import sys as _sys
from typing import Any, cast


def ava_module() -> Any:
    """The `ava` package module itself — reached through `sys.modules` so
    these private modules never create an import cycle during `import ava`."""
    return cast(Any, _sys.modules["ava"])
