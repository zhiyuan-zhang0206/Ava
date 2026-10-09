"""The `ava plugins inspect` catalog — per-machine view of what plugins declare, diff.

The plugins here are written to disk and loaded through the real
`load_extensions`, so what the catalog reports is what an actual plugin load
produced: the SDK surface is what the plugin's `plugin.py` `contribute()` declares, and hooks, state and
prompt sections are what its `agent_runtime.py` `contribute()` declares — not anything the test hands
the registry. The load gate of
`agent.extensions.registry.build_registry` (a plugin whose manifest and `contribute()`
disagree, or whose state declaration is invalid, is left out and reported) is covered at the
bottom, on the same harness.
"""

from pathlib import Path

import pytest

from agent.extensions import catalog as catalog_mod
from agent.extensions import load_extensions
from agent.extensions.registry import declarations
from base import paths
from base.lm.catalog import ModelCatalog
from base.packages.plugins import load_report
from base.packages.plugins.enable_config import write_local
from base.packages.plugins.gate import ContributionMismatch


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_catalog: ModelCatalog):
    monkeypatch.setattr("base.lm.plugin_providers.build_model_catalog", lambda: model_catalog)
    repo = tmp_path / "repo_plugins"
    user = tmp_path / "user_plugins"
    repo.mkdir()
    user.mkdir()
    monkeypatch.setattr(paths, "repo_plugins_dir", lambda: repo)
    monkeypatch.setattr(paths, "plugins_dir", lambda: user)
    monkeypatch.setattr(paths, "plugins_config_path", lambda: tmp_path / "plugins.json")
    monkeypatch.setenv("AVA_HOME", str(tmp_path / "ava"))
    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path)


_DEMO_PLUGIN = '''
"""A demo plugin."""

__description__ = "declares one of nearly everything"

from base.packages.plugins.extensions import PluginContributions, SdkWrap


def _passthrough(inner, *args, **kwargs):
    return inner(*args, **kwargs)


def contribute() -> PluginContributions:
    return PluginContributions(sdk_wraps=(SdkWrap("files.read", _passthrough),))
'''

_DEMO_RUNTIME = """
from pydantic import BaseModel

from agent.hooks import Hook
from base.lm.catalog import ModelCatalog
from base.packages.plugins.extensions import PluginContributions


class DemoState(BaseModel):
    counter: int = 0


class _DemoHook(Hook):
    async def __call__(self, state, runtime, config, /):
        return None


def demo_section(_slices: object, *, catalog: ModelCatalog) -> str:
    return "## Demo"


def contribute() -> PluginContributions:
    return PluginContributions(
        system_prompt_sections=(demo_section,), before_llm=(_DemoHook(),), state=(DemoState,)
    )
"""


def _write_plugin(
    name: str, body: str, *, manifest: str | None = None, runtime: str | None = None
) -> Path:
    plugin_dir = paths.repo_plugins_dir() / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.py").write_text(body)
    if runtime is not None:
        (plugin_dir / "agent_runtime.py").write_text(runtime)
    if manifest is not None:
        (plugin_dir / "ava-plugin.json").write_text(manifest)
    return plugin_dir


def _enable(**plugins: bool) -> None:
    write_local({"plugins": {name: {"enabled": on} for name, on in plugins.items()}})


def test_every_surface_entry_point_resolves():
    """`SURFACES` is hand-written; its entry points are not. Each one must
    resolve to a live object — a moved declaration type breaks the catalog here
    rather than in front of an agent reading a signature that no longer exists."""
    for surface in catalog_mod.SURFACES:
        assert surface.entry_points, f"{surface.id} lists no entry point"
        for entry_point in surface.entry_points:
            rendered = catalog_mod.entry_point_signature(entry_point)
            assert rendered.startswith(entry_point.replace(":", "."))
            assert "(" in rendered


def test_declarations_are_attributed_to_the_declaring_plugin():
    """Every surface a plugin declared shows up under its name, keyed the way the
    manifest declares it."""
    _write_plugin("demo", _DEMO_PLUGIN, runtime=_DEMO_RUNTIME)
    _enable(demo=True)

    catalog = catalog_mod.build_catalog()
    view = catalog.plugin("demo")

    assert view.enabled is True
    assert view.description == "declares one of nearly everything"
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


