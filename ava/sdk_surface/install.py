"""The one writer of the `ava` module: install a registry's SDK surface, undo it, nothing else.

A plugin declares its SDK surface in `plugin.py`'s `contribute()` (`PluginContributions.sdk_namespaces`,
`sdk_members`, `sdk_expansions`, `sdk_wraps`, `skill_sources`, `config`, `flags`) and never touches `ava`.
`install(registry)` applies every plugin's declaration, in registry order (plugin name order), through
the primitives in `plugins.py` / `wraps.py` / `skill_sources.py` / `config_registration.py` /
`flags.py`; each returns the undo that reverses it. Why this exists at all: agent code reaches the SDK as
attribute access on the singleton `ava` module, so the module itself is the one thing that cannot be
passed around as a value — the write to it is concentrated here, once per process.

Per plugin the order is: namespaces, members (a member may hang on the plugin's own namespace),
expansions, wraps (a wrap target may be a namespace or member just added, or another plugin's),
skill sources, flags, config. A plugin whose declaration cannot be applied (a conflicting or
disabled namespace name, a wrap target that does not resolve, a flag or config that does not
validate or bind) is **rolled back whole** — its already-applied pieces undone, in reverse — reported
as a plugin load failure, and left out of the registry `install` returns. After the last plugin the
SDK-usage recorder is installed over the final surface, so it sits outermost of any wrap layer (one
count per agent call); `uninstall` removes it first for the same reason.

`install` refuses to run twice: the one installation must be `uninstall`ed before the next, which is
what a reload is (a new registry, a new install). Nothing here triggers a reload at runtime; the host
loads once per process and a changed plugin set takes effect on the next host start.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from base.packages.plugins import load_report
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions

from . import ava_module, skill_sources, wraps
from . import plugins as _plugins


@dataclass
class Installation:
    """What the installed registry put into the process, and how to take it back."""

    registry: ExtensionRegistry
    expansions: tuple[str, ...]
    undo: list[Callable[[], None]] = field(default_factory=list)


# The installation is recorded on the `ava` module object itself — the one process-wide thing it
# describes — rather than in a module global of its own.
_SLOT = "__plugin_installation__"


def installed() -> Installation | None:
    """The current installation, or None before `install` / after `uninstall`."""
    return getattr(ava_module(), _SLOT, None)


def expansions() -> tuple[str, ...]:
    """Dotted `ava` paths the installed plugins promote into the system prompt's expanded SDK
    reference, in registry order (rendered ahead of the configured framework list)."""
    current = installed()
    return () if current is None else current.expansions


def _apply(
    plugin: str,
    contributions: PluginContributions,
    namespaces: dict[str, str],
    undo: list[Callable[[], None]],
) -> list[str]:
    """Apply one plugin's SDK declaration; each applied piece appends its undo. Returns the plugin's
    expansion paths. Raises on the first piece that cannot be applied."""
    from base.packages.plugins import config_registration, flags

    promoted: list[str] = []
    for ns in contributions.sdk_namespaces:
        undo.append(_plugins.install_namespace(plugin, ns.name, ns.module, namespaces))
        namespaces[ns.name] = plugin
        if ns.expand:
            _plugins.check_expansion(ns.name)
            promoted.append(ns.name)
    for member in contributions.sdk_members:
        undo.append(_plugins.install_member(plugin, member.namespace, member.name, member.fn))
    for path in contributions.sdk_expansions:
        _plugins.check_expansion(path)
        promoted.append(path)
    for wrap in contributions.sdk_wraps:
        undo.append(wraps.apply_wrap(wrap.target, wrap.wrapper, plugin))
    for provider in contributions.skill_sources:
        undo.append(skill_sources.add(provider))
    if contributions.flags:
        undo.append(flags.declare_flags(plugin, contributions.flags))
    if contributions.config is not None:
        undo.append(config_registration.bind_plugin_config(plugin, contributions.config))
    return promoted


def _run(undo: list[Callable[[], None]], report: load_report.Reporter | None = None) -> None:
    """Run undos newest first; one failing does not stop the rest."""
    while undo:
        step = undo.pop()
        try:
            step()
        except Exception as exc:
            load_report.reporter(report)("<sdk-surface>", exc)


def install(
    registry: ExtensionRegistry, report: load_report.Reporter | None = None
) -> ExtensionRegistry:
    """Install `registry`'s SDK surface into `ava`; return the registry of the plugins admitted.

    Raises:
        RuntimeError: a previous installation is still in place (`uninstall()` first).
    """
    if installed() is not None:
        raise RuntimeError(
            "the SDK surface is already installed; uninstall() it before installing another registry"
        )
    from . import metering

    undo: list[Callable[[], None]] = []
    admitted: list[tuple[str, PluginContributions]] = []
    promoted: list[str] = []
    namespaces: dict[str, str] = {}
    for plugin, contributions in registry.plugins:
        applied: list[Callable[[], None]] = []
        claimed = dict(namespaces)
        try:
            paths = _apply(plugin, contributions, claimed, applied)
        except Exception as exc:
            _run(applied, report)
            load_report.reporter(report)(plugin, exc)
            continue
        namespaces = claimed
        undo.extend(applied)
        promoted.extend(paths)
        admitted.append((plugin, contributions))
    admitted_registry = ExtensionRegistry(tuple(admitted))
    metering.install()
    setattr(ava_module(), _SLOT, Installation(admitted_registry, tuple(promoted), undo))
    return admitted_registry


def uninstall() -> None:
    """Take the installed SDK surface back out of the process (a no-op when none is installed)."""
    from . import metering

    # The recorder sits outermost over plugin wraps, so it comes off first.
    metering.uninstall()
    installation = installed()
    if installation is None:
        return
    setattr(ava_module(), _SLOT, None)
    _run(installation.undo)
