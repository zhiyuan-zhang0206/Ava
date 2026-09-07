"""`AVA_SDK_DISABLE` — remove pieces of the agent-facing SDK after import.

The env-var parse, the sentinel module, and the idempotent apply machinery
(split out of `ava/__init__.py`) live here; the package entry imports
`_apply_sdk_disable` / `_sdk_disable_entries` and applies the env entries at
its own import-time point (after the submodule imports + `__all_for_ava__`
exist). The disposable exec child applies its agent overlay before
importing the SDK; the shared host does not mutate exports per turn.
"""

import inspect
import os as _os
import sys as _sys
import types as _types
from typing import Any

from . import ava_module

# AVA_SDK_DISABLE removes pieces of the agent-facing SDK after the regular
# imports finish. Each comma-separated entry is either:
#   - a top-level module name (`monitor`, `schedule`, `agents`) — the submodule
#     attribute is deleted from this package and the entry in sys.modules is
#     replaced with a sentinel module that raises a legible error on any
#     attribute access. So `ava.<name>` raises AttributeError (not found on
#     parent package), `import ava.<name>` returns the sentinel (no crash),
#     but `ava.<name>.anything` raises a clear "disabled by AVA_SDK_DISABLE"
#     error. help(ava) does not list disabled modules.
#   - a dotted path (`self.terminate`, `agents.spawn`, `shell.sessions`) — the
#     leaf is resolved at apply time. If it's a nested submodule (e.g.
#     `shell.sessions`) it's disabled as a module: deleted from its parent and
#     its sys.modules entry swapped for the sentinel, so `import ava.a.b` /
#     `ava.a.b.x` raise the same legible "disabled by AVA_SDK_DISABLE" error as
#     a top-level module. If it's a plain attribute / function
#     (`self.terminate`) it's just deleted; `ava.<module>.<attr>` then raises
#     AttributeError. Either way help() stops listing it.
# Used to scope the SDK to a context — e.g. a benchmark runner that owns the
# agent lifecycle disables watcher / self / agents / shell.sessions.
_sdk_disable_raw = _os.environ.get("AVA_SDK_DISABLE", "")
_sdk_disable_entries: list[str] = [e.strip() for e in _sdk_disable_raw.split(",") if e.strip()]

# (the package entry applies the parsed entries after its submodule imports
# and `__all_for_ava__` exist — see `ava/__init__.py`)


class _DisabledSDKModule(_types.ModuleType):
    """Stand-in for a module removed by AVA_SDK_DISABLE.

    Returned by `import ava.<name>` after `<name>` is in the disable list so a
    plugin author trying to use a disabled SDK gets a legible error on first
    attribute access instead of a raw ModuleNotFoundError at import time.
    """

    def __getattr__(self, attr: str) -> Any:
        leaf = self.__name__.split(".", 1)[1]
        raise AttributeError(
            f"{self.__name__}.{attr} — module is disabled by AVA_SDK_DISABLE "
            f"(entry: {leaf!r}). If the caller needs it, remove {leaf!r} from "
            "AVA_SDK_DISABLE."
        )


# Track entries already applied so re-entrant calls are idempotent and
# cumulative — the env parse runs first, then per-agent config_overlay
# additions add new entries on top without re-processing the old ones.
_applied_disable_entries: set[str] = set()


def _apply_sdk_disable(entries: list[str]) -> None:
    """Apply SDK disable entries — idempotent, re-entrant, cumulative.

    Each call computes the delta (entries not yet applied) and processes
    only those. Called at import time from env ``AVA_SDK_DISABLE`` and
    later from per-agent ``config_overlay`` sdk_disable additions.
    """
    new_entries = [e for e in entries if e not in _applied_disable_entries]
    if not new_entries:
        return

    # Top-level modules: delete from this package + swap sys.modules entry
    # with the sentinel so `import ava.<mod>` returns it — its __getattr__
    # raises legibly on first use. Also remove from __all_for_ava__ so
    # help(ava) does not list it.
    for _mod in {e for e in new_entries if "." not in e}:
        _disable_top_level_module(_mod)

    # Dotted entries: resolve the leaf. A nested submodule is disabled as a
    # module (delete from parent + swap its sys.modules entry for the sentinel,
    # so `import ava.a.b` / `ava.a.b.x` raise legibly, same as a top-level
    # module); a plain attribute / function is just deleted (`ava.a.b` then
    # raises AttributeError).
    for _entry in [e for e in new_entries if "." in e]:
        _disable_dotted_entry(_entry)

    _applied_disable_entries.update(new_entries)


def _disable_top_level_module(name: str) -> None:
    """Remove a top-level SDK module: drop the package attribute, swap the
    ``sys.modules`` entry for the sentinel, and hide it from ``help(ava)``."""
    pkg = ava_module()
    if hasattr(pkg, name):
        delattr(pkg, name)
    _sys.modules[f"ava.{name}"] = _DisabledSDKModule(f"ava.{name}")
    surface = getattr(pkg, "__all_for_ava__", None)
    if surface is not None and name in surface:
        surface.remove(name)


def _disable_dotted_entry(entry: str) -> None:
    """Disable one dotted ``AVA_SDK_DISABLE`` entry by resolving its leaf.

    A nested submodule is disabled as a module (delete from parent + swap its
    ``sys.modules`` entry for the sentinel); a plain attribute / function is
    just deleted. Missing parents or leaves are skipped."""
    parts = entry.split(".")
    parent: Any = getattr(ava_module(), parts[0], None)
    for p in parts[1:-1]:
        parent = getattr(parent, p, None)
    if parent is None:
        return
    leaf = parts[-1]
    target = getattr(parent, leaf, None)
    if target is None:
        return
    delattr(parent, leaf)
    if inspect.ismodule(target):
        _sys.modules[f"ava.{entry}"] = _DisabledSDKModule(f"ava.{entry}")
