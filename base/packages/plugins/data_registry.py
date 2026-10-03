"""The registry of the data surfaces a plugin declares: metrics and inspector widgets.

These are read by processes that load no agent runtime face — the gateway (per-agent inspector
panels) and the Grafana dashboard supply — so they live in the plugin's own `metrics.py` /
`inspector.py`, each exporting `contribute()` (a pure function returning a `PluginContributions`
with only its field filled). A process builds its registry from whichever of those modules it loads:

- `load_declaration(plugin, importer, gate)` runs one face fail-soft: an import that raises, a
  missing `contribute()`, a result that is not a `PluginContributions`, or an `ava-plugin.json` in the
  plugin directory that disagrees with the declaration on the face's own key is reported
  (`load_report`) and the plugin contributes nothing from that face. The manifest is checked here, while
  the plugin's tree exists (an unpacked installed plugin is deleted right after its import);
- `build_data_registry(declared)` validates each declaration and admits it atomically — a spec that
  fails its query validation, a metric name another plugin already holds, or a widget id repeated within
  its plugin refuses the whole plugin and leaves the others untouched. Admission fills every spec's
  `plugin` with the registry entry's name, so a spec cannot claim another plugin.

Nothing is registered anywhere, so a failed face leaves no partial state to clean up and a later call
sees the fixed file.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from base.packages.plugins import load_report
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions
from base.packages.plugins.gate import check_manifest
from base.packages.plugins.inspector import DuplicateInspectWidget
from base.telemetry.metrics.plugin_metrics import DuplicateMetric, validate_spec_sql

# The manifest key each data face owns (key == attribution surface id): a face is gated only on its
# own key, since the other face of the same plugin is a separate load.
METRICS_KEY = "metrics"
WIDGETS_KEY = "inspectWidgets"


@dataclass(frozen=True)
class DeclaredFace:
    """One plugin's declaration from one data face, ready for admission."""

    plugin: str
    contributions: PluginContributions


def declaration_of(module: ModuleType) -> PluginContributions:
    """`module.contribute()`, refused unless it is a `PluginContributions`."""
    contribute = getattr(module, "contribute", None)
    if contribute is None:
        raise AttributeError(f"{module.__name__} has no contribute()")
    contributions = contribute()
    if not isinstance(contributions, PluginContributions):
        raise TypeError(
            f"{module.__name__}.contribute() returned {type(contributions).__name__}, "
            "not PluginContributions"
        )
    return contributions


def load_declaration(
    plugin: str,
    importer: Callable[[], ModuleType],
    gate: tuple[Path, str] | None = None,
) -> DeclaredFace | None:
    """Import one data face and take its declaration; None (reported) when that fails.

    `importer` does the import (by dotted name, or by file for an unpacked tree) so the caller owns
    where the module comes from and what it caches. `gate` is `(plugin directory, manifest key)` to
    check the plugin's `ava-plugin.json` against the declaration, or None to skip the check. A
    failure is the plugin's: reported through `load_report`, never raised.
    """
    try:
        contributions = declaration_of(importer())
        if gate is not None:
            check_manifest(plugin, gate[0], contributions, (gate[1],))
        return DeclaredFace(plugin, contributions)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        load_report.report_plugin_load_failure(plugin, exc)
        return None


def _admitted(face: DeclaredFace, taken_metrics: dict[str, str]) -> PluginContributions:
    """The face's declaration, validated, with every spec's `plugin` filled. Raises on refusal."""
    plugin, contributions = face.plugin, face.contributions
    names: set[str] = set()
    for spec in contributions.metrics:
        holder = taken_metrics.get(spec.name)
        if spec.name in names or holder is not None:
            raise DuplicateMetric(
                f"metric {spec.name!r} already declared (by plugin {holder or plugin!r}) — "
                f"names are global, prefix with the plugin name."
            )
        names.add(spec.name)
        validate_spec_sql(spec)
    seen: set[str] = set()
    for widget in contributions.inspect_widgets:
        if widget.id in seen:
            raise DuplicateInspectWidget(
                f"widget {widget.id!r} declared twice by plugin {plugin!r}"
            )
        seen.add(widget.id)
    return dataclasses.replace(
        contributions,
        metrics=tuple(spec.model_copy(update={"plugin": plugin}) for spec in contributions.metrics),
        inspect_widgets=tuple(
            widget.model_copy(update={"plugin": plugin}) for widget in contributions.inspect_widgets
        ),
    )


def build_data_registry(
    declared: Iterable[DeclaredFace | None],
) -> tuple[ExtensionRegistry, list[str]]:
    """(registry of the admitted declarations, names of the plugins refused), in input order.

    A refusal is reported through `load_report` and costs only that plugin; metric names are unique
    across the whole registry, first claimant wins.
    """
    admitted: list[tuple[str, PluginContributions]] = []
    refused: list[str] = []
    taken: dict[str, str] = {}
    for face in declared:
        if face is None:
            continue
        try:
            contributions = _admitted(face, taken)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            load_report.report_plugin_load_failure(face.plugin, exc)
            refused.append(face.plugin)
            continue
        for spec in contributions.metrics:
            taken[spec.name] = face.plugin
        admitted.append((face.plugin, contributions))
    return ExtensionRegistry(tuple(admitted)), refused
