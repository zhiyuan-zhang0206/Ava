"""Prepare-only extension/service/provider compatibility probe in a private unit home."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

from agent.graph._build import _load_extensions
from ops.spec import _plugin_services
from shared import paths, plugins_config
from shared.lm._plugin_providers import ensure_provider_plugins_loaded
from shared.runtime_plugins import declared_plugins
from shared.runtime_release import ReleaseRejectedError


def prove_plugin_registration(root: Path, required: tuple[str, ...]) -> None:
    """Import trusted retained code, never audit mutable production plugins.

    Three faces run under the substituted reporter: the `plugin.py` loader,
    the presence-based `services.py` roster, and provider registration
    (`ensure_provider_plugins_loaded`) — a provider that fails to load only
    degrades the model registry at runtime, so a candidate shipping one must
    be rejected here. The image's own provider-bearing built-ins stay enabled
    next to the retained set: the provider loader treats an empty binding set
    as a hard configuration error, and their load exercises the same
    registration contract the retained providers must satisfy.
    """
    known = plugins_config.installed_plugin_dirs()
    if any(known[name] != root / name for name in declared_plugins(root)):
        raise ReleaseRejectedError("plugin discovery did not bind candidate image")
    enabled = set(required)
    builtin_root = str(paths.repo_plugins_dir())
    for name, plugin_dir in known.items():
        if builtin_root in str(plugin_dir.resolve()) and (plugin_dir / "provider.py").is_file():
            enabled.add(name)
    config = {"plugins": {name: {"enabled": name in enabled} for name in known}}
    path = plugins_config.local_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(config, stream)
    with (
        patch("socket.socket.connect", side_effect=RuntimeError("prepare network forbidden")),
        patch("socket.socket.connect_ex", side_effect=RuntimeError("prepare network forbidden")),
        patch("socket.create_connection", side_effect=RuntimeError("prepare network forbidden")),
        # The canonical fail-soft reporter (shared/plugin_load_report.py):
        # substituting it turns any contained plugin load failure — plugin.py,
        # services.py, provider registration, a dangling config entry — back
        # into a hard release rejection, so a candidate image with unloadable
        # plugin code never ships.
        patch(
            "shared.plugin_load_report.report_plugin_load_failure",
            side_effect=ReleaseRejectedError("candidate plugin import failed"),
        ),
    ):
        _load_extensions()
        services = _plugin_services()
        ensure_provider_plugins_loaded()
    for name in required:
        module = sys.modules[f"plugins.{name}.plugin"]
        if Path(module.__file__ or "").resolve() != root / name / "plugin.py":
            raise ReleaseRejectedError("plugin import origin escaped candidate")
    if len({spec.session for spec in services}) != len(services):
        raise ReleaseRejectedError("plugin services have duplicate session names")
