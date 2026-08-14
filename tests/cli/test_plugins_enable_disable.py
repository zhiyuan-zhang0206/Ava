"""`ava plugins enable/disable` write the per-machine local config."""

import json
from pathlib import Path

from cli.commands import cmd_plugins_disable, cmd_plugins_enable


def test_cmd_enable_then_disable(tmp_path: Path, capsys):
    # conftest points AVA_HOME at a tmp dir; create a plugin on disk + seed local.
    from shared import paths
    from shared.plugins_config import local_config_path, write_local

    pdir = paths.plugins_dir() / "compact"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "plugin.py").write_text("__description__ = 'x'\n")
    write_local({"plugins": {"compact": {"enabled": True}}})

    assert cmd_plugins_disable("compact") == 0
    assert json.loads(local_config_path().read_text())["plugins"]["compact"]["enabled"] is False

    assert cmd_plugins_enable("compact") == 0
    assert json.loads(local_config_path().read_text())["plugins"]["compact"]["enabled"] is True


def test_cmd_enable_unknown_returns_1(tmp_path: Path, capsys):
    assert cmd_plugins_enable("nonexistent") == 1
    assert "not present on this machine" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_cmd_enable_accepts_the_dash_spelling_of_an_underscore_plugin(tmp_path: Path, capsys):
    """A plugin directory has to stay a Python package, so `my_plugin` is the
    on-disk identity while `my-plugin` is the name a human writes. The typed name
    folds onto the directory, and the config stays keyed by the directory."""
    from shared import paths
    from shared.plugins_config import local_config_path

    pdir = paths.plugins_dir() / "my_plugin"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "plugin.py").write_text("__description__ = 'x'\n")

    assert cmd_plugins_disable("my-plugin") == 0
    written = json.loads(local_config_path().read_text())["plugins"]
    # keyed by the DIRECTORY, and no dash duplicate alongside it
    assert written["my_plugin"]["enabled"] is False
    assert "my-plugin" not in written


def test_load_folds_a_dash_config_key_onto_the_plugin_dir(tmp_path: Path):
    """A hand-edited plugins_config.json spelling a plugin with dashes resolves
    to the real package instead of raising DanglingPlugin."""
    from shared import paths
    from shared.plugins_config import load, write_local

    pdir = paths.plugins_dir() / "my_plugin"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "plugin.py").write_text("__description__ = 'x'\n")
    write_local({"plugins": {"my-plugin": {"enabled": False}}})

    cfg = load({"my_plugin"})
    assert set(cfg.plugins) == {"my_plugin"}
    assert cfg.plugins["my_plugin"].enabled is False
