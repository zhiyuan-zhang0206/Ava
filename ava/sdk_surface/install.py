"""The one writer of the `ava` module: install a registry's SDK surface, undo it, nothing else.

A plugin declares its SDK surface in `plugin.py`'s `contribute()` (`PluginContributions.sdk_namespaces`,
`sdk_members`, `sdk_expansions`, `sdk_wraps`, `skill_sources`, `config`, `flags`) and never touches `ava`.
`install(registry)` applies every plugin's declaration, in registry order (plugin name order), through
the primitives in `plugins.py` / `wraps.py` / `skill_sources.py` / `config_registration.py` /
`flags.py`; each returns the undo that reverses it. Why this exists at all: agent code reaches the SDK as
attribute access on the singleton `ava` module, so the module itself is the one thing that cannot be
passed around as a value — the write to it is concentrated here, once per process.

`install` produces one **`Installation`**: the admitted registry, the expansions, the wrap layers, the
skill providers, the metering ledger, the applied SDK-disable entries, the faces flag, and the undos —
frozen, so nothing outside it is written after plugin load. The holder is a single slot on the `ava`
module (`__plugin_installation__`); a change (an additive SDK-disable entry, a scoped skill root, the
agent-runtime faces loading) builds a new value and swaps the holder. `uninstall` reverses the surface
and empties the slot.

Before any plugin applies, the env's `AVA_SDK_DISABLE` entries are applied (so a disabled plugin
namespace is refused, exactly as it was when the env was applied at `import ava`); later additions
(per-agent config overlay, the eval-isolation boundary) apply additively on the installed value
(`sdk_disable.apply_sdk_disable`). After the last plugin the SDK-usage recorder is installed over the
final surface, so it sits outermost of any wrap layer (one count per agent call); `uninstall` removes
it first for the same reason.

Per plugin the order is: namespaces, members (a member may hang on the plugin's own namespace),
expansions, wraps (a wrap target may be a namespace or member just added, or another plugin's),
skill sources, flags, config. Explicit declaration refusals (`RegisterNamespaceError`,
`WrapTargetError`, `PluginFlagError`, `SchemaDriftError`, `InvalidConfigData`) are rolled back whole,
reported, and left out of the returned registry only if rollback succeeds. Other errors abort the
entire installation and propagate unchanged after every undo is attempted. Cleanup failures are
fatal themselves when there is no primary error; otherwise they are notes on the primary error.

`install` refuses to run twice: the one installation must be `uninstall`ed before the next, which is
what a reload is (a new registry, a new install). Nothing here triggers a reload at runtime; the host
loads once per process and a changed plugin set takes effect on the next host start.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from base.packages.plugins import load_report
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions

from . import ava_module, wraps
from . import plugins as _plugins
from .wraps import WrapLayer

_SkillProvider = Callable[[], list[Path]]


@dataclass(frozen=True)
class Installation:
    """What the installed registry put into the process, and how to take it back.

    Frozen: a change builds a new value and swaps the holder (`_SLOT`) — see the
    module docstring. `wrap_layers` and the provider tuple are snapshots taken when
    the installation was built; the undos still close over the builders that filled
    them (they are only ever run once, on the way out)."""

    registry: ExtensionRegistry
    expansions: tuple[str, ...]
    wrap_layers: Mapping[str, tuple[WrapLayer, ...]]
    skill_providers: tuple[_SkillProvider, ...]
    metered: tuple[tuple[Any, str], ...]
    disabled: frozenset[str]
    faces: bool
    undo: tuple[Callable[[], None], ...]


# The installation is recorded on the `ava` module object itself — the one process-wide thing it
# describes — rather than in a module global of its own. A loading value also retains an
# existing surface during a faces upgrade; a failed value retains the original error.
_SLOT = "__plugin_installation__"


@dataclass(frozen=True)
class _Loading:
    """An in-flight load, or its failure; neither is a successful installation."""

    installation: Installation | None = None
    failure: BaseException | None = None


_LOADING = _Loading()


def _slot() -> Any:
    return getattr(ava_module(), _SLOT, None)


def installed() -> Installation | None:
    """The current installation, or None before `install` / after `uninstall`."""
    value = _slot()
    if isinstance(value, _Loading):
        return value.installation
    return value if isinstance(value, Installation) else None


def load_attempted() -> bool:
    """Whether a load is in flight or was attempted and failed in this process (no retry)."""
    return isinstance(_slot(), _Loading)


def mark_load_attempt() -> None:
    """Guard a load or faces upgrade without discarding the already-installed surface."""
    if not load_attempted():
        setattr(ava_module(), _SLOT, _Loading(installed()))


def mark_load_failed(exc: BaseException) -> None:
    """Keep the original failed load in the same slot; later accesses must not retry it."""
    setattr(ava_module(), _SLOT, _Loading(installed(), exc))


def raise_load_failure() -> None:
    """Propagate a prior load failure instead of answering as if the surface were ready."""
    value = _slot()
    if isinstance(value, _Loading) and value.failure is not None:
        raise value.failure


def clear_load_attempt() -> None:
    """Release the slot after a deferred load (the loader module was still importing)."""
    value = _slot()
    if isinstance(value, _Loading):
        setattr(ava_module(), _SLOT, value.installation)


def mark_faces_loaded() -> None:
    """Record that the registry's agent-runtime faces loaded: build a new value, swap the holder."""
    current = installed()
    if current is None or current.faces:
        return
    setattr(ava_module(), _SLOT, replace(current, faces=True))


