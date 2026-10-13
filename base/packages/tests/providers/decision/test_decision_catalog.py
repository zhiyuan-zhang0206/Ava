"""Decision models are installed beside, never into, the chat catalog."""

from __future__ import annotations

import pytest

from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import CatalogBuilder, ModelCatalog
from base.lm.catalog.decide.types import DecisionModelSpec, UnknownDecisionModelError, YesNo
from base.lm.decision.call import decide
from base.lm.decision.tests.support import MODEL, FakeBinding, provider
from base.lm.factory import build_chat_model, validate_model_config
from base.lm.provider_api import (
    PriceRates,
    ProviderBinding,
    ProviderContribution,
    ProviderRegistrationError,
)
from base.lm.registry import ModelSpec
from base.packages.plugin_providers import build_model_catalog


def _chat_contribution(prefix: str, model_id: str) -> ProviderContribution:
    return ProviderContribution(
        binding=ProviderBinding(
            prefix=prefix,
            display_name="Chat",
            key_env="CHAT_API_KEY",
            build=lambda _ctx: pytest.fail("not built"),
        ),
        models={model_id: ModelSpec(provider=prefix.rstrip("-"), spawnable=False)},
        pricing={
            model_id: PriceRates(
                cache_miss=1.0,
                cache_hit=0.1,
                output=3.0,
                source_url="https://example.com/pricing",
                source_checked_at="2026-10-11",
                vendor="chatvendor",
            )
        },
    )


def _catalog() -> ModelCatalog:
    builder = CatalogBuilder()
    builder.install("chat_plugin", _chat_contribution("chat-", "chat-1"))
    builder.install_decision("fake_plugin", provider(FakeBinding({})))
    return builder.build()


def test_decision_ids_never_enter_the_chat_tables() -> None:
    catalog = _catalog()

    assert MODEL in catalog.decision_models
    assert MODEL not in catalog.models
    assert MODEL not in catalog.prices.archive
    assert MODEL in catalog.prices.plugin
    assert "fake" not in catalog.supported_models
    assert catalog.decision_provider_of(MODEL) is not None
    assert catalog.decision_provider_of("chat-1") is None


def test_the_chat_path_rejects_a_decision_id() -> None:
    catalog = _catalog()

    with pytest.raises(ValueError, match="decision model"):
        validate_model_config(
            model=MODEL, catalog=catalog, check_provider_key=False, llm_override=""
        )
    with pytest.raises(ValueError, match="decision model"):
        build_chat_model(
            MODEL,
            catalog=catalog,
            llm_override="",
            overrides=ModelOverrides.from_pins({}),
        )


async def test_the_decision_path_rejects_a_chat_id() -> None:
    catalog = _catalog()

    with pytest.raises(UnknownDecisionModelError, match="it is a chat model"):
        await decide(
            "chat-1",
            "hello",
            {"q": YesNo("Is it?", "yes", "no")},
            catalog=catalog,
            usage_kind="test",
        )


@pytest.mark.parametrize(
    ("chat_prefix", "decision_prefix"),
    [("same-", "same-"), ("same-", "same-deep-"), ("same-deep-", "same-")],
)
def test_prefix_collisions_across_plugins_are_rejected_in_both_orders(
    chat_prefix: str, decision_prefix: str
) -> None:
    chat_first = CatalogBuilder()
    chat_first.install("chat_plugin", _chat_contribution(chat_prefix, f"{chat_prefix}1"))
    with pytest.raises(ProviderRegistrationError, match="share one prefix namespace"):
        chat_first.install_decision(
            "other_plugin",
            provider(
                FakeBinding({}),
                prefix=decision_prefix,
                models={f"{decision_prefix}1": DecisionModelSpec(context_window=10)},
            ),
        )

    decision_first = CatalogBuilder()
    decision_first.install_decision(
        "other_plugin",
        provider(
            FakeBinding({}),
            prefix=decision_prefix,
            models={f"{decision_prefix}1": DecisionModelSpec(context_window=10)},
        ),
    )
    with pytest.raises(ProviderRegistrationError, match="share one prefix namespace"):
        decision_first.install("chat_plugin", _chat_contribution(chat_prefix, f"{chat_prefix}1"))


