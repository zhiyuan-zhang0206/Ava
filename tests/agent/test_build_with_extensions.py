"""build_graph integration with plugins config — unified plugin model.

Confirms:
- Plugin config is read from the per-machine local file at graph build time
- Only enabled=true plugins are imported
- enabled=false plugins are not imported
- A repeat load re-executes the module already in sys.modules (issue #147)
"""

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from shared import paths
from shared.config import settings
from shared.plugins_config import write_local

# Every dotted name `_load_extensions` can register a plugin module under.
_PLUGIN_MODULE_PREFIXES = ("ava_builtins.plugins.", "plugins.")


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


@pytest.fixture(autouse=True)
def _restore_plugin_modules() -> Iterator[None]:
    """The synthetic plugins here live in a per-test tmp dir but claim a
    process-global dotted name, so a load leaks a module object bound to a
    directory the next test has already discarded — the pollution class of
    issue #147. Snapshot the plugin namespace and put it back, same shape as
    the migration-authority fix in c86c63dce.
    """
    before = {k: v for k, v in sys.modules.items() if k.startswith(_PLUGIN_MODULE_PREFIXES)}
    yield
    for key in [
        k for k in sys.modules if k.startswith(_PLUGIN_MODULE_PREFIXES) and k not in before
    ]:
        del sys.modules[key]
    sys.modules.update(before)


def _make_plugin(name: str, body: str | None = None) -> None:
    root = paths.repo_plugins_dir()
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.py").write_text(body or f"__description__ = '{name}'\n")


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


def test_a_repeat_load_reuses_the_module_object_so_a_patch_still_lands(
    monkeypatch: pytest.MonkeyPatch,
):
    """Issue #147: a second `_load_extensions()` re-executes the plugin module
    already registered in `sys.modules` — it must not bind a new one.

    Replacing it forks the module identity. Whoever imported the plugin before
    the load — a test's module-level `from ...plugin import hook`, a hook object
    built at import time — keeps the old object, while `mock.patch` resolves the
    dotted path through `sys.modules` and patches the new one. The patch then
    silently never reaches the code under test: it is not a mock failure, it is
    two live copies of one module. `tests/agent/test_syntax_fix.py` failed
    exactly this way whenever a plugin-loading sibling ran first in the same
    xdist worker, and passed in isolation.

    Repeated in-process loads are a production path too, not only a test
    fixture: `agent/plugin_catalog.py:build_catalog()` loads in the calling
    process, and the runner-hosted executor sketched in
    `future/infra/extension-ownership.md` would reload as the activated union
    changes rather than once at process boot.
    """
    _make_plugin(
        "demo",
        "__description__ = 'demo'\nMARK = 'real'\n\n\ndef probe():\n    return MARK\n",
    )
    write_local({"plugins": {"demo": {"enabled": True}}})

    from agent.graph import _build

    _build._load_extensions()
    dotted = "ava_builtins.plugins.demo.plugin"
    first = sys.modules[dotted]
    stale_probe = first.probe  # the reference an earlier importer would be holding

    _build._load_extensions()

    assert sys.modules[dotted] is first
    # The consequence that actually bites: a callable captured before the reload
    # resolves its globals out of the one module `__dict__`, so a patch applied
    # through `sys.modules` reaches it. Two module objects, and it would not.
    monkeypatch.setattr(sys.modules[dotted], "MARK", "patched")
    assert stale_probe() == "patched"


