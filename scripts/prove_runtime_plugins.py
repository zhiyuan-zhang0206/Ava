"""CI-only actual agent extension and presence-based service discovery proof."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from agent.graph._build import _load_extensions
from ops.spec import _plugin_services
from services.agent_host.daemon import _plugins_fingerprint
from shared import paths, plugins_config
from shared.runtime_interpreter import runtime_plugins_dir


def require(value: bool, message: str) -> None:  # noqa: FBT001 — assertion predicate.
    if not value:
        raise AssertionError(message)


def main() -> None:
    require(os.environ["GITHUB_ACTIONS"] == "true", "CI-only proof")
    home = Path(os.environ["AVA_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    known = plugins_config.installed_plugin_dirs()
    retained = runtime_plugins_dir() / "runtime_fixture"
    require(known["runtime_fixture"] == retained, "discovery escaped image")
    config = {"plugins": {name: {"enabled": name == "runtime_fixture"} for name in known}}
    plugins_config.local_config_path().write_text(json.dumps(config))
    _load_extensions()
    module = sys.modules["plugins.runtime_fixture.plugin"]
    require(module.VALUE == "retained-resource", "agent plugin did not import resource")
    fingerprint = _plugins_fingerprint()
    mutable = paths.plugins_dir() / "runtime_fixture"
    mutable.mkdir()
    (mutable / "plugin.py").write_text("raise RuntimeError('mutable poison must not execute')\n")
    _load_extensions()
    require(_plugins_fingerprint() == fingerprint, "mutable input triggered host restart")
    require(module.VALUE == "retained-resource", "mutable install changed loaded image")
    # Machine services depend on presence, not agent-facing enable-state.
    config["plugins"]["runtime_fixture"]["enabled"] = False
    plugins_config.local_config_path().write_text(json.dumps(config))
    require(
        any(spec.session == "runtime-fixture" for spec in _plugin_services()),
        "disabled agent plugin lost its installed machine service",
    )
    require(paths.plugins_dir().is_relative_to(home), "installer destination moved into image")
    (home.parent / "plugin-proof.json").write_text(
        json.dumps(
            {
                "sourceAbsent": True,
                "agentExtensionImported": True,
                "presenceServiceDiscovered": True,
                "mutablePoisonIgnored": True,
                "staticResourceRead": True,
                "installerStillMutableHome": True,
            }
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
