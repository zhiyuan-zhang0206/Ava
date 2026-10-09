"""Fixture plugin writer for the provider-plugin tests: a `provider.py` + `plugin.py` pair in the
session's tmp AVA_HOME. Each test explicitly builds its own catalog after writing its plugins."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Generator

import pytest

from base import paths

_PLUGIN_SOURCE = """from langchain_core.language_models.fake_chat_models import FakeListChatModel

from base.lm.provider_api import PriceRates, ProviderBinding, ProviderContribution
from base.packages.plugins.extensions import PluginContributions
from base.lm.registry import ModelSpec, ModelTuning
from base.lm.stop import StopSpec

def _build(ctx):
    return FakeListChatModel(responses=["hello"])

PROVIDER = ProviderContribution(
    binding=ProviderBinding(
        prefix="{prefix}",
        display_name="{display}",
        key_env="{key_env}",
        build=_build,
        vision={vision},
        stop_spec={stop_spec},
    ),
    models={{
        {models}
    }},
    pricing={{
        {pricing}
    }},
)


def contribute():
    return PluginContributions(providers=(PROVIDER,))
"""

_MODEL_LINE = """\"{model}\": ModelSpec(
            provider={provider!r},
            spawnable=True,
            context_window=200_000,
            knowledge_cutoff="2026-01",
            effort_levels=("low", "high"),
            tuning=ModelTuning(reasoning_effort="high"),
            media_types={model_media_types},
            superseded_by={superseded_by!r},
        )"""

_PRICE_LINE = """\"{model}\": PriceRates(
            cache_miss=1.0, cache_hit=0.1, output=3.0,
            source_url="https://example.com/pricing",
            source_checked_at="2026-08-22",
            vendor={vendor!r},
        )"""


@pytest.fixture
def provider_plugin() -> Generator[Callable[..., None], None, None]:
    """Write fixture provider declarations for an explicit catalog build."""
    # Tests share one session AVA_HOME — remove anything this test created so
    # a later test's loader scan cannot see leftover plugin dirs.
    plugin_root = paths.plugins_dir()
    plugin_root.mkdir(parents=True, exist_ok=True)
    original = set(plugin_root.iterdir())

    def _write(
        prefix: str = "testp-",
        display: str = "TestProvider",
        key_env: str = "TESTP_API_KEY",
        vision: bool = False,
        model_vision: bool = False,
        stop_spec: str | None = None,
        model: str | None = "testp-1",
        with_price: bool = True,
        price_vendor: str | None = None,
        superseded_by: str | None = None,
        dir_name: str = "test_provider",
    ) -> None:
        plugin_dir = paths.plugins_dir() / dir_name
        plugin_dir.mkdir(parents=True, exist_ok=True)
        models = (
            _MODEL_LINE.format(
                model=model,
                provider=prefix.rstrip("-"),
                model_media_types='frozenset({"image"})' if model_vision else "frozenset()",
                superseded_by=superseded_by,
            )
            if model
            else ""
        )
        pricing_line = (
            _PRICE_LINE.format(model=model, vendor=price_vendor) if model and with_price else ""
        )
        source = _PLUGIN_SOURCE.format(
            prefix=prefix,
            display=display,
            key_env=key_env,
            vision="True" if vision else "False",
            stop_spec=stop_spec if stop_spec is not None else "None",
            models=models,
            pricing=pricing_line,
        )
        (plugin_dir / "provider.py").write_text(source)
        # Discovery is keyed on plugin.py — a provider plugin ships one (an
        # empty stub here; it contributes nothing agent-side).
        (plugin_dir / "plugin.py").write_text("# provider plugin stub")

    try:
        yield _write
    finally:
        for directory in set(plugin_root.iterdir()) - original:
            shutil.rmtree(directory)
        cfg = paths.ava_home() / "plugins_config.json"
        cfg.unlink(missing_ok=True)