def test_a_different_file_under_the_same_name_gets_a_fresh_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Reuse is keyed on the file, not just the dotted name.

    Plugin names are unique within one discovery pass, but a tmp-dir plugin can
    claim a dotted name that an earlier test's plugin already registered. Those
    are different modules; executing the new file into the old one's globals
    would let the dead file's names survive into the live one — the same
    cross-test leak this reuse exists to close, only pointed the other way.
    """
    _make_plugin("demo", "__description__ = 'demo'\nGHOST = 'first file'\n")
    write_local({"plugins": {"demo": {"enabled": True}}})

    from agent.graph import _build

    _build._load_extensions()
    dotted = "ava_builtins.plugins.demo.plugin"
    first = sys.modules[dotted]
    assert first.GHOST == "first file"

    other_repo = tmp_path / "other_repo_plugins"
    other_repo.mkdir()
    monkeypatch.setattr(paths, "repo_plugins_dir", lambda: other_repo)
    _make_plugin("demo", "__description__ = 'demo'\n")

    _build._load_extensions()

    assert sys.modules[dotted] is not first
    assert not hasattr(sys.modules[dotted], "GHOST")


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


# --- fail-soft loading (2026-08-28 ava_ledger incident) --------------------


def _make_external_plugin_with(name: str, body: str) -> None:
    """External plugin with an explicit plugin.py body (the default helper
    writes only `__description__`)."""
    plugin_dir = paths.plugins_dir() / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.py").write_text(body, encoding="utf-8")


def _capture_plugin_load_events(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, dict[str, object]]]:
    """Route shared.telemetry.emit into a list of (event_name, attributes)."""
    import shared.telemetry

    events: list[tuple[str, dict[str, object]]] = []

    def fake_emit(
        category: str,
        event_name: str,
        *,
        level: str = "info",
        attributes: dict[str, object] | None = None,
        **kw: object,
    ) -> None:
        events.append((event_name, attributes or {}))

    monkeypatch.setattr(shared.telemetry, "emit", fake_emit)
    return events


def test_broken_external_plugin_skipped_and_others_load(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict]
) -> None:
    """Fail-soft contract: a plugin whose plugin.py raises at import is skipped
    with a loud warning; the load proceeds; other plugins load.

    The 2026-08-28 ava_ledger incident shape: plugin.py present, a sibling
    module it imports missing — `from . import _ledger` raised
    ModuleNotFoundError and took `import ava` down with it.
    """
    _make_external_plugin_with("ava_ledger", "from . import _missing\n__description__ = 'x'\n")
    _make_external_plugin("audit")
    write_local({"plugins": {"ava_ledger": {"enabled": True}, "audit": {"enabled": True}}})

    from agent.graph import _build

    events = _capture_plugin_load_events(monkeypatch)

    _build._load_extensions()  # must not raise

    # the healthy plugin loaded
    assert "plugins.audit.plugin" in sys.modules
    # the broken one left no half-executed module behind
    assert "plugins.ava_ledger.plugin" not in sys.modules
    # loud: a loguru error naming the plugin
    assert any(
        "ava_ledger" in r["message"] and "failed to load" in r["message"] for r in loguru_records
    )
    # loud: a plugin_load_failed event carrying the plugin + the exception
    attrs = [a for n, a in events if n == "plugin_load_failed"]
    assert [a["plugin"] for a in attrs] == ["ava_ledger"]
    # the missing sibling surfaces as an ImportError-family exception
    error = str(attrs[0]["error"])
    assert "ImportError" in error and "_missing" in error


def test_broken_builtin_plugin_skipped_and_others_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same fail-soft contract applies to repo-shipped plugins."""
    _make_plugin("syntax_fix", "raise RuntimeError('boom')\n")
    _make_plugin("compact")
    write_local({"plugins": {"syntax_fix": {"enabled": True}, "compact": {"enabled": True}}})

    from agent.graph import _build

    _build._load_extensions()  # must not raise

    assert "ava_builtins.plugins.compact.plugin" in sys.modules
    assert "ava_builtins.plugins.syntax_fix.plugin" not in sys.modules


def test_external_plugin_relative_import_resolves_when_plugins_prefix_is_shadowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sys.path fix: `from . import _helper` inside plugin.py resolves against
    the plugin's own directory even when the top-level `plugins` package
    resolves somewhere else entirely.

    The exec child boots with cwd=$AVA_HOME/source, so `plugins` resolves to
    the checkout's legacy `plugins/` dir (or to nothing) — never to
    $AVA_HOME/plugins. The loader registers the `plugins.<name>` parent
    package itself, so the relative import works regardless of where the
    plain `plugins` name points.
    """
    import types

    # simulate the prod shadow: `plugins` already resolves to a decoy dir
    decoy = types.ModuleType("plugins")
    decoy.__path__ = ["/nonexistent/decoy"]  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setitem(sys.modules, "plugins", decoy)

    pdir = paths.plugins_dir() / "ava_ledger"
    pdir.mkdir(parents=True)
    (pdir / "plugin.py").write_text(
        "from . import _helper\n__description__ = 'x'\nMARK = _helper.VALUE\n",
        encoding="utf-8",
    )
    (pdir / "_helper.py").write_text("VALUE = 7\n", encoding="utf-8")
    write_local({"plugins": {"ava_ledger": {"enabled": True}}})

    from agent.graph import _build

    _build._load_extensions()  # must not raise

    assert sys.modules["plugins.ava_ledger.plugin"].MARK == 7
    assert sys.modules["plugins.ava_ledger"].__path__ == [str(pdir)]


def test_dangling_config_entry_skipped_with_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config entry whose plugin directory is gone (interrupted upgrade,
    manual rm) must not block the plugin load: reported loudly, treated as
    disabled, the rest of the config intact.
    """
    _make_external_plugin("audit")
    write_local({"plugins": {"audit": {"enabled": True}, "vanished": {"enabled": True}}})

    from agent.graph import _build

    events = _capture_plugin_load_events(monkeypatch)

    config = _build._load_extensions()  # must not raise

    assert "plugins.audit.plugin" in sys.modules
    assert "vanished" not in config.plugins
    attrs = [a for n, a in events if n == "plugin_load_failed"]
    assert [a["plugin"] for a in attrs] == ["vanished"]


def test_dot_prefixed_dirs_are_not_discovered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Atomic-install residue (.name.staging / .name.backup-<pid>) must never
    surface as ghost plugins — a hard kill between rename steps would
    otherwise have the loader import a half-installed tree as a plugin."""
    from shared.plugins_config import _discover_plugins

    _make_plugin("real")
    _make_external_plugin("ext")
    # same ghost name in BOTH roots: without the dot-prefix filter this would
    # raise DuplicatePlugin, not just mis-discover
    for root in (paths.repo_plugins_dir(), paths.plugins_dir()):
        ghost = root / ".ghost.staging"
        ghost.mkdir()
        (ghost / "plugin.py").write_text("__description__ = 'ghost'\n", encoding="utf-8")

    assert set(_discover_plugins()) == {"real", "ext"}
