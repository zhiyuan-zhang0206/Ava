"""The provider face is a declaration: `provider.py` exports `contribute()`, and the loader — the one
writer of the model catalog — installs it after the manifest gate.

A provider plugin whose `ava-plugin.json` disagrees with what its `contribute()` provides on the
`providers` key (a prefix declared but not provided, or provided but not declared), or whose
`provider.py` has no usable `contribute()`, is a load failure of that plugin: reported, skipped, the
other providers still install.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from base import paths
from base.lm.plugin_providers import model_catalog
from base.lm.provider_api import ProviderBinding, ProviderContribution
from base.lm.tests.provider_plugin_support import provider_plugin as provider_plugin
from base.packages.plugins.extensions import PluginContributions


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

    model_catalog()

    assert "kept-1" in model_catalog().models


def test_a_manifest_that_disagrees_refuses_that_provider_and_the_rest_load(
    provider_plugin: Callable[..., None], loguru_records: list[dict[str, Any]]
) -> None:
    provider_plugin(prefix="kept-", model="kept-1", dir_name="kept_provider")
    provider_plugin(prefix="gated-", model="gated-1", dir_name="gated_provider")
    gated_dir = paths.plugins_dir() / "gated_provider"
    (gated_dir / "ava-plugin.json").write_text(_manifest("gated_provider", ["other-"]))

    model_catalog()  # a gate failure is fail-soft: must not raise

    assert "kept-1" in model_catalog().models
    assert "gated-1" not in model_catalog().models
    assert any("gated_provider" in r["message"] for r in loguru_records)


def test_a_provider_py_without_contribute_is_a_load_failure(
    provider_plugin: Callable[..., None], loguru_records: list[dict[str, Any]]
) -> None:
    provider_plugin(prefix="kept-", model="kept-1", dir_name="kept_provider")
    legacy = paths.plugins_dir() / "legacy_provider"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "plugin.py").write_text("# provider plugin stub")
    (legacy / "provider.py").write_text("X = 1\n")

    model_catalog()

    assert "kept-1" in model_catalog().models
    assert any("legacy_provider" in r["message"] for r in loguru_records)


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
