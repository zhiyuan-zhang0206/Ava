"""The provider face is a declaration: `provider.py` exports `contribute()`, and the loader — the one
writer of the model catalog — installs it after the manifest gate.

A provider plugin whose `ava-plugin.json` disagrees with what its `contribute()` provides on the
`providers` key (a prefix declared but not provided, or provided but not declared), that ships no
manifest, or whose `provider.py` has no usable `contribute()`, refuses the complete catalog build.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from base import paths
from base.lm import provider_api
from base.lm.catalog import provider_contract
from base.lm.plugin_providers import build_model_catalog
from base.lm.provider_api import (
    DecisionModelSpec,
    DecisionProviderContribution,
    ProviderBinding,
    ProviderContribution,
)
from base.lm.tests.providers.provider_plugin_support import provider_plugin as provider_plugin
from base.packages.plugins.extensions import PluginContributions
from base.packages.plugins.gate import ContributionMismatch, check_manifest
from base.packages.plugins.manifest import ManifestError


def _manifest(name: str, providers: list[str]) -> str:
    return json.dumps(
        {
            "apiVersion": 2,
            "name": name,
            "version": "1.0.0",
            "engines": {"ava": ">=0.1.0"},
            "contributions": {"providers": providers},
        }
    )


def test_a_manifest_that_matches_the_declared_prefix_installs_the_provider(
    provider_plugin: Callable[..., None],
) -> None:
    provider_plugin(prefix="kept-", model="kept-1", dir_name="matching_provider")
    plugin_dir = paths.plugins_dir() / "matching_provider"
    (plugin_dir / "ava-plugin.json").write_text(_manifest("matching_provider", ["kept-"]))

    catalog = build_model_catalog()

    assert "kept-1" in catalog.models


def test_a_manifest_that_disagrees_refuses_the_catalog_and_can_be_corrected(
    provider_plugin: Callable[..., None],
) -> None:
    provider_plugin(prefix="kept-", model="kept-1", dir_name="kept_provider")
    provider_plugin(prefix="gated-", model="gated-1", dir_name="gated_provider")
    gated_dir = paths.plugins_dir() / "gated_provider"
    manifest = gated_dir / "ava-plugin.json"
    manifest.write_text(_manifest("gated_provider", ["other-"]))

    with pytest.raises(ContributionMismatch, match="gated_provider"):
        build_model_catalog()

    manifest.write_text(_manifest("gated_provider", ["gated-"]))
    catalog = build_model_catalog()
    assert "kept-1" in catalog.models
    assert "gated-1" in catalog.models


def test_a_provider_plugin_without_a_manifest_refuses_the_catalog(
    provider_plugin: Callable[..., None],
) -> None:
    provider_plugin(prefix="bare-", model="bare-1", dir_name="bare_provider")
    (paths.plugins_dir() / "bare_provider" / "ava-plugin.json").unlink()

    with pytest.raises(ManifestError, match=r"bare_provider.*ava-plugin\.json not found"):
        build_model_catalog()


def test_a_provider_py_without_contribute_is_a_load_failure(
    provider_plugin: Callable[..., None],
) -> None:
    provider_plugin(prefix="kept-", model="kept-1", dir_name="kept_provider")
    legacy = paths.plugins_dir() / "legacy_provider"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "plugin.py").write_text("# provider plugin stub")
    (legacy / "provider.py").write_text("X = 1\n")

    with pytest.raises(AttributeError, match=r"legacy_provider\.provider has no contribute"):
        build_model_catalog()


def test_the_declaration_is_attributed_by_prefix() -> None:
    contribution = ProviderContribution(
        binding=ProviderBinding(
            prefix="rec-",
            display_name="Rec",
            key_env="REC_KEY",
            build=lambda _ctx: None,  # pyright: ignore[reportArgumentType]
        ),
        models={},
        pricing={},
    )

    (record,) = PluginContributions(providers=(contribution,)).as_records("rec_plugin")

    assert (record.surface, record.identifier, record.plugin) == ("providers", "rec-", "rec_plugin")


def _decision_contribution() -> PluginContributions:
    return PluginContributions(
        decision_providers=(
            DecisionProviderContribution(
                prefix="dec-",
                display_name="Dec",
                key_env="DEC_KEY",
                build=lambda _ctx: None,  # pyright: ignore[reportArgumentType]
                models={"dec-1": DecisionModelSpec(context_window=10)},
                pricing={},
            ),
        )
    )


def test_a_decision_declaration_is_attributed_by_prefix() -> None:
    (record,) = _decision_contribution().as_records("dec_plugin")

    assert (record.surface, record.identifier, record.plugin) == (
        "decisionProviders",
        "dec-",
        "dec_plugin",
    )


@pytest.mark.parametrize(("declared", "agrees"), [(["dec-"], True), (["other-"], False)])
def test_the_manifest_gates_the_decision_providers_key(
    tmp_path: Path, declared: list[str], agrees: bool
) -> None:
    manifest = {
        "apiVersion": 2,
        "name": "dec_plugin",
        "version": "1.0.0",
        "engines": {"ava": ">=0.1.0"},
        "contributions": {"decisionProviders": declared},
    }
    (tmp_path / "ava-plugin.json").write_text(json.dumps(manifest))

    if agrees:
        check_manifest("dec_plugin", tmp_path, _decision_contribution(), ("decisionProviders",))
    else:
        with pytest.raises(ContributionMismatch, match="decisionProviders"):
            check_manifest("dec_plugin", tmp_path, _decision_contribution(), ("decisionProviders",))


@pytest.mark.parametrize(
    "name",
    [
        "PROVIDER_API_VERSION",
        "AttachPolicy",
        "BuildContext",
        "DecisionBuildContext",
        "DecisionProviderContribution",
        "InferenceSpeed",
        "PricePeriod",
        "PriceRates",
        "PriceTier",
        "PriceWindow",
        "ProviderBinding",
        "ProviderContribution",
        "ProviderRegistrationError",
        "ThinkingConfig",
    ],
)
def test_provider_api_exposes_the_single_declaration_owner(name: str) -> None:
    assert getattr(provider_api, name) is getattr(provider_contract, name)
