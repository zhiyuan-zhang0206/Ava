"""Load an `ava_builtins/skills/` script module by file path, for tests.

`ava_builtins/skills/<group>/<skill>/` directory names are kebab-case, so they are
never importable Python packages — and Structure Rule 6
(`scripts/structure/path_imports.py`) forbids loading one by file path from
*inside* `ava_builtins/` too (a skill script imports a sibling only through
the narrow `__file__`-derived `sys.path` guard, never a loader). Tests are
outside that scope (pytest manages their own sys.path), so a test loads the
script under test directly from its file path with the standard
`importlib.util.spec_from_file_location` recipe — `load_skill_script` below
just centralizes the handful of lines every such test previously repeated
inline. Lives in this package's `__init__.py` (not its own module) so that
adding it did not grow `tests/skills/`'s direct-entry count past the
structure budget.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[2]


def load_skill_script(*parts: str, name: str | None = None) -> ModuleType:
    """Load `ava_builtins/skills/<parts...>` as a module, executed and
    registered in `sys.modules` under `name` (default: `"<stem>_under_test"`)
    so `@dataclass` and similar can resolve the module by name.

    Example: `load_skill_script("integrations", "gmail", "scripts", "imap.py")` loads
    `ava_builtins/skills/integrations/gmail/scripts/imap.py`.
    """
    path = _REPO_ROOT.joinpath("ava_builtins", "skills", *parts)
    module_name = name or f"{path.stem}_under_test"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