def test_a_disabled_plugin_reports_no_declarations():
    """A disabled plugin is never imported, so the honest answer is its
    enable-state and nothing else — not a guess read off its source."""
    _write_plugin("demo", _DEMO_PLUGIN, runtime=_DEMO_RUNTIME)
    _enable(demo=False)

    view = catalog_mod.build_catalog().plugin("demo")

    assert view.enabled is False
    assert view.contributions == ()


def test_a_reload_does_not_accumulate_contributions():
    """Each load reads the declarations afresh (and reinstalls the SDK surface over the previous
    install's undo), so a second load reports one contribution per surface, not two."""
    _write_plugin("demo", _DEMO_PLUGIN, runtime=_DEMO_RUNTIME)
    _enable(demo=True)

    first = catalog_mod.build_catalog().plugin("demo").contributions
    second = catalog_mod.build_catalog().plugin("demo").contributions

    assert len(first) == len(second)


def test_framework_hooks_are_not_reported_as_plugin_contributions():
    """The catalog is plugin contributions only. The framework's own hooks (repair, compact,
    capability drift) are `framework_hooks()`, outside every plugin's declaration; reporting
    them would make the catalog describe code no plugin contributed."""
    from agent.hooks.framework import framework_hooks

    _write_plugin("demo", _DEMO_PLUGIN, runtime=_DEMO_RUNTIME)
    _enable(demo=True)

    catalog = catalog_mod.build_catalog()
    reported = {c.detail for view in catalog.plugins for c in view.contributions}
    framework = {
        f"{type(h).__module__}.{type(h).__qualname__}"
        for hooks in framework_hooks().values()
        for h in hooks
    }

    assert framework
    assert not reported & framework
    assert {c.plugin for view in catalog.plugins for c in view.contributions} == {"demo"}


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
__description__ = "declares more than it provides"

from base.packages.plugins.extensions import PluginContributions, SdkWrap


def _passthrough(inner, *args, **kwargs):
    return inner(*args, **kwargs)


def contribute() -> PluginContributions:
    return PluginContributions(sdk_wraps=(SdkWrap("files.write", _passthrough),))
"""

_DECLARED_RUNTIME = """
from agent.hooks import Hook
from base.lm.catalog import ModelCatalog
from base.packages.plugins.extensions import PluginContributions


class _DeclaredHook(Hook):
    async def __call__(self, state, runtime, config, /):
        return None


def demo_section(_slices: object, *, catalog: ModelCatalog) -> str:
    return "## Declared"


def contribute() -> PluginContributions:
    return PluginContributions(
        system_prompt_sections=(demo_section,), before_llm=(_DeclaredHook(),)
    )
