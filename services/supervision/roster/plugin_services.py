"""Plugin-registered ops services — discovery of the ``services.py`` faces present on this machine.

Plugins expose ``services() -> tuple[ServiceSpec, ...]`` from their services module;
``plugin_services()`` discovers the code-present plugins and returns what they declare, and
``services.supervision.roster.build_services()`` appends it to the core roster: the plugin declares, ops discovers.
Each plugin service's own ``ServiceSpec.gate`` keeps cluster-level enablement out of
``services.supervision.roster.spec.gate_reason``. The fleet task daemon follows this path; see
``docs/decisions/extensions/plugins/2026-07-19-plugin-registered-services.md``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import cast

from services.supervision.roster.service_spec import ServiceSpec


def plugin_services() -> tuple[ServiceSpec, ...]:
    """The services contributed by the plugins PRESENT on this machine.

    Discovery, not import-of-known-plugins: `base.packages.plugins.enable_config` enumerates the
    plugins installed on THIS machine (builtin + external), and each that ships a
    ``services.py`` exposing ``services() -> tuple[ServiceSpec, ...]`` gets folded
    into the roster. This keeps the direction "plugin declares, ops discovers" — no
    reverse edge from ops into any specific plugin's domain code.

    Discovery keys on plugin **presence**, NOT the agent-facing enable-state
    (``ava plugins enable/disable``): the roster is a machine/cluster concern, and
    coupling it to the agent-plugin-registration plane would be cross-plane
    semantics. A plugin gates its own service (whether it starts) via an explicit
    settings field in ``ServiceSpec.gate`` — e.g. task-maintenance's
    Fleet ``task_maintenance_enabled`` — which is deterministic at daemon-start and
    unaffected by any per-agent config overlay. start / root / status all
    follow, since they derive from `build_services()`.

    The ``services.py`` module is loaded by FILE PATH (like
    `base.packages.plugin_config_admin.update_all_disk_images` loads `default_config.py`) so an
    external plugin under ``~/.ava/plugins/`` — off the ``plugins.`` package path —
    can register too; it must import only light deps (ops / base), never its
    own `plugin.py`, so this load does not drag the agent kernel into the ops
    process.

    A missing services.py is an optional absent face. Once present, import,
    declaration and config errors are reported and propagated to the operation
    owner; an invalid roster is never presented as a successful partial one.
    Session-name collisions also fail: no rule can pick between two owners.
    """
    from base.packages.plugins import load_report
    from base.packages.plugins.enable_config import installed_plugin_dirs

    specs: list[ServiceSpec] = []
    for name, plugin_dir in sorted(installed_plugin_dirs().items()):
        services_py = plugin_dir / "services.py"
        if not services_py.exists():
            continue
        try:
            module = load_plugin_module(name, services_py)
            specs.extend(_declared_plugin_services(name, module))
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            load_report.report_plugin_load_failure(name, exc)
            raise
    return tuple(specs)


def _declared_plugin_services(name: str, module: object) -> tuple[ServiceSpec, ...]:
    """Validate the present services face before accepting any roster entries."""
    declare = getattr(module, "services", None)
    if not callable(declare):
        raise PluginServiceError(f"plugin {name!r} services.py must define callable services()")
    declared = declare()
    if not isinstance(declared, tuple):
        raise PluginServiceError(f"plugin {name!r} services() must return tuple[ServiceSpec, ...]")
    members = cast(tuple[object, ...], declared)
    if not all(isinstance(item, ServiceSpec) for item in members):
        raise PluginServiceError(f"plugin {name!r} services() must return tuple[ServiceSpec, ...]")
    return cast(tuple[ServiceSpec, ...], members)


def load_plugin_module(name: str, services_py: Path) -> object:
    """Load and register ``services.py`` so its own healthcheck is importable.

    An external plugin can keep its protocol health probes here and declare
    ``healthcheck_module=__name__``. Root health monitoring must resolve that
    module even when no agent has bootstrapped the external plugin namespace. Registration
    before execution also gives dataclasses their normal import-time identity.
    Failed imports restore the prior module, never a half-executed replacement.
    """
    spec = importlib.util.spec_from_file_location(f"plugins.{name}.services", services_py)
    if spec is None or spec.loader is None:
        raise PluginServiceError(f"cannot load services.py for plugin {name!r} ({services_py})")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if previous is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous
        raise
    return module


def assert_unique_sessions(core: tuple[ServiceSpec, ...], plugin: tuple[ServiceSpec, ...]) -> None:
    """Fail fast if a plugin service's session name collides with a core service or
    another plugin's — the roster is keyed on `session` (session, root
    roster, status), so a duplicate would silently shadow one entry."""
    seen = {s.session for s in core}
    for s in plugin:
        if s.session in seen:
            raise PluginServiceError(
                f"plugin service session {s.session!r} collides with an existing service; "
                "session names must be unique across core + all plugins"
            )
        seen.add(s.session)


class PluginServiceError(RuntimeError):
    """A plugin's service declaration is malformed or collides with another."""
