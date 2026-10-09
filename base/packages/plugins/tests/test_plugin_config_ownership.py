"""Plugin configuration bindings and SDK installation ownership."""

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from base.lm.catalog import ModelCatalog
from base.packages.plugins import load_report
from base.packages.plugins.config_registration import (
    SchemaDriftError,
    bind_plugin_config,
    disk_image_path,
    is_per_agent_field,
    read_authority_config,
    validate_config_overlay,
)
from base.packages.plugins.tests.test_plugin_config import (
    _FixtureConfig,
)
from base.packages.plugins.tests.test_plugin_config import (
    isolated_registry as isolated_registry,
)


def test_syntax_fix_ruff_format_overlay_is_accepted(model_catalog: ModelCatalog) -> None:
    """Per-agent A/B of the ruff format gate (task #1858 follow-up, user chose
    a paired experiment): the field must accept a spawn config_overlay, like
    prompt_codeact_enabled after #719."""

    validate_config_overlay(
        {"syntax_fix_ruff_format": True}, models=model_catalog.models
    )  # must not raise
    validate_config_overlay({"syntax_fix_ruff_format": False}, models=model_catalog.models)


def test_bind_undo_drops_the_binding(isolated_registry: dict[str, BaseModel], unit_home):
    undo = bind_plugin_config("test_plugin", _FixtureConfig, configs=isolated_registry)
    assert is_per_agent_field("test_plugin", "marker", configs=isolated_registry) is True
    undo()

    assert "test_plugin" not in isolated_registry
    assert is_per_agent_field("test_plugin", "marker", configs=isolated_registry) is False
    bind_plugin_config(
        "test_plugin", _FixtureConfig, configs=isolated_registry
    )  # a rebind after the undo is legal


def test_install_refuses_a_plugin_whose_config_does_not_bind_and_installs_the_rest(
    isolated_registry: dict[str, BaseModel], unit_home, monkeypatch: pytest.MonkeyPatch
):
    """A config that cannot bind (SchemaDriftError) is a load failure of that plugin alone: it is
    rolled back whole (its earlier namespace too), reported, and absent from the returned registry,
    while the plugins around it install."""
    from types import SimpleNamespace

    import ava
    from ava.sdk_surface import install
    from base.packages.plugins.extensions import (
        ExtensionRegistry,
        PluginContributions,
        SdkNamespace,
    )

    drifted = disk_image_path("drifted")
    drifted.parent.mkdir(parents=True)
    drifted.write_text(json.dumps({"flag": True, "marker": ".git", "extra_field": 42}))

    reported: list[tuple[str, BaseException]] = []

    def _capture(name: str, exc: BaseException) -> None:
        reported.append((name, exc))

    monkeypatch.setattr(load_report, "report_plugin_load_failure", _capture)
    registry = ExtensionRegistry(
        (
            (
                "drifted",
                PluginContributions(
                    sdk_namespaces=(SdkNamespace("drifted_ns", SimpleNamespace()),),
                    config=_FixtureConfig,
                ),
            ),
            (
                "healthy",
                PluginContributions(
                    sdk_namespaces=(SdkNamespace("healthy_ns", SimpleNamespace()),),
                    config=_FixtureConfig,
                ),
            ),
        )
    )

    admitted = install.install(registry)
    try:
        assert [name for name, _ in admitted.plugins] == ["healthy"]
        assert [name for name, _ in reported if name == "drifted"] == ["drifted"]
        assert isinstance(
            next(exc for name, exc in reported if name == "drifted"), SchemaDriftError
        )
        assert not hasattr(ava, "drifted_ns")
        assert hasattr(ava, "healthy_ns")
        current = install.installed()
        assert current is not None
        assert "drifted" not in current.configs
        assert "healthy" in current.configs
    finally:
        install.uninstall()
    assert not hasattr(ava, "healthy_ns")
    assert install.installed() is None


def test_authority_path_canonicalizes_a_symlink_home_without_creating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_home = tmp_path / "uncreated-target"
    alias = tmp_path / "home-link"
    alias.symlink_to(missing_home, target_is_directory=True)
    monkeypatch.setenv("AVA_HOME", str(alias))
    path = disk_image_path("canonical-probe")
    assert path == missing_home.resolve() / "configs" / "canonical-probe" / "config.json"
    assert read_authority_config("canonical-probe", _FixtureConfig, path) == _FixtureConfig()
    assert not missing_home.exists()
