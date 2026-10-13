"""The recovery CLI upgrades a retired-only plugin image without loading the plugin runtime.

Split out of `base/packages/tests/test_update_disk_images.py`: it drives `cli.commands.extensions.plugins` against the fleet plugin's real config model."""

import json
from pathlib import Path

import pytest

from base import paths
from base.packages.plugins.config_registration import disk_image_path


@pytest.fixture(autouse=True)
def _isolate_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """All tests use a temporary directory for plugin scanning and ava_home —
    avoids polluting the real repo / user plugins or ~/.ava."""
    repo = tmp_path / "repo_plugins"
    user = tmp_path / "user_plugins"
    repo.mkdir()
    user.mkdir()
    monkeypatch.setattr(paths, "repo_plugins_dir", lambda: repo)
    monkeypatch.setattr(paths, "plugins_dir", lambda: user)
    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path)
    monkeypatch.setenv("AVA_HOME", str(tmp_path))


def _make_plugin_dir(name: str, tmp_path: Path) -> Path:
    root = paths.repo_plugins_dir()
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.py").write_text(f'__description__ = "{name} plugin"\n')
    return plugin_dir


def test_plugins_update_reconciles_retired_only_image(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The recovery CLI upgrades the old image without loading the plugin runtime."""
    from ava_builtins.plugins.ava_fleet.default_config import FleetConfig
    from cli.commands.extensions.plugins import cmd_plugins_update

    plugin_dir = _make_plugin_dir("ava_fleet", tmp_path)
    (plugin_dir / "plugin.py").write_text('raise RuntimeError("runtime must not load")\n')
    (plugin_dir / "default_config.py").write_text(
        "from ava_builtins.plugins.ava_fleet.default_config import contribute\n"
    )
    image = disk_image_path("ava_fleet")
    image.parent.mkdir(parents=True)
    image.write_text('{"agent_standing_directives": []}\n')

    assert cmd_plugins_update() == 0
    assert json.loads(image.read_text()) == FleetConfig().model_dump()
    saved = image.read_bytes()
    capsys.readouterr()
    assert cmd_plugins_update() == 0
    assert "ava_fleet: no schema diff" in capsys.readouterr().out
    assert image.read_bytes() == saved