def record_disabled(disabled: frozenset[str]) -> None:
    """Record newly applied SDK-disable entries: build a new value, swap the holder."""
    current = installed()
    if current is None:
        raise RuntimeError("no SDK installation to record disable entries on")
    setattr(ava_module(), _SLOT, replace(current, disabled=disabled))


def expansions() -> tuple[str, ...]:
    """Dotted `ava` paths the installed plugins promote into the system prompt's expanded SDK
    reference, in registry order (rendered ahead of the configured framework list)."""
    current = installed()
    return () if current is None else current.expansions


def _provider_remover(
    providers: list[_SkillProvider], provider: _SkillProvider
) -> Callable[[], None]:
    def undo() -> None:
        providers.remove(provider)

    return undo


def _provider_mount(provider: _SkillProvider) -> Installation:
    """The minimal installation a scoped skill root carries when nothing is installed.

    The ops runner scopes a project skill root around one request without running a
    plugin load (its process has no installation of its own); the value still needs a
    home because there is one holder for the process's SDK configuration."""
    return Installation(
        registry=ExtensionRegistry(),
        expansions=(),
        wrap_layers=MappingProxyType({}),
        skill_providers=(provider,),
        metered=(),
        disabled=frozenset(),
        faces=False,
        undo=(),
    )


def mount_skill_provider(provider: _SkillProvider) -> Callable[[], None]:
    """Extend this process's skill-root providers by one; returns the undo.

    For a framework caller that needs a request-scoped skill root (the ops runner's
    per-agent command view): build a new installation value with the provider appended
    and swap the holder; the undo removes exactly that provider and puts the holder
    back. With no installation at all, a minimal provider-only value is mounted.
    """
    prior = _slot()
    mounted = (
        replace(prior, skill_providers=(*prior.skill_providers, provider))
        if isinstance(prior, Installation)
        else _provider_mount(provider)
    )
    setattr(ava_module(), _SLOT, mounted)

    def undo() -> None:
        current = _slot()
        if current is mounted:
            setattr(ava_module(), _SLOT, prior)
            return
        # An interleaved mount/unmount changed the value under us: remove exactly this
        # provider from whatever is current, and clear the holder if nothing is left.
        if isinstance(current, Installation):
            remaining = tuple(p for p in current.skill_providers if p is not provider)
            if not remaining and _is_bare_provider_mount(current):
                setattr(ava_module(), _SLOT, None)
            else:
                setattr(ava_module(), _SLOT, replace(current, skill_providers=remaining))

    return undo


def _is_bare_provider_mount(installation: Installation) -> bool:
    """Whether the value carries nothing but skill providers (a scoped mount's shell)."""
    return (
        not installation.registry.plugins
        and not installation.expansions
        and not installation.wrap_layers
        and not installation.metered
        and not installation.disabled
        and not installation.faces
        and not installation.undo
    )


