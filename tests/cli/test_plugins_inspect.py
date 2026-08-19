"""`ava plugins inspect` rendering — the shape an agent greps.

The catalog itself is covered in `tests/agent/test_plugin_catalog.py`; here it is
handed in ready-made, so these tests fix the OUTPUT contract: the line prefixes,
the diff vocabulary, and the exit codes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.plugin_catalog import SURFACES, Catalog, PluginView
from cli.commands import plugins_inspect
from shared.plugin_contributions import Contribution
from shared.plugin_manifest import Dependencies, Lifecycle, PluginManifest


def _manifest(**contributions: object) -> PluginManifest:
    return PluginManifest(
        name="declared",
        version="1.0.0",
        engines={"ava": ">=0.1.0"},
        description=None,
        contributions=contributions,
        dependencies=Dependencies(plugins={}, python_packages={}, host_capabilities={}),
        lifecycle=Lifecycle(entry=None, activation="immediate", dispose="effect-registry"),
    )


def _view(
    name: str = "demo",
    *,
    enabled: bool = True,
    contributions: tuple[Contribution, ...] = (),
    manifest: PluginManifest | None = None,
) -> PluginView:
    return PluginView(
        name=name,
        enabled=enabled,
        builtin=True,
        directory=Path("/repo/ava_builtins/plugins") / name,
        description=f"{name} does things",
        contributions=contributions,
        manifest=manifest,
    )


def _install(monkeypatch: pytest.MonkeyPatch, *views: PluginView) -> None:
    monkeypatch.setattr(
        plugins_inspect, "build_catalog", lambda: Catalog(surfaces=SURFACES, plugins=views)
    )


_HOOK = Contribution(
    surface="hooks", identifier="before_llm", plugin="demo", detail="demo.plugin._DemoHook"
)
_WRAP = Contribution(
    surface="sdkWraps", identifier="files.read", plugin="demo", detail="demo.plugin._wrapped_read"
)
_STATE = Contribution(
    surface="state", identifier="demo__counter", plugin="demo", detail="DemoState.counter: int"
)


def test_no_argument_lists_every_surface_and_every_plugin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    _install(monkeypatch, _view(contributions=(_HOOK, _WRAP)), _view("quiet", enabled=False))

    assert plugins_inspect.cmd_plugins_inspect(None) == 0

    out = capsys.readouterr().out
    for surface in SURFACES:
        assert f"surface {surface.id}\n" in out
    # The signature is rendered live, not transcribed — so it carries the real
    # parameter name a plugin author passes.
    assert "entry        agent.hooks.register_before_llm(hook: Hook) -> None" in out
    assert "plugin demo  enabled  builtin  hooks 1, sdkWraps 1" in out
    assert "plugin quiet  disabled  builtin  (not loaded)" in out


def test_a_surface_nobody_uses_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """An unused surface still gets its block — the reference half of the
    catalog is what a plugin-authoring agent reads, and it must not shrink to
    whatever this machine happens to run."""
    _install(monkeypatch, _view(contributions=(_HOOK,)))

    plugins_inspect.cmd_plugins_inspect(None)

    out = capsys.readouterr().out
    assert "registered   1 — demo 1" in out
    assert "registered   nothing registered here" in out


def test_detail_lists_every_registered_fact(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    _install(monkeypatch, _view(contributions=(_HOOK, _STATE)))

    assert plugins_inspect.cmd_plugins_inspect("demo") == 0

    out = capsys.readouterr().out
    assert "registered contributions (2)" in out
    assert "hooks                before_llm                demo.plugin._DemoHook" in out
    assert "state                demo__counter             DemoState.counter: int" in out
    assert "(no manifest — a plugin without ava-plugin.json declares nothing)" in out


def test_detail_renders_the_diff_and_what_is_not_drift(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """Three vocabularies, deliberately distinct: a status per compared
    identifier, `undeclarable` for surfaces the spec cannot express, and
    `install-time` for declarations with no runtime registry behind them."""
    _install(
        monkeypatch,
        _view(
            contributions=(_HOOK, _WRAP, _STATE),
            manifest=_manifest(hooks=["before_llm", "after_exec"], skills=["a-skill"]),
        ),
    )

    plugins_inspect.cmd_plugins_inspect("demo")

    out = capsys.readouterr().out
    assert "  ok                       hooks/before_llm" in out
    assert "  declared-not-registered  hooks/after_exec" in out
    assert "  registered-not-declared  sdkWraps/files.read" in out
    assert "undeclarable state —" in out.replace("\n", " ")
    assert "install-time skills —" in out.replace("\n", " ")


def test_detail_of_a_disabled_plugin_explains_the_empty_list(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    _install(monkeypatch, _view("quiet", enabled=False))

    assert plugins_inspect.cmd_plugins_inspect("quiet") == 0

    out = capsys.readouterr().out
    assert "disabled and never imported" in out


def test_an_unknown_plugin_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    _install(monkeypatch, _view())

    assert plugins_inspect.cmd_plugins_inspect("nope") == 1

    captured = capsys.readouterr()
    assert "no plugin named 'nope'" in captured.err
    assert captured.out == ""
