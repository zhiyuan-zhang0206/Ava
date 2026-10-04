"""The settings-read rule: a sliced package reads no process-global `settings`.

A package that owns configuration slices (`services/entrypoints/im_bridge/config.py`) receives
them through constructors; only its composition root reads `settings` and builds
them. Governed packages and their roots are the closed `SLICED_PACKAGES` list in
`allowlist.py`. In any other non-test module of a governed package, importing
`settings`, `get_field`, `set_field` or `ensure_eager` from
`base.config` (at module level or inside a function), or importing `base.config`
itself, is a site frozen like any other ambient-state site as
`path::settings-read:<name>`. A package whose slicing lands in several steps may
freeze what is left of its reads; a finished one has none.
"""

from __future__ import annotations

import ast

from scripts.structure.ambient_state import allowlist as allow
from scripts.structure.ambient_state.scan import Hit

SETTINGS_READ = "settings-read"
FIX = (
    "this package receives its configuration as constructor arguments (its slices in "
    "`config.py`); only the composition root named in SLICED_PACKAGES reads `settings` and builds them"
)
_READERS = frozenset({"settings", "get_field", "set_field", "ensure_eager"})
_CONFIG = "base.config"


def package_of(rel: str) -> str | None:
    """The governed package directory holding this module, if any."""
    return next((pkg for pkg in allow.SLICED_PACKAGES if rel.startswith(f"{pkg}/")), None)


def _is_config(module: str | None) -> bool:
    return module is not None and (module == _CONFIG or module.startswith(f"{_CONFIG}."))


def _imported_readers(node: ast.Import | ast.ImportFrom) -> list[str]:
    """The names of the global configuration one import statement reaches."""
    if isinstance(node, ast.ImportFrom) and node.level == 0:
        if _is_config(node.module):
            return [a.name for a in node.names if a.name in _READERS]
        if node.module == "base":
            return [_CONFIG for a in node.names if a.name == "config"]
    elif isinstance(node, ast.Import):
        return [a.name for a in node.names if _is_config(a.name)]
    return []


def hits(tree: ast.Module, rel: str) -> list[Hit]:
    """Every read of the global configuration in a governed, non-root module."""
    package = package_of(rel)
    if package is None or rel in allow.SLICED_PACKAGES[package]:
        return []
    return [
        Hit(SETTINGS_READ, name, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for name in _imported_readers(node)
    ]
