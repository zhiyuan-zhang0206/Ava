"""Prepare-only extension/service compatibility probe in a private unit home."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

from agent.graph._build import _load_extensions
from ops.spec import _plugin_services
from shared import plugins_config
from shared.runtime_plugins import declared_plugins
from shared.runtime_release import ReleaseRejectedError


def prove_plugin_registration(root: Path, required: tuple[str, ...]) -> None:
    """Import trusted retained code, never audit mutable production plugins."""
    known = plugins_config.installed_plugin_dirs()
    if any(known[name] != root / name for name in declared_plugins(root)):
        raise ReleaseRejectedError("plugin discovery did not bind candidate image")
    config = {"plugins": {name: {"enabled": name in required} for name in known}}
    path = plugins_config.local_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(config, stream)
    with (
        patch("socket.socket.connect", side_effect=RuntimeError("prepare network forbidden")),
        patch("socket.socket.connect_ex", side_effect=RuntimeError("prepare network forbidden")),
        patch("socket.create_connection", side_effect=RuntimeError("prepare network forbidden")),
        patch(
            "agent.graph._build._report_plugin_load_failure",
            side_effect=ReleaseRejectedError("candidate plugin import failed"),
        ),
    ):
        _load_extensions()
        services = _plugin_services()
    for name in required:
        module = sys.modules[f"plugins.{name}.plugin"]
        if Path(module.__file__ or "").resolve() != root / name / "plugin.py":
            raise ReleaseRejectedError("plugin import origin escaped candidate")
    if len({spec.session for spec in services}) != len(services):
        raise ReleaseRejectedError("plugin services have duplicate session names")
