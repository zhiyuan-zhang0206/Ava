"""base/packages/plugins/enable_config.py unit tests — schema + load + validation.

Plugin config is per-machine local only: reads and writes go through
~/.meiva/plugins_config.json. No DB involvement.
"""

import json
from pathlib import Path

import pytest

from base import paths
from base.packages.plugins.enable_config import (
    DanglingPlugin,
    DuplicatePlugin,
    PluginEntry,
    PluginsConfig,
    PluginsConfigError,
    SchemaInvalid,
    discover_plugins,
    load,
    parse_description,
    write_local,
)


@pytest.fixture(autouse=True)
def _isolate_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """All tests use a temporary directory for plugin scanning and meiva_home —
    avoids polluting the real repo / user plugins or ~/.meiva."""
    repo = tmp_path / "repo_plugins"
    user = tmp_path / "user_plugins"
    repo.mkdir()
    user.mkdir()
    monkeypatch.setattr(paths, "repo_plugins_dir", lambda: repo)
    monkeypatch.setattr(paths, "plugins_dir", lambda: user)
    monkeypatch.setattr(paths, "meiva_home", lambda: tmp_path)
    monkeypatch.setenv("MEIVA_HOME", str(tmp_path))


def _make_external_plugin(name: str) -> None:
    """Create a plugin in the external plugin directory."""
    user_root = paths.plugins_dir()
    plugin_dir = user_root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.py").write_text(f'__description__ = "{name} plugin"\n')


def _make_plugin_dir(name: str, tmp_path: Path) -> Path:
    root = paths.repo_plugins_dir()
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.py").write_text(f'__description__ = "{name} plugin"\n')
    return plugin_dir


# ── schema ──


def test_plugin_entry_defaults():
    entry = PluginEntry(enabled=True)
    assert entry.enabled is True


def test_plugins_config_defaults():
    cfg = PluginsConfig()
    assert cfg.plugins == {}


# ── load ──


def test_load_writes_default_when_no_local_file(tmp_path: Path):
    _make_plugin_dir("compact", tmp_path)
    _make_plugin_dir("syntax_fix", tmp_path)

    cfg = load({"compact", "syntax_fix"})
    assert set(cfg.plugins) == {"compact", "syntax_fix"}
    assert all(e.enabled for e in cfg.plugins.values())


def test_load_reads_local_file(tmp_path: Path):
    """End-to-end: load() honors the per-machine file via _read_raw."""
    from base.packages.plugins.enable_config import local_config_path

    _make_plugin_dir("compact", tmp_path)
    local_config_path().write_text(json.dumps({"plugins": {"compact": {"enabled": False}}}))
    cfg = load({"compact"})
    assert cfg.plugins["compact"].enabled is False


def test_load_reads_existing_config(tmp_path: Path):
    _make_plugin_dir("compact", tmp_path)
    write_local({"plugins": {"compact": {"enabled": False}}})

    cfg = load({"compact"})
    assert cfg.plugins["compact"].enabled is False


def test_load_auto_merges_new_plugins(tmp_path: Path):
    _make_plugin_dir("compact", tmp_path)
    _make_plugin_dir("syntax_fix", tmp_path)
    write_local({"plugins": {"compact": {"enabled": False}}})

    cfg = load({"compact", "syntax_fix"})
    assert cfg.plugins["compact"].enabled is False
    assert cfg.plugins["syntax_fix"].enabled is True


def test_load_auto_merge_new_plugins_deterministic_order(tmp_path: Path):
    """New in-memory entries preserve the sorted plugin-load invariant."""
    for name in ("zeta", "alpha", "mid"):
        _make_plugin_dir(name, tmp_path)
    write_local({"plugins": {"alpha": {"enabled": False}}})

    cfg = load({"zeta", "alpha", "mid"})

    assert list(cfg.plugins) == ["alpha", "mid", "zeta"]


def test_load_raises_on_dangling(tmp_path: Path):
    _make_plugin_dir("compact", tmp_path)
    write_local({"plugins": {"ghost": {"enabled": True}}})

    with pytest.raises(DanglingPlugin, match="ghost"):
        load({"compact"})


# ── set_local_enabled ──


def test_set_local_enabled_writes_sorted_order(tmp_path: Path):
    """The written plugins.json key order is sorted, not set-hash-dependent —
    the on-disk iteration order drives hook registration order, so it must be
    deterministic. Created out of alphabetical order on purpose."""
    from base.packages.plugins.enable_config import local_config_path, set_local_enabled

    for name in ("zeta", "alpha", "mid"):
        _make_plugin_dir(name, tmp_path)

    set_local_enabled("mid", enabled=False)

    written = json.loads(local_config_path().read_text())
    assert list(written["plugins"].keys()) == ["alpha", "mid", "zeta"]
    assert written["plugins"]["mid"]["enabled"] is False
    assert written["plugins"]["alpha"]["enabled"] is True