"""


def test_declared_vs_registered_reports_both_directions():
    """A manifest surface the plugin does not provide, and a provided surface the manifest does not
    declare — the two halves of the S3 gate, reported rather than enforced. The report reads
    `declarations()`, which is ungated, so a plugin the load gate excludes still shows both
    directions here."""
    _write_plugin("declared", _DECLARED_PLUGIN, manifest=_MANIFEST, runtime=_DECLARED_RUNTIME)
    _enable(declared=True)

    view = catalog_mod.build_catalog().plugin("declared")
    assert view.manifest is not None
    statuses = {
        (e.surface, e.identifier): e.status for e in catalog_mod.declared_vs_registered(view)
    }

    assert statuses[("hooks", "before_llm")] == "ok"
    assert statuses[("hooks", "after_exec")] == "declared-not-registered"
    assert statuses[("systemPromptSections", "demo_section")] == "ok"
    assert statuses[("sdkWraps", "files.write")] == "registered-not-declared"


def test_a_plugin_without_a_manifest_has_no_diff():
    """No manifest means nothing was declared — which is not the same as
    agreement, so the diff is empty rather than all-ok."""
    _write_plugin("demo", _DEMO_PLUGIN, runtime=_DEMO_RUNTIME)
    _enable(demo=True)

    view = catalog_mod.build_catalog().plugin("demo")

    assert view.manifest is None
    assert catalog_mod.declared_vs_registered(view) == ()


def test_install_time_manifest_keys_have_no_runtime_registry():
    """`skills` / `commands` / `mcpServers` / `opsServices` settle on disk at
    install time and `ui` is read straight from the manifest by the console;
    none of them is a `PluginContributions` field a face declares, so the diff must not be able to
    call them missing."""
    assert {
        "skills",
        "commands",
        "mcpServers",
        "opsServices",
        "ui",
    } == catalog_mod.DECLARATION_ONLY_KEYS


def test_unknown_plugin_fails_fast_and_names_the_installed_ones():
    _write_plugin("demo", _DEMO_PLUGIN, runtime=_DEMO_RUNTIME)
    _enable(demo=True)
    catalog = catalog_mod.build_catalog()

    with pytest.raises(catalog_mod.UnknownPlugin, match="demo"):
        catalog.plugin("nope")


def test_a_dashed_name_resolves_to_the_plugin_directory():
    """`ava plugins inspect ava-code` addresses the `ava_code` directory, the
    same folding `plugins_config.load` does for a hand-edited config."""
    _write_plugin("demo_plugin", _DEMO_PLUGIN)
    _enable(demo_plugin=True)

    assert catalog_mod.build_catalog().plugin("demo-plugin").name == "demo_plugin"


# ── the load gate: build_registry() admits a plugin only when its declaration is sound ──

_HOOK_POINTS = ("after_init", "before_llm", "before_exec", "after_exec")


def _gate_runtime(
    hooks: tuple[str, ...] = (), *, section: bool = True, state: str | None = None
) -> str:
    """A face whose `contribute()` provides the given hook points, a section and optional state."""
    classes = "".join(
        f"class _{point.title().replace('_', '')}Hook(Hook):\n"
        "    async def __call__(self, state, runtime, config, /):\n"
        "        return None\n\n\n"
        for point in hooks
    )
    kwargs = [f"{point}=(_{point.title().replace('_', '')}Hook(),)" for point in hooks]
    if section:
        kwargs.append("system_prompt_sections=(gate_section,)")
    if state is not None:
        kwargs.append("state=(GateState,)")
    return (
        "from pydantic import BaseModel\n\n"
        "from agent.hooks import Hook\n"
        "from base.lm.catalog import ModelCatalog\n"
        "from base.packages.plugins.extensions import PluginContributions\n\n\n"
        f"{state or ''}\n\n"
        f"{classes}"
        "def gate_section(_slices: object, *, catalog: ModelCatalog) -> str:\n"
        '    return "## Gate"\n\n\n'
        "def contribute() -> PluginContributions:\n"
        f"    return PluginContributions({', '.join(kwargs)})\n"
    )


def _gate_manifest(
    name: str, *, hooks: tuple[str, ...] = (), sections: tuple[str, ...] = ()
) -> str:
    import json

    return json.dumps(
        {
            "apiVersion": 2,
            "name": name,
            "version": "1.0.0",
            "engines": {"ava": ">=0.1.0"},
            "contributions": {"hooks": list(hooks), "systemPromptSections": list(sections)},
        }
    )


_GATE_PLUGIN = '__description__ = "a plugin under the load gate"\n'


@pytest.fixture
def load_failures(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, BaseException]]:
    """Every plugin load failure the gate reports, as (plugin, exception)."""
    failures: list[tuple[str, BaseException]] = []

    def spy(name: str, exc: BaseException) -> None:
        failures.append((name, exc))

    monkeypatch.setattr(load_report, "report_plugin_load_failure", spy)
    return failures


def _registered() -> dict[str, tuple[str, ...]]:
    """What a fresh load admits (the gate, then the SDK install): plugin -> its declared hook points."""
    registry = load_extensions().registry
    return {
        name: tuple(point for point in _HOOK_POINTS if contributions.hooks(point))  # pyright: ignore[reportArgumentType]
        for name, contributions in registry.plugins
    }


def test_a_plugin_whose_manifest_and_contribute_agree_is_admitted(
    load_failures: list[tuple[str, BaseException]],
):
    _write_plugin(
        "gate_ok",
        _GATE_PLUGIN,
        manifest=_gate_manifest("gate_ok", hooks=("before_llm",), sections=("gate_section",)),
        runtime=_gate_runtime(("before_llm",)),
    )
    _enable(gate_ok=True)

    assert _registered() == {"gate_ok": ("before_llm",)}
    assert load_failures == []


@pytest.mark.parametrize(
    ("manifest_hooks", "provided_hooks", "problem"),
    [
        pytest.param(
            ("before_llm", "after_exec"),
            ("before_llm",),
            "hooks: 'after_exec' is declared but the plugin does not provide it",
            id="declared-hook-missing-from-contribute",
        ),
        pytest.param(
            ("before_llm",),
            ("before_llm", "after_exec"),
            "hooks: 'after_exec' is provided but not declared",
            id="provided-hook-not-declared",
        ),
    ],
)
def test_a_plugin_whose_manifest_and_contribute_disagree_on_hooks_is_excluded(
    load_failures: list[tuple[str, BaseException]],
    manifest_hooks: tuple[str, ...],
    provided_hooks: tuple[str, ...],
    problem: str,
):
    """Both directions refuse the whole plugin, with a load report naming the difference; the
    other plugins' declarations are unaffected."""
    _write_plugin(
        "gate_bad",
        _GATE_PLUGIN,
        manifest=_gate_manifest("gate_bad", hooks=manifest_hooks, sections=("gate_section",)),
        runtime=_gate_runtime(provided_hooks),
    )
    _write_plugin(
        "gate_ok",
        _GATE_PLUGIN,
        manifest=_gate_manifest("gate_ok", hooks=("before_llm",), sections=("gate_section",)),
        runtime=_gate_runtime(("before_llm",)),
    )
    _enable(gate_bad=True, gate_ok=True)

    assert _registered() == {"gate_ok": ("before_llm",)}
    [(name, exc)] = load_failures
    assert name == "gate_bad"
    assert isinstance(exc, ContributionMismatch)
    assert problem in str(exc)
    # The ungated read the catalog uses still sees the excluded plugin's declaration.
    assert "gate_bad" in {declared for declared, _dir, _c in declarations()}


