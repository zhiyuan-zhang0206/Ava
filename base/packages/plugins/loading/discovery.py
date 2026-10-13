"""Which plugins are enabled and where their faces live — the loader's discovery leaf.

`faces.py` (the loader), `registry.py` and `agent/extensions/catalog.py` all start from the same answer:
the enabled plugin directories, the dotted package each loads under, and the two face names the
loader owns. They live here, below all three, so none of them has to import another to learn it.
"""

from __future__ import annotations

from pathlib import Path

from base import paths
from base.packages.plugins import enable_config as plugins_cfg
from base.packages.plugins import load_report

SURFACE_MODULE = "plugin"
FACE_MODULE = "agent_runtime"


def pkg_of(plugin_dir: Path) -> str:
    """The dotted package a plugin's modules load under (builtin vs external)."""
    repo_dir = str(paths.repo_plugins_dir())
    return "ava_builtins.plugins" if repo_dir in str(plugin_dir.resolve()) else "plugins"


def discovered_and_config(
    report: load_report.Reporter | None = None,
) -> tuple[dict[str, Path], plugins_cfg.PluginsConfig]:
    """Discover plugins + read the enable config, fail-soft on dangling entries.

    A config entry whose plugin directory is gone (interrupted upgrade, manual
    rm) must not block the load: each dangling name is reported through the one
    canonical reporter on each failed read via `plugins_cfg.report_dangling`
    and treated as disabled (the same contract the loader always had —
    2026-08-28 ava_ledger incident).
    """
    discovered = plugins_cfg.discover_plugins()
    known = set(discovered)
    try:
        config = plugins_cfg.load(known)
    except plugins_cfg.DanglingPlugin as exc:
        plugins_cfg.report_dangling(exc, report)
        config = plugins_cfg.load(known, allow_dangling=True)
    return discovered, config


def enabled_plugin_dirs(
    report: load_report.Reporter | None = None,
) -> list[tuple[str, Path]]:
    """(name, plugin_dir) for every enabled discovered plugin, name-sorted."""
    discovered, config = discovered_and_config(report)
    return [
        (name, discovered[name])
        for name in sorted(config.plugins)
        if config.plugins[name].enabled and name in discovered
    ]
