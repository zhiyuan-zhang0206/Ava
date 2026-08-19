"""The `ava plugins inspect` catalog — attribution ledger, per-machine view, diff.

The plugins here are written to disk and loaded through the real
`_load_extensions`, so what the ledger reports is what an actual plugin import
produced: attribution comes from the `PluginContext` the loader opens, not from
anything the test hands the registry.
"""

from pathlib import Path

import pytest

from agent import plugin_catalog
from shared import paths, plugin_contributions
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


_DEMO_PLUGIN = '''
"""A demo plugin."""

__description__ = "registers one of nearly everything"

from pydantic import BaseModel

import ava
from agent.graph._system_prompt import register_system_prompt_section
from agent.hooks import Hook, register_before_llm
from agent.state import register_plugin_state


class DemoState(BaseModel):
    counter: int = 0


register_plugin_state(DemoState)


class _DemoHook(Hook):
    async def __call__(self, state, runtime, config, /):
        return None


register_before_llm(_DemoHook())


@register_system_prompt_section
def demo_section() -> str:
    return "## Demo"


def _passthrough(inner, *args, **kwargs):
    return inner(*args, **kwargs)


ava.extend.wrap("files.read", _passthrough)
'''


def _write_plugin(name: str, body: str, *, manifest: str | None = None) -> Path:
    plugin_dir = paths.repo_plugins_dir() / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.py").write_text(body)
    if manifest is not None:
        (plugin_dir / "ava-plugin.json").write_text(manifest)
    return plugin_dir


def _enable(**plugins: bool) -> None:
    write_local({"plugins": {name: {"enabled": on} for name, on in plugins.items()}})


def test_every_surface_entry_point_resolves():
    """`SURFACES` is hand-written; its entry points are not. Each one must
    resolve to a live callable — a moved `register_*` breaks the catalog here
    rather than in front of an agent reading a signature that no longer exists."""
    for surface in plugin_catalog.SURFACES:
        assert surface.entry_points, f"{surface.id} lists no entry point"
        for entry_point in surface.entry_points:
            rendered = plugin_catalog.entry_point_signature(entry_point)
            assert rendered.startswith(entry_point.replace(":", "."))
            assert "(" in rendered


def test_registrations_are_attributed_to_the_importing_plugin():
    """Every surface a plugin touched shows up under its name, keyed the way the
    manifest declares it."""
    _write_plugin("demo", _DEMO_PLUGIN)
    _enable(demo=True)

    catalog = plugin_catalog.build_catalog()
    view = catalog.plugin("demo")

    assert view.enabled is True
    assert view.description == "registers one of nearly everything"
    assert view.surface_counts() == {
        "hooks": 1,
        "state": 1,
        "sdkWraps": 1,
        "systemPromptSections": 1,
    }
    by_surface = {c.surface: c for c in view.contributions}
    assert by_surface["hooks"].identifier == "before_llm"
    assert by_surface["state"].identifier == "demo__counter"
    assert by_surface["sdkWraps"].identifier == "files.read"
    assert by_surface["systemPromptSections"].identifier == "demo_section"
    assert all(c.plugin == "demo" for c in view.contributions)


def test_a_disabled_plugin_reports_no_registrations():
    """A disabled plugin is never imported, so the honest answer is its
    enable-state and nothing else — not a guess read off its source."""
    _write_plugin("demo", _DEMO_PLUGIN)
    _enable(demo=False)

    view = plugin_catalog.build_catalog().plugin("demo")

    assert view.enabled is False
    assert view.contributions == ()


def test_a_reload_does_not_accumulate_contributions():
    """`clear_plugin_registrations` clears the ledger with the registries it
    shadows, so a second load reports one contribution per surface, not two."""
    _write_plugin("demo", _DEMO_PLUGIN)
    _enable(demo=True)

    first = plugin_catalog.build_catalog().plugin("demo").contributions
    second = plugin_catalog.build_catalog().plugin("demo").contributions

    assert len(first) == len(second)


def test_framework_registrations_are_not_recorded():
    """The ledger is plugin contributions only. The framework registers its own
    hooks through the same entry points, outside any `PluginContext`; recording
    those would make a reload change what the catalog reports about code that
    never reloads."""
    from agent.hooks import Hook, register_before_llm

    class _FrameworkHook(Hook):
        async def __call__(self, state, runtime, config, /):
            return None

    before = len(plugin_contributions.contributions())
    register_before_llm(_FrameworkHook())

    assert len(plugin_contributions.contributions()) == before


_MANIFEST = """{
  "apiVersion": 2,
  "name": "declared",
  "version": "1.0.0",
  "engines": {"ava": ">=0.1.0"},
  "contributions": {
    "hooks": ["before_llm", "after_exec"],
    "systemPromptSections": ["demo_section"],
    "skills": ["something-on-disk"]
  }
}"""

_DECLARED_PLUGIN = """
__description__ = "declares more than it registers"

import ava
from agent.graph._system_prompt import register_system_prompt_section
from agent.hooks import Hook, register_before_llm


class _DeclaredHook(Hook):
    async def __call__(self, state, runtime, config, /):
        return None


register_before_llm(_DeclaredHook())


@register_system_prompt_section
def demo_section() -> str:
    return "## Declared"


def _passthrough(inner, *args, **kwargs):
    return inner(*args, **kwargs)


ava.extend.wrap("files.write", _passthrough)
"""


def test_declared_vs_registered_reports_both_directions():
    """A declared surface nobody registered, and a registration nobody
    declared — the two halves of the S3 gate, reported rather than enforced."""
    _write_plugin("declared", _DECLARED_PLUGIN, manifest=_MANIFEST)
    _enable(declared=True)

    view = plugin_catalog.build_catalog().plugin("declared")
    assert view.manifest is not None
    statuses = {
        (e.surface, e.identifier): e.status for e in plugin_catalog.declared_vs_registered(view)
    }

    assert statuses[("hooks", "before_llm")] == "ok"
    assert statuses[("hooks", "after_exec")] == "declared-not-registered"
    assert statuses[("systemPromptSections", "demo_section")] == "ok"
    assert statuses[("sdkWraps", "files.write")] == "registered-not-declared"


def test_a_plugin_without_a_manifest_has_no_diff():
    """No manifest means nothing was declared — which is not the same as
    agreement, so the diff is empty rather than all-ok."""
    _write_plugin("demo", _DEMO_PLUGIN)
    _enable(demo=True)

    view = plugin_catalog.build_catalog().plugin("demo")

    assert view.manifest is None
    assert plugin_catalog.declared_vs_registered(view) == ()


def test_install_time_manifest_keys_have_no_runtime_registry():
    """`skills` / `commands` / `mcpServers` / `opsServices` settle on disk at
    install time; the diff must not be able to call them missing."""
    assert {
        "skills",
        "commands",
        "mcpServers",
        "opsServices",
    } == plugin_catalog.DECLARATION_ONLY_KEYS


def test_unknown_plugin_fails_fast_and_names_the_installed_ones():
    _write_plugin("demo", _DEMO_PLUGIN)
    _enable(demo=True)
    catalog = plugin_catalog.build_catalog()

    with pytest.raises(plugin_catalog.UnknownPlugin, match="demo"):
        catalog.plugin("nope")


def test_a_dashed_name_resolves_to_the_plugin_directory():
    """`ava plugins inspect ava-code` addresses the `ava_code` directory, the
    same folding `plugins_config.load` does for a hand-edited config."""
    _write_plugin("demo_plugin", _DEMO_PLUGIN)
    _enable(demo_plugin=True)

    assert plugin_catalog.build_catalog().plugin("demo-plugin").name == "demo_plugin"
