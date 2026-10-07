"""Fast rate declarations stay inside the non-executing pricing workflow."""

from decimal import Decimal
from pathlib import Path

import pytest

from scripts.model_registry import plugin_price_sync as sync

_SOURCE = """
PROVIDER = ProviderContribution(
    binding=None,
    models={"fixture": None},
    pricing={"fixture": PriceRates(
        cache_miss=1, cache_hit=0.1, output=3,
        source_url="https://example.com/pricing",
        source_checked_at="2026-10-07", vendor="fixture",
    )},
)
def contribute():
    return PluginContributions(providers=(with_fast_variants(PROVIDER, {
        "fixture": PriceRates(
            cache_miss=2, cache_hit=0.2, output=6,
            source_url="https://example.com/pricing",
            source_checked_at="2026-10-07", vendor="fixture",
        ),
    }),))
"""


def test_fast_rates_are_parsed_and_rewritten_without_executing_plugin(tmp_path: Path) -> None:
    path = tmp_path / "provider.py"
    source = _SOURCE + '\nraise RuntimeError("plugin code must never execute")\n'
    manifest = sync._provider_manifest(source, path)
    assert manifest.models == {"fixture", "fixture-fast"}
    assert manifest.prices["fixture-fast"].rates == sync.FlatRates(
        Decimal("2"), Decimal("0.2"), Decimal("6")
    )
    fast = manifest.prices["fixture-fast"]
    rates = sync.FlatRates(Decimal("4"), Decimal("0.4"), Decimal("12"))
    tier = sync._TierDeclaration(0, None, rates, ())
    period = sync._PeriodDeclaration(None, None, (tier,))
    target = fast._replace(rates=rates, periods=(period,))
    targets = {**manifest.prices, "fixture-fast": target}
    rewritten, changed, _ = sync._rewrite_provider(source, path, manifest, targets)
    assert changed == ("fixture-fast",)
    restored = sync._provider_manifest(rewritten, path)
    assert restored.prices["fixture-fast"] == target
    assert restored.prices["fixture"] == manifest.prices["fixture"]
    assert 'raise RuntimeError("plugin code must never execute")' in rewritten


@pytest.mark.parametrize(
    "call",
    [
        "with_fast_variants(PROVIDER, dynamic_prices)",
        "with_fast_variants(OTHER, {})",
        "with_fast_variants(PROVIDER, {}, unsupported=True)",
        "with_fast_variants(PROVIDER, {}, {})",
    ],
)
def test_fast_manifest_rejects_dynamic_or_unsupported_declarations(
    tmp_path: Path, call: str
) -> None:
    source = (
        _SOURCE.split("def contribute():", maxsplit=1)[0]
        + f"def contribute():\n    return {call}\n"
    )
    with pytest.raises(RuntimeError, match="expected literal"):
        sync._provider_manifest(source, tmp_path / "provider.py")


def test_fast_manifest_rejects_multiple_declarations(tmp_path: Path) -> None:
    source = _SOURCE + "    with_fast_variants(PROVIDER, {})\n"
    with pytest.raises(RuntimeError, match="at most one"):
        sync._provider_manifest(source, tmp_path / "provider.py")


def test_fast_manifest_rejects_missing_base_model(tmp_path: Path) -> None:
    source = _SOURCE.replace(
        '"fixture": PriceRates(\n            cache_miss=2',
        '"missing": PriceRates(\n            cache_miss=2',
    )
    with pytest.raises(RuntimeError, match="invalid or duplicate Fast model"):
        sync._provider_manifest(source, tmp_path / "provider.py")


def test_cache_write_dimensions_survive_sync_at_every_rate_level(tmp_path: Path) -> None:
    path = tmp_path / "provider.py"
    manifest = sync._provider_manifest(_SOURCE, path)
    fast = manifest.prices["fixture-fast"]
    rates = sync.FlatRates(Decimal("4"), Decimal("0.4"), Decimal("12"), Decimal("5"), Decimal("8"))
    window = sync._WindowDeclaration("01:00:00", "03:00:00", rates)
    tier = sync._TierDeclaration(0, None, rates, (window,))
    period = sync._PeriodDeclaration(None, None, (tier,))
    target = fast._replace(rates=rates, periods=(period,))
    rewritten, changed, _ = sync._rewrite_provider(
        _SOURCE, path, manifest, {**manifest.prices, "fixture-fast": target}
    )
    assert changed == ("fixture-fast",)
    assert sync._provider_manifest(rewritten, path).prices["fixture-fast"] == target
    assert "cache_write_5m=5," in rewritten
    assert 'cache_write_1h="8",' in rewritten


@pytest.mark.parametrize("value", ["-1", 'float("nan")', "True"])
def test_cache_write_source_rejects_invalid_or_executing_rates(tmp_path: Path, value: str) -> None:
    source = _SOURCE.replace("cache_miss=2,", f"cache_miss=2, cache_write_5m={value},")
    with pytest.raises((RuntimeError, TypeError)):
        sync._provider_manifest(source, tmp_path / "provider.py")
