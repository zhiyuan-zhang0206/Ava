"""A plugin's `default_config.py` config face — read without importing `plugin.py`."""

from __future__ import annotations

from pathlib import Path

import pytest

from base import paths
from base.packages.plugins import config_face
from base.packages.plugins.config_registration import (
    InvalidConfigOverlay,
    overlay_config_classes,
    resolve_overlay_targets,
    validate_config_overlay,
)
from base.packages.plugins.enable_config import write_local

_FACE = (
    "from pydantic import BaseModel, Field\n"
    "from base.packages.plugins.extensions import PluginContributions\n"
    "class Config(BaseModel):\n"
    "    marker: str = Field(default='.git', json_schema_extra={'per_agent': True})\n"
    "    fixed: int = 1\n"
    "def contribute():\n"
    "    return PluginContributions(config=Config)\n"
)


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo_plugins"
    user = tmp_path / "user_plugins"
    repo.mkdir()
    user.mkdir()
    monkeypatch.setattr(paths, "repo_plugins_dir", lambda: repo)
    monkeypatch.setattr(paths, "plugins_dir", lambda: user)
    monkeypatch.setattr(paths, "plugins_config_path", lambda: tmp_path / "plugins.json")
    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path)


def _plugin(name: str, face: str | None) -> Path:
    plugin_dir = paths.plugins_dir() / name
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text(f"__description__ = '{name}'\n")
    if face is not None:
        (plugin_dir / "default_config.py").write_text(face)
    return plugin_dir


def test_the_declared_class_is_read_from_the_face() -> None:
    plugin_dir = _plugin("probe", _FACE)
    cls = config_face.declared_config_class("probe", plugin_dir)
    assert cls is not None
    assert list(cls.model_fields) == ["marker", "fixed"]


def test_a_plugin_without_a_face_declares_no_class() -> None:
    assert config_face.declared_config_class("bare", _plugin("bare", None)) is None


@pytest.mark.parametrize(
    "extra",
    [
        "def contribute():\n    return PluginContributions(config=Config, state=Config)\n",
        "def contribute():\n    return 1\n",
    ],
)
def test_a_config_face_rejects_other_contribution_surfaces(extra: str) -> None:
    body = _FACE.split("def contribute", maxsplit=1)[0] + extra
    with pytest.raises((ValueError, TypeError)):
        config_face.declared_config_class("probe", _plugin("probe", body))


def test_overlay_validation_recognizes_the_enabled_plugins_config() -> None:
    _plugin("probe", _FACE)
    write_local({"plugins": {"probe": {"enabled": True}}})

    assert overlay_config_classes()["probe"].__name__ == "Config"
    assert resolve_overlay_targets({"marker": "x"}) == {"marker": ("probe", "marker")}
    validate_config_overlay({"marker": "x"})
    with pytest.raises(InvalidConfigOverlay, match="type validation"):
        validate_config_overlay({"marker": 3})
    with pytest.raises(InvalidConfigOverlay, match="per_agent"):
        resolve_overlay_targets({"fixed": 2})


def test_overlay_validation_ignores_a_disabled_plugin() -> None:
    _plugin("probe", _FACE)
    write_local({"plugins": {"probe": {"enabled": False}}})

    assert "probe" not in overlay_config_classes()
    with pytest.raises(InvalidConfigOverlay, match="not in framework Settings"):
        resolve_overlay_targets({"marker": "x"})


def test_a_broken_face_is_left_out_not_raised() -> None:
    _plugin("probe", _FACE)
    _plugin("broken", "raise RuntimeError('boom')\n")
    write_local({"plugins": {"probe": {"enabled": True}, "broken": {"enabled": True}}})

    assert sorted(overlay_config_classes()) == ["probe"]


def test_a_pure_face_admits_config_and_core_dependencies() -> None:
    body = _FACE.replace(
        "config=Config)", "config=Config, flags=('daemon.notice_ttl_limit_seconds',))"
    )
    declaration = config_face.configuration_declaration("probe", _plugin("probe", body))
    assert declaration.config is not None
    assert declaration.flags == ("daemon.notice_ttl_limit_seconds",)


def test_a_dependency_only_face_needs_no_config_class() -> None:
    body = _FACE.replace("config=Config", "flags=('daemon.notice_ttl_limit_seconds',)")
    plugin_dir = _plugin("probe", body)
    assert config_face.declared_config_class("probe", plugin_dir) is None
    assert config_face.configuration_declaration("probe", plugin_dir).flags


@pytest.mark.parametrize("key", ["data_plane.db_url", "daemon.not_a_field", "a.b.c"])
def test_pure_admission_rejects_sensitive_and_unknown_core_dependencies(key: str) -> None:
    from base.packages.plugins.flags import UnknownFlag

    body = _FACE.replace("config=Config)", f"config=Config, flags=({key!r},))")
    with pytest.raises(UnknownFlag):
        config_face.configuration_declaration("probe", _plugin("probe", body))
