"""Grafana dashboard spec suppliers — the plugin side of the render input.

Task #3697 slice S1 (parent #3689). The dashboard renderer
(``base.telemetry.metrics.grafana_dashboard``) is a pure function of its spec lists; this
module collects the plugin specs from the two content sources (decision A2):

- **repo** — the checkout's ``ava_builtins/plugins/*/metrics.py``, imported
  and their ``contribute()`` declarations admitted into a data registry (the
  inspector's loader pattern, task #180);
- **installed** — the enabled ``kind='plugin'`` rows of the extension
  registry, unpacked from their blobs and imported; panel presence follows
  "installed and enabled", not local runnability.

A module that fails to import or declare is skipped loudly
(``plugin_load_report``) and the remaining plugins still render — never a
half-written dashboard; a declaration is admitted whole or not at all. Suppliers are impure by design (module imports,
filesystem, database); the renderer stays clean of all three.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import tempfile
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import psycopg

from base.db import Database
from base.log import logger
from base.packages.extensions import registry
from base.packages.plugins import data_registry, load_report
from base.telemetry.metrics.plugin_metrics import MetricSpec

# ── plugin spec suppliers ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class PluginSpecs:
    """One supplier pass: the collected specs, the plugins that loaded, and the
    plugins that failed to load (already reported and dropped)."""

    specs: list[MetricSpec]
    loaded: list[str]
    failed: list[str]


_REPO_PLUGINS_DIR = Path(__file__).resolve().parents[3] / "ava_builtins" / "plugins"


def _specs_of(faces: list[data_registry.DeclaredFace | None], names: list[str]) -> PluginSpecs:
    """Admit the declarations (`names` are the plugins they came from, in order, `None` = a face
    that failed to load) into one data registry and report the outcome."""
    registry, refused = data_registry.build_data_registry(faces)
    failed = [name for name, face in zip(names, faces, strict=True) if face is None]
    refused_set = set(refused)
    loaded = [
        name
        for name, face in zip(names, faces, strict=True)
        if face is not None and name not in refused_set
    ]
    return PluginSpecs(specs=list(registry.metrics()), loaded=loaded, failed=[*failed, *refused])


def _repo_faces(
    plugins_dir: Path,
) -> tuple[list[data_registry.DeclaredFace | None], list[str]]:
    """Every ``metrics.py`` declaration under `plugins_dir`, fail-soft, with the plugin names."""
    faces: list[data_registry.DeclaredFace | None] = []
    names: list[str] = []
    for metrics_py in sorted(plugins_dir.glob("*/metrics.py")):
        name = metrics_py.parent.name
        names.append(name)
        faces.append(
            data_registry.load_declaration(
                name,
                lambda name=name, path=metrics_py: _import_plugin_metrics(
                    f"ava_repo_plugins.{name}.metrics", path
                ),
                (metrics_py.parent, data_registry.METRICS_KEY),
            )
        )
    return faces, names


def load_repo_plugin_specs(plugins_dir: Path = _REPO_PLUGINS_DIR) -> PluginSpecs:
    """Load every shipped plugin's ``metrics.py`` declaration (`plugins_dir`: the checkout's plugin root).

    Mirrors the inspector's loader (task #180 PR D): import, fail soft with a
    loud report, and keep going — the module cache makes repeated calls free.
    """
    return _specs_of(*_repo_faces(plugins_dir))


def _import_plugin_metrics(module_name: str, metrics_py: Path) -> ModuleType:
    """Import one plugin's ``metrics.py`` from its file location.

    The module is registered under its synthetic name before execution (the
    standard importlib recipe) so repeated supplier passes reuse the cached
    module; a module that fails leaves neither a ``sys.modules`` entry nor
    partial registrations behind. File-location import (rather than
    ``import_module``) keeps the loader independent of the plugins root being
    an importable package path — the installed trees are unpacked scratch
    directories, and the checkout's root is just another directory to this
    loader.
    """
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, metrics_py)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {metrics_py}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _installed_faces(
    conn: psycopg.Connection, skip_names: Iterable[str]
) -> tuple[list[data_registry.DeclaredFace | None], list[str], list[str]]:
    """(declarations, plugin names, plugins that could not even be read) of the enabled installed
    plugins. The declaration is taken while the unpacked tree exists, so the manifest gate and the
    import both see it."""
    faces: list[data_registry.DeclaredFace | None] = []
    names: list[str] = []
    unreadable: list[str] = []
    already = set(skip_names)
    for extension in registry.list_enabled(conn, kind="plugin"):
        if extension.is_repo_source or extension.name in already:
            continue
        if extension.content_hash is None:  # pragma: no cover — schema-forbidden
            logger.error("plugin {name} has no content hash — skipped", name=extension.name)
            unreadable.append(extension.name)
            continue
        archive = registry.get_blob(conn, extension.content_hash)
        if archive is None:  # pragma: no cover — schema-forbidden
            logger.error(
                "plugin {name} points at content_hash {digest} with no blob — skipped",
                name=extension.name,
                digest=extension.content_hash,
            )
            unreadable.append(extension.name)
            continue
        try:
            with _unpacked_plugin(extension.name, archive) as tree:
                metrics_py = tree / "metrics.py"
                if not metrics_py.is_file():
                    logger.error(
                        "plugin {name} ships no metrics.py — no dashboard panels",
                        name=extension.name,
                    )
                    continue
                names.append(extension.name)
                faces.append(
                    data_registry.load_declaration(
                        extension.name,
                        lambda name=extension.name, path=metrics_py: _import_plugin_metrics(
                            f"ava_installed_plugins.{name}.metrics", path
                        ),
                        (tree, data_registry.METRICS_KEY),
                    )
                )
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            load_report.report_plugin_load_failure(extension.name, exc)
            unreadable.append(extension.name)
    return faces, names, unreadable


def load_installed_plugin_specs(
    conn: psycopg.Connection, *, skip_names: Iterable[str] = ()
) -> PluginSpecs:
    """Load the enabled installed plugins from the registry (kind='plugin').

    Row presence and ``default_enabled`` decide what ships panels — the
    content comes from the blob, unpacked into a scratch directory for the
    import; repo-source rows carry no blob and are the repo supplier's job.
    ``skip_names`` holds plugins the caller already loaded (e.g. from the
    checkout) — they are not imported twice. A row whose blob is missing, or
    whose ``metrics.py`` fails to import or declare, is reported and skipped;
    the rest still load.
    """
    faces, names, unreadable = _installed_faces(conn, skip_names)
    specs = _specs_of(faces, names)
    return PluginSpecs(specs=specs.specs, loaded=specs.loaded, failed=[*unreadable, *specs.failed])


@contextmanager
def _unpacked_plugin(name: str, archive: bytes) -> Generator[Path]:
    """Unpack a plugin blob into a scratch tree; yields the tree root. Owns
    (and removes) the scratch tree it creates."""
    with tempfile.TemporaryDirectory(prefix=f"ava-plugin-{name}-") as scratch:
        tree = Path(scratch)
        registry.unpack_tree(archive, tree)
        yield tree


def collect_plugin_specs(
    conn: psycopg.Connection | None = None, plugins_dir: Path = _REPO_PLUGINS_DIR
) -> PluginSpecs:
    """The dual supplier: repo plugins (checkout) plus enabled installed
    plugins (registry rows + blobs), deduplicated by plugin name — a name
    already loaded from the checkout is not loaded again from the registry.
    Both sources are admitted into one data registry, so a metric name two
    plugins claim is refused for the second."""
    faces, names = _repo_faces(plugins_dir)
    unreadable: list[str] = []
    if conn is not None:
        installed_faces, installed_names, unreadable = _installed_faces(conn, names)
        faces.extend(installed_faces)
        names.extend(installed_names)
    merged = _specs_of(faces, names)
    return PluginSpecs(
        specs=merged.specs, loaded=merged.loaded, failed=[*unreadable, *merged.failed]
    )


def render_dashboard_json(db: Database, *, repo_only: bool = False) -> tuple[str, tuple[str, ...]]:
    """Render the complete ava-ops dashboard JSON from the live spec suppliers.

    The one render path shared by the operator command (``ava lgtm render``)
    and the converge provisioning step (task #3697 S3). ``repo_only`` skips
    the installed-plugin registry read; otherwise the enabled installed
    plugins load from the cluster database. A plugin that fails to load is
    skipped and reported by its supplier — never a half-written render.

    Returns:
        ``(dashboard_json, failed_plugins)`` — the deterministic serialization
        plus the sorted names of plugins whose metrics module failed to load.
    """
    from base.telemetry.metrics.core import catalog
    from base.telemetry.metrics.grafana_dashboard import render_dashboard, render_to_json

    core_specs = catalog.collect_core_metrics()
    if repo_only:
        plugins = collect_plugin_specs()
    else:
        with db.connect() as conn:
            plugins = collect_plugin_specs(conn)
    rendered = render_to_json(render_dashboard(core_specs, plugins.specs))
    return rendered, tuple(sorted(plugins.failed))