# ── parse_description ──


def test_parse_description_from_plugin_file(tmp_path: Path):
    plugin_py = tmp_path / "plugin.py"
    plugin_py.write_text('__description__ = "test plugin"\n')
    assert parse_description(plugin_py) == "test plugin"


def test_parse_description_falls_back_to_docstring(tmp_path: Path):
    plugin_py = tmp_path / "plugin.py"
    plugin_py.write_text('"""Test docstring.\n\nMore details."""\n')
    assert parse_description(plugin_py) == "Test docstring."


def test_parse_description_empty_when_nothing(tmp_path: Path):
    plugin_py = tmp_path / "plugin.py"
    plugin_py.write_text("x = 1\n")
    assert parse_description(plugin_py) == ""


# ── discover_plugins ──


def test_discover_finds_repo_plugins(tmp_path: Path):
    _make_plugin_dir("myplugin", tmp_path)
    discovered = discover_plugins()
    assert "myplugin" in discovered


def test_discover_duplicate_raises(tmp_path: Path):
    _make_plugin_dir("dup", tmp_path)
    _make_external_plugin("dup")
    with pytest.raises(DuplicatePlugin, match="dup"):
        discover_plugins()


# ── error hierarchy ──


def test_schema_invalid_is_config_error():
    assert issubclass(SchemaInvalid, PluginsConfigError)


def test_dangling_is_config_error():
    assert issubclass(DanglingPlugin, PluginsConfigError)


def test_duplicate_is_config_error():
    assert issubclass(DuplicatePlugin, PluginsConfigError)


# ── per-machine local enable/disable ──


def test_set_local_enabled_writes_local_file(tmp_path: Path):
    """Toggling writes the per-machine local file; the flag flips."""
    from base.packages.plugins.enable_config import (
        local_config_path,
        set_local_enabled,
    )

    _make_plugin_dir("compact", tmp_path)
    write_local({"plugins": {"compact": {"enabled": True}}})

    cfg = set_local_enabled("compact", enabled=False)
    assert cfg.plugins["compact"].enabled is False

    on_disk = json.loads(local_config_path().read_text())
    assert on_disk["plugins"]["compact"]["enabled"] is False


def test_set_local_enabled_scopes_to_local_plugins(tmp_path: Path):
    """The written local file references only plugins present on THIS machine,
    so a later load() never raises DanglingPlugin."""
    from base.packages.plugins.enable_config import set_local_enabled

    _make_plugin_dir("compact", tmp_path)
    # A stale local file references a plugin that is NOT on this machine.
    write_local({"plugins": {"compact": {"enabled": True}, "ghost": {"enabled": True}}})

    set_local_enabled("compact", enabled=False)
    cfg = load({"compact"})  # would raise DanglingPlugin if 'ghost' survived
    assert "ghost" not in cfg.plugins
    assert cfg.plugins["compact"].enabled is False


def test_set_local_enabled_rejects_unknown(tmp_path: Path):
    """A name not present on this machine is refused (no file written)."""
    from base.packages.plugins.enable_config import DanglingPlugin, set_local_enabled

    with pytest.raises(DanglingPlugin):
        set_local_enabled("nonexistent", enabled=True)


# ── corrupt-file philosophy (audit #9) ──


def test_load_raises_on_malformed_json(tmp_path: Path):
    """A corrupt local file fails fast instead of silently degrading to the
    all-enabled default — the same rule install_registry applies to
    installed.json (audit #9)."""
    from base.packages.plugins.enable_config import local_config_path

    _make_plugin_dir("compact", tmp_path)
    local_config_path().write_text("{ not json")
    with pytest.raises(json.JSONDecodeError):
        load({"compact"})


def test_load_raises_on_non_object_json(tmp_path: Path):
    from base.packages.plugins.enable_config import local_config_path

    _make_plugin_dir("compact", tmp_path)
    local_config_path().write_text("[1, 2]")
    with pytest.raises(PluginsConfigError):
        load({"compact"})


def test_load_empty_file_is_default_all_enabled(tmp_path: Path):
    from base.packages.plugins.enable_config import local_config_path

    _make_plugin_dir("compact", tmp_path)
    local_config_path().write_text("   \n")
    cfg = load({"compact"})
    assert cfg.plugins["compact"].enabled is True