@pytest.mark.parametrize(
    ("manifest_sections", "provides_section", "problem"),
    [
        pytest.param(
            ("gate_section",),
            False,
            "systemPromptSections: 'gate_section' is declared but the plugin does not provide it",
            id="declared-section-missing-from-contribute",
        ),
        pytest.param(
            (),
            True,
            "systemPromptSections: 'gate_section' is provided but not declared",
            id="provided-section-not-declared",
        ),
    ],
)
def test_a_plugin_whose_manifest_and_contribute_disagree_on_sections_is_excluded(
    load_failures: list[tuple[str, BaseException]],
    manifest_sections: tuple[str, ...],
    provides_section: bool,
    problem: str,
):
    _write_plugin(
        "gate_bad",
        _GATE_PLUGIN,
        manifest=_gate_manifest("gate_bad", hooks=("before_llm",), sections=manifest_sections),
        runtime=_gate_runtime(("before_llm",), section=provides_section),
    )
    _enable(gate_bad=True)

    assert _registered() == {}
    [(name, exc)] = load_failures
    assert name == "gate_bad"
    assert isinstance(exc, ContributionMismatch)
    assert problem in str(exc)


def test_a_plugin_without_a_manifest_is_never_gated(load_failures: list[tuple[str, BaseException]]):
    """No `ava-plugin.json` means nothing declared, so nothing to disagree with: whatever
    `contribute()` provides is admitted."""
    _write_plugin(
        "gate_bare",
        _GATE_PLUGIN,
        runtime=_gate_runtime(("before_llm", "after_exec")),
    )
    _enable(gate_bare=True)

    assert _registered() == {"gate_bare": ("before_llm", "after_exec")}
    assert load_failures == []


def test_an_invalid_state_declaration_excludes_the_plugin(
    load_failures: list[tuple[str, BaseException]],
):
    """A core channel (`halted`) is framework-managed every turn: a plugin declaring it is a load
    failure of that plugin, whether or not it ships a manifest, and its other declarations go too."""
    core_state = "class GateState(BaseModel):\n    halted: bool = False\n"
    _write_plugin(
        "gate_state", _GATE_PLUGIN, runtime=_gate_runtime(("before_llm",), state=core_state)
    )
    _write_plugin("gate_ok", _GATE_PLUGIN, runtime=_gate_runtime(("before_llm",)))
    _enable(gate_state=True, gate_ok=True)

    assert _registered() == {"gate_ok": ("before_llm",)}
    [(name, exc)] = load_failures
    assert name == "gate_state"
    assert isinstance(exc, ValueError)
    assert "halted" in str(exc)


def test_a_valid_state_declaration_is_admitted(load_failures: list[tuple[str, BaseException]]):
    state = "class GateState(BaseModel):\n    counter: int = 0\n"
    _write_plugin("gate_state", _GATE_PLUGIN, runtime=_gate_runtime(state=state))
    _enable(gate_state=True)

    registry = load_extensions().registry

    assert [(plugin, cls.__name__) for plugin, cls in registry.state_classes()] == [
        ("gate_state", "GateState")
    ]
    assert load_failures == []