def _apply(
    plugin: str,
    contributions: PluginContributions,
    build: _Build,
    namespaces: dict[str, str],
) -> list[str]:
    """Apply one plugin's SDK declaration into `build`; each applied piece appends its undo.
    Returns the plugin's expansion paths. Raises on the first piece that cannot be applied."""
    from base.packages.plugins import config_registration, flags

    promoted: list[str] = []
    for ns in contributions.sdk_namespaces:
        build.undo.append(_plugins.install_namespace(plugin, ns.name, ns.module, namespaces))
        namespaces[ns.name] = plugin
        if ns.expand:
            _plugins.check_expansion(ns.name)
            promoted.append(ns.name)
    for member in contributions.sdk_members:
        build.undo.append(_plugins.install_member(plugin, member.namespace, member.name, member.fn))
    for path in contributions.sdk_expansions:
        _plugins.check_expansion(path)
        promoted.append(path)
    for wrap in contributions.sdk_wraps:
        build.undo.append(wraps.apply_wrap(wrap.target, wrap.wrapper, plugin, build.layers))
    for provider in contributions.skill_sources:
        build.providers.append(provider)
        build.undo.append(_provider_remover(build.providers, provider))
    if contributions.flags:
        build.undo.append(flags.declare_flags(plugin, contributions.flags))
    if contributions.config is not None:
        build.undo.append(config_registration.bind_plugin_config(plugin, contributions.config))
    return promoted


@dataclass
class _Build:
    """The mutable ledger `install` fills while it applies plugins; frozen into the Installation."""

    namespaces: dict[str, str] = field(default_factory=dict)
    layers: dict[str, list[WrapLayer]] = field(default_factory=dict)
    providers: list[_SkillProvider] = field(default_factory=list)
    expansions: list[str] = field(default_factory=list)
    undo: list[Callable[[], None]] = field(default_factory=list)


def _run(undo: list[Callable[[], None]], primary: BaseException | None = None) -> None:
    """Attempt every undo; preserve a primary failure or raise the first cleanup failure."""
    failure = primary
    while undo:
        step = undo.pop()
        try:
            step()
        except BaseException as exc:
            if failure is None:
                failure = exc
            else:
                failure.add_note(f"SDK rollback also failed: {type(exc).__name__}: {exc}")
    if primary is None and failure is not None:
        raise failure


def install(
    registry: ExtensionRegistry, report: load_report.Reporter | None = None
) -> ExtensionRegistry:
    """Install `registry`'s SDK surface into `ava`; return the registry of the plugins admitted.

    Raises:
        RuntimeError: a previous installation is still in place (`uninstall()` first).
        BaseException: an unexpected application or cleanup failure; already-applied
            pieces are undone before the original error propagates.
    """
    if installed() is not None:
        raise RuntimeError(
            "the SDK surface is already installed; uninstall() it before installing another registry"
        )
    from base.packages.plugins import config_registration, flags

    from . import metering, sdk_disable

    prior = _slot()
    setattr(ava_module(), _SLOT, _LOADING)
    build = _Build()
    admitted: list[tuple[str, PluginContributions]] = []
    disabled = sdk_disable.env_entries()
    try:
        if disabled:
            # Apply the env entries before any plugin: a disabled plugin namespace is
            # refused by install_namespace (same as when the env was applied at import).
            sdk_disable.apply_entries(disabled)
        for plugin, contributions in registry.plugins:
            mark = len(build.undo)
            claimed = dict(build.namespaces)
            try:
                paths = _apply(plugin, contributions, build, claimed)
            except (
                _plugins.RegisterNamespaceError,
                wraps.WrapTargetError,
                flags.PluginFlagError,
                config_registration.SchemaDriftError,
                config_registration.InvalidConfigData,
            ) as exc:
                partial = build.undo[mark:]
                del build.undo[mark:]
                _run(partial)
                load_report.reporter(report)(plugin, exc)
                continue
            build.namespaces = claimed
            build.expansions.extend(paths)
            admitted.append((plugin, contributions))
        metered = metering.install()
    except BaseException as exc:
        _run(build.undo, exc)
        setattr(ava_module(), _SLOT, prior)
        raise
    installation = Installation(
        registry=ExtensionRegistry(tuple(admitted)),
        expansions=tuple(build.expansions),
        wrap_layers=MappingProxyType(
            {target: tuple(layers) for target, layers in build.layers.items()}
        ),
        skill_providers=tuple(build.providers),
        metered=metered,
        disabled=frozenset(disabled),
        faces=False,
        undo=tuple(build.undo),
    )
    setattr(ava_module(), _SLOT, installation)
    return installation.registry


def uninstall() -> None:
    """Take the installed SDK surface back out of the process (a no-op when none is installed)."""
    from . import metering

    installation = installed()
    # The recorder sits outermost over plugin wraps, so it comes off first.
    metering.uninstall(() if installation is None else installation.metered)
    if installation is None:
        # A `_LOADING` marker is not an installation: leave it in place (a load may be
        # in flight; a failed attempt stays un-retried), a fresh install overwrites it.
        return
    setattr(ava_module(), _SLOT, None)
    _run(list(installation.undo))
