"""Grafana dashboard spec suppliers — the plugin side of the render input.

Task #3697 slice S1 (parent #3689). The dashboard renderer
(``shared.grafana_dashboard``) is a pure function of its spec lists; this
module collects the plugin specs from the two content sources (decision A2):

- **repo** — the checkout's ``ava_builtins/plugins/*/metrics.py``, imported
  under their plugin contexts (the inspector's loader pattern, task #180);
- **installed** — the enabled ``kind='plugin'`` rows of the extension
  registry, unpacked from their blobs and imported; panel presence follows
  "installed and enabled", not local runnability.

A module that fails to import is skipped loudly (``plugin_load_report`` +
``drop_plugin_metrics``) and the remaining plugins still render — never a
half-written dashboard. Suppliers are impure by design (module imports,
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

import psycopg

from shared import extension_registry, plugin_load_report
from shared.log import logger
from shared.plugin_context import PluginContext
from shared.plugin_metrics import MetricSpec, drop_plugin_metrics, registered_metrics

# ── plugin spec suppliers ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class PluginSpecs:
    """One supplier pass: the collected specs, the plugins that loaded, and the
    plugins that failed to load (already reported and dropped)."""

    specs: list[MetricSpec]
    loaded: list[str]
    failed: list[str]


_REPO_PLUGINS_DIR = Path(__file__).resolve().parents[1] / "ava_builtins" / "plugins"


def _plugin_specs_for(names: Iterable[str]) -> list[MetricSpec]:
    """The currently-registered specs of the given plugins (registration
    order), for the supplier that loaded them."""
    wanted = set(names)
    return [spec for spec in registered_metrics() if spec.plugin in wanted]


def load_repo_plugin_specs() -> PluginSpecs:
    """Load every shipped plugin's ``metrics.py`` under its plugin context.

    Mirrors the inspector's loader (task #180 PR D): import, fail soft with a
    loud report and a partial-registration cleanup, and keep going — the
    module cache makes repeated calls free.
    """
    loaded: list[str] = []
    failed: list[str] = []
    for metrics_py in sorted(_REPO_PLUGINS_DIR.glob("*/metrics.py")):
        name = metrics_py.parent.name
        try:
            with PluginContext(name):
                _import_plugin_metrics(f"ava_repo_plugins.{name}.metrics", metrics_py)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            plugin_load_report.report_plugin_load_failure(name, exc)
            drop_plugin_metrics(name)
            failed.append(name)
        else:
            loaded.append(name)
    return PluginSpecs(specs=_plugin_specs_for(loaded), loaded=loaded, failed=failed)


def _import_plugin_metrics(module_name: str, metrics_py: Path) -> None:
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
    if module_name in sys.modules:
        return
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


def load_installed_plugin_specs(
    conn: psycopg.Connection, *, skip_names: Iterable[str] = ()
) -> PluginSpecs:
    """Load the enabled installed plugins from the registry (kind='plugin').

    Row presence and ``default_enabled`` decide what ships panels — the
    content comes from the blob, unpacked into a scratch directory for the
    import; repo-source rows carry no blob and are the repo supplier's job.
    ``skip_names`` holds plugins the caller already loaded (e.g. from the
    checkout) — they are not imported twice. A row whose blob is missing, or
    whose ``metrics.py`` fails to import, is reported and skipped; the rest
    still load.
    """
    loaded: list[str] = []
    failed: list[str] = []
    already = set(skip_names)
    for extension in extension_registry.list_enabled(conn, kind="plugin"):
        if extension.is_repo_source or extension.name in already:
            continue
        if extension.content_hash is None:  # pragma: no cover — schema-forbidden
            logger.error("plugin {name} has no content hash — skipped", name=extension.name)
            failed.append(extension.name)
            continue
        archive = extension_registry.get_blob(conn, extension.content_hash)
        if archive is None:  # pragma: no cover — schema-forbidden
            logger.error(
                "plugin {name} points at content_hash {digest} with no blob — skipped",
                name=extension.name,
                digest=extension.content_hash,
            )
            failed.append(extension.name)
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
                with PluginContext(extension.name):
                    _import_plugin_metrics(
                        f"ava_installed_plugins.{extension.name}.metrics", metrics_py
                    )
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            plugin_load_report.report_plugin_load_failure(extension.name, exc)
            drop_plugin_metrics(extension.name)
            failed.append(extension.name)
        else:
            loaded.append(extension.name)
    return PluginSpecs(specs=_plugin_specs_for(loaded), loaded=loaded, failed=failed)


@contextmanager
def _unpacked_plugin(name: str, archive: bytes) -> Generator[Path]:
    """Unpack a plugin blob into a scratch tree; yields the tree root. Owns
    (and removes) the scratch tree it creates."""
    with tempfile.TemporaryDirectory(prefix=f"ava-plugin-{name}-") as scratch:
        tree = Path(scratch)
        extension_registry.unpack_tree(archive, tree)
        yield tree


def collect_plugin_specs(conn: psycopg.Connection | None = None) -> PluginSpecs:
    """The dual supplier: repo plugins (checkout) plus enabled installed
    plugins (registry rows + blobs), deduplicated by plugin name — a name
    already loaded from the checkout is not loaded again from the registry."""
    repo = load_repo_plugin_specs()
    loaded = list(repo.loaded)
    failed = list(repo.failed)
    specs = list(repo.specs)
    if conn is not None:
        installed = load_installed_plugin_specs(conn, skip_names=loaded)
        loaded.extend(installed.loaded)
        failed.extend(installed.failed)
        specs.extend(installed.specs)
    return PluginSpecs(specs=specs, loaded=loaded, failed=failed)


def render_dashboard_json(*, repo_only: bool = False) -> tuple[str, tuple[str, ...]]:
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
    from shared import core_metrics
    from shared.grafana_dashboard import render_dashboard, render_to_json

    core_specs = core_metrics.collect_core_metrics()
    if repo_only:
        plugins = collect_plugin_specs()
    else:
        from shared.db import connect

        with connect() as conn:
            plugins = collect_plugin_specs(conn)
    rendered = render_to_json(render_dashboard(core_specs, plugins.specs))
    return rendered, tuple(sorted(plugins.failed))