def test_one_plugin_may_hold_a_chat_and_a_decision_provider_under_one_prefix() -> None:
    builder = CatalogBuilder()
    builder.install("vendor", _chat_contribution("same-", "same-1"))
    builder.install_decision(
        "vendor",
        provider(
            FakeBinding({}),
            prefix="same-",
            models={"same-1-decisions": DecisionModelSpec(context_window=10)},
        ),
    )
    catalog = builder.build()

    assert "same-1" in catalog.models
    assert "same-1-decisions" in catalog.decision_models
    assert "same-1-decisions" not in catalog.models


def test_decision_prefixes_collide_with_each_other() -> None:
    builder = CatalogBuilder()
    builder.install_decision(
        "one",
        provider(
            FakeBinding({}), prefix="dup-", models={"dup-1": DecisionModelSpec(context_window=10)}
        ),
    )
    with pytest.raises(ProviderRegistrationError, match="already claimed"):
        builder.install_decision(
            "two",
            provider(
                FakeBinding({}),
                prefix="dup-",
                models={"dup-2": DecisionModelSpec(context_window=10)},
            ),
        )


def test_a_decision_id_may_not_reuse_a_chat_id_in_either_order() -> None:
    chat_first = CatalogBuilder()
    chat_first.install("vendor", _chat_contribution("same-", "same-1"))
    with pytest.raises(ProviderRegistrationError, match="also a chat model id"):
        chat_first.install_decision(
            "vendor",
            provider(
                FakeBinding({}),
                prefix="same-",
                models={"same-1": DecisionModelSpec(context_window=10)},
            ),
        )

    decision_first = CatalogBuilder()
    decision_first.install_decision(
        "vendor",
        provider(
            FakeBinding({}),
            prefix="same-",
            models={"same-1": DecisionModelSpec(context_window=10)},
        ),
    )
    with pytest.raises(ProviderRegistrationError, match="already a decision model id"):
        decision_first.install("vendor", _chat_contribution("same-", "same-1"))


def test_every_decision_model_needs_a_price_and_the_prefix() -> None:
    builder = CatalogBuilder()
    unpriced = provider(FakeBinding({}))
    with pytest.raises(ProviderRegistrationError, match="exactly one price"):
        builder.install_decision(
            "p",
            type(unpriced)(
                prefix=unpriced.prefix,
                display_name=unpriced.display_name,
                key_env=unpriced.key_env,
                build=unpriced.build,
                models=unpriced.models,
                pricing={},
            ),
        )
    with pytest.raises(ProviderRegistrationError, match="must start with"):
        builder.install_decision(
            "p",
            provider(
                FakeBinding({}),
                prefix="fake-",
                models={"other-1": DecisionModelSpec(context_window=10)},
            ),
        )


def test_repo_catalog_registers_both_decision_providers() -> None:
    catalog = build_model_catalog()

    assert set(catalog.decision_models) == {"jev-1.13.0", "gpt-6-luna-decisions"}
    assert {p.key_env for p in catalog.decision_providers.values()} == {
        "TYPESAFE_API_KEY",
        "OPENAI_API_KEY",
    }
    assert catalog.decision_models["jev-1.13.0"].context_window == 32_000
    assert catalog.decision_models["jev-1.13.0"].request_limit == 64_000
    assert catalog.decision_models["jev-1.13.0"].media_types == frozenset()
    assert catalog.decision_models["gpt-6-luna-decisions"].media_types == frozenset({"image"})
    assert not {"jev-1.13.0", "gpt-6-luna-decisions"} & set(catalog.models)
    assert "jev-1.13.0" not in str(catalog.supported_models)


@pytest.mark.parametrize(("model", "rate"), [("jev-1.13.0", 0.042), ("gpt-6-luna-decisions", 0.10)])
def test_repo_decision_prices_are_input_only_with_provenance(model: str, rate: float) -> None:
    catalog = build_model_catalog()

    rates = catalog.prices.rates_at(model, None, 1_000)  # pyright: ignore[reportArgumentType]
    assert rates is not None
    assert rates.as_tuple() == (rate, rate, 0.0)
    provenance = catalog.prices.provenance(model)
    assert provenance is not None
    assert provenance[1] == "2026-10-11"
