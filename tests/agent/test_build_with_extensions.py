"""build_graph integration with plugins config — unified plugin model.

Confirms:
- Plugin config is read from the per-machine local file at graph build time
- Only enabled=true plugins are imported
- enabled=false plugins are not imported
"""

from pathlib import Path

import pytest

from shared import paths
from shared.config import settings
from shared.plugins_config import write_local


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo_plugins"
    user = tmp_path / "user_plugins"
    repo.mkdir()
    user.mkdir()
    monkeypatch.setattr(paths, "repo_plugins_dir", lambda: repo)
    monkeypatch.setattr(paths, "plugins_dir", lambda: user)
    monkeypatch.setattr(paths, "plugins_config_path", lambda: tmp_path / "plugins.json")
    monkeypatch.setattr(settings.general, "ava_home", str(tmp_path / "ava"))
    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path)


def _make_plugin(name: str) -> None:
    root = paths.repo_plugins_dir()
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.py").write_text(f"__description__ = '{name}'\n")


def _make_external_plugin(name: str) -> None:
    root = paths.plugins_dir()
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.py").write_text(f"__description__ = '{name}'\n")


def test_only_enabled_plugins_are_imported(monkeypatch: pytest.MonkeyPatch):
    """enabled=true plugins are imported; enabled=false are not."""
    _make_plugin("compact")
    _make_plugin("syntax_fix")
    _make_plugin("demo")

    write_local(
        {
            "plugins": {
                "compact": {"enabled": True},
                "syntax_fix": {"enabled": True},
                "demo": {"enabled": False},
            }
        }
    )

    import importlib.util

    from agent.graph import _build

    loaded: list[str] = []
    real = importlib.util.spec_from_file_location

    def spy(name, location, **kw):
        loaded.append(name)  # pyright: ignore[reportUnknownArgumentType]
        return real(name, location, **kw)  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(importlib.util, "spec_from_file_location", spy)  # pyright: ignore[reportUnknownArgumentType]

    _build._load_extensions()

    assert any("compact" in n for n in loaded)
    assert any("syntax_fix" in n for n in loaded)
    assert not any("demo" in n for n in loaded)


def test_external_plugin_also_loaded(monkeypatch: pytest.MonkeyPatch):
    """External plugins (~/.ava/plugins/) are also loaded."""
    _make_plugin("compact")
    _make_external_plugin("audit")

    write_local(
        {
            "plugins": {
                "compact": {"enabled": False},
                "audit": {"enabled": True},
            }
        }
    )

    import importlib.util

    from agent.graph import _build

    loaded: list[str] = []
    real = importlib.util.spec_from_file_location

    def spy(name, location, **kw):
        loaded.append(name)  # pyright: ignore[reportUnknownArgumentType]
        return real(name, location, **kw)  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(importlib.util, "spec_from_file_location", spy)  # pyright: ignore[reportUnknownArgumentType]

    _build._load_extensions()

    assert any("audit" in n for n in loaded)
    assert not any("compact" in n for n in loaded)


def test_load_extensions_installs_sdk_metering(monkeypatch: pytest.MonkeyPatch):
    """_load_extensions must install the SDK-usage recorder over the final ava.*
    surface (after plugins load), so every agent's SDK calls get metered."""
    from agent import sdk_metering
    from agent.graph import _build

    installed: list[bool] = []
    monkeypatch.setattr(sdk_metering, "install", lambda: installed.append(True))

    _build._load_extensions()

    assert installed == [True]


def test_duplicate_plugin_name_raises(monkeypatch: pytest.MonkeyPatch):
    """Same-named plugin in two locations -> _discover_plugins raises DuplicatePlugin."""
    from shared.plugins_config import DuplicatePlugin, _discover_plugins

    _make_plugin("dup")
    _make_external_plugin("dup")

    with pytest.raises(DuplicatePlugin, match="dup"):
        _discover_plugins()


def test_clear_plugin_registrations_keeps_framework_sections():
    """clear_plugin_registrations drops plugin-contributed system prompt
    sections but keeps the framework-owned ones (registered at module import).
    Otherwise a plugin reload silently strips e.g. the always-on skill index for
    the rest of the process — which is exactly how it used to break the system
    prompt snapshot when another test cleared registrations first."""
    from agent.graph._system_prompt import (
        _FRAMEWORK_SECTION_COUNT,
        _SYSTEM_PROMPT_SECTIONS,
        register_system_prompt_section,
    )
    from agent.state import clear_plugin_registrations

    saved = _SYSTEM_PROMPT_SECTIONS[:]
    try:

        def _plugin_section() -> str:
            return "## plugin section"

        register_system_prompt_section(_plugin_section)
        assert _plugin_section in _SYSTEM_PROMPT_SECTIONS

        clear_plugin_registrations()

        assert _FRAMEWORK_SECTION_COUNT >= 1
        assert len(_SYSTEM_PROMPT_SECTIONS) == _FRAMEWORK_SECTION_COUNT
        assert _plugin_section not in _SYSTEM_PROMPT_SECTIONS
    finally:
        _SYSTEM_PROMPT_SECTIONS[:] = saved
