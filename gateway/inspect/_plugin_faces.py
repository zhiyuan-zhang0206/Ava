"""Which shipped plugins the gateway's inspector surfaces load faces from.

The gateway never runs plugin code of its own beyond a plugin's data faces (`metrics.py`,
`inspector.py`); a face is loaded for a builtin plugin only while that plugin is ENABLED
(`plugins_config`), consulted per request, so `ava plugins disable` takes effect without a gateway
restart. Metrics and widgets use the same rule. (The Grafana dashboard supply is a different
surface: it renders panels for every shipped plugin so a disabled plugin's history stays visible.)
"""

from __future__ import annotations

from pathlib import Path

from base.packages.plugins import enable_config


def enabled_face_files(plugins_dir: Path, filename: str) -> list[Path]:
    """`filename` of every ENABLED plugin directory under `plugins_dir` that ships it, sorted by
    plugin name."""
    if not plugins_dir.is_dir():
        return []
    installed = enable_config.installed_plugin_dirs()
    config = enable_config.load_for_runtime(set(installed))
    files: list[Path] = []
    for plugin_dir in sorted(plugins_dir.iterdir()):
        face = plugin_dir / filename
        if not face.is_file():
            continue
        entry = config.plugins.get(plugin_dir.name)
        if entry is None or not entry.enabled:
            continue
        files.append(face)
    return files
