"""Fast IDs preserve selection, vendor requests and actual-service accounting."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage, UsageMetadata
from langchain_openai import ChatOpenAI

from base.config import get_field, settings
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.compat.anthropic_thinking import ThinkingTokensChatAnthropic
from base.lm.factory import build_chat_model
from base.lm.pricing import Rates, rates_at, tally_tokens
from base.lm.reasoning import extract_reasoning_tokens
from base.lm.registry import ModelSpec, validate_models
from base.lm.usage import log_usage_from_message, usage_model


def test_additive_service_fields_preserve_plugin_positional_constructor() -> None:
    spec = ModelSpec("fixture", True, context_window=100, fast_of="fixture-standard")
    assert spec.provider == "fixture"
    assert spec.spawnable is True
    assert spec.fast_of == "fixture-standard"
    assert spec.reference_tps is None


@pytest.mark.parametrize("standard", ["gpt-6.1-sol", "claude-opus-5-5"])
def test_fast_picker_entry_inherits_facts_and_effort(
    standard: str, *, model_catalog: ModelCatalog
) -> None:
    catalog = model_catalog
    fast_id = f"{standard}-fast"
    base = catalog.models[standard]
    fast = catalog.models[fast_id]
    assert fast.fast_of == standard
    assert fast.context_window == base.context_window
    assert fast.effort_levels == base.effort_levels
    assert fast.tuning == base.tuning
    assert fast.media_types == base.media_types
    assert standard in catalog.supported_models[base.provider]
    assert fast_id in catalog.supported_models[base.provider]
    assert base.fast_of is None


@pytest.mark.parametrize("fast", [False, True])
def test_openai_wire_model_and_service_tier(
    monkeypatch: pytest.MonkeyPatch, fast: bool, *, model_catalog: ModelCatalog
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    model = build_chat_model(
        "gpt-6.1-sol-fast" if fast else "gpt-6.1-sol",
        catalog=model_catalog,
        llm_override=settings.lm.llm_override,
        overrides=ModelOverrides.from_pins(
            {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
        ),
    )
    assert isinstance(model, ChatOpenAI)
    # The pinned SDK leaves this private payload method's dict unparameterized.
    payload = model._get_request_payload([HumanMessage(content="Hello")])  # pyright: ignore[reportUnknownMemberType]
    assert payload["model"] == "gpt-6.1-sol"
    assert payload["service_tier"] == ("fast" if fast else "default")
    assert payload["reasoning"]["effort"] == "medium"


@pytest.mark.parametrize("fast", [False, True])
def test_anthropic_wire_model_speed_and_beta(
    monkeypatch: pytest.MonkeyPatch, fast: bool, *, model_catalog: ModelCatalog
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    model = build_chat_model(
        "claude-opus-5-5-fast" if fast else "claude-opus-5-5",
        catalog=model_catalog,
        llm_override=settings.lm.llm_override,
        overrides=ModelOverrides.from_pins(
            {name: get_field(name) for name in ModelOverrides.__dataclass_fields__}
        ),
    )
    assert isinstance(model, ThinkingTokensChatAnthropic)
    # Exercise the SDK's actual wire conversion despite its incomplete typing.
    payload = model._get_request_payload([HumanMessage(content="Hello")])  # pyright: ignore[reportUnknownMemberType]
    assert payload["model"] == "claude-opus-5-5"
    assert payload["output_config"]["effort"] == "medium"
    if fast:
        assert payload["speed"] == "fast"
        assert "fast-mode-2026-02-01" in payload["betas"]
    else:
        assert "speed" not in payload


@pytest.mark.parametrize(
    ("tier", "expected"),
    [("fast", "gpt-6.1-sol-fast"), ("priority", "gpt-6.1-sol-fast"), ("default", "gpt-6.1-sol")],
)
def test_openai_actual_service_controls_usage_and_price(
    tier: str, expected: str, *, model_catalog: ModelCatalog
) -> None:
    message = AIMessage(
        content="answer",
        response_metadata={"service_tier": tier, "model_name": "gpt-6.1-sol"},
        usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
    )
    assert usage_model(message, "gpt-6.1-sol-fast", catalog=model_catalog) == expected
    result = log_usage_from_message(message, "gpt-6.1-sol-fast", catalog=model_catalog)
    assert result is not None
    assert result[1] == pytest.approx(0.0003 if tier == "default" else 0.0006)


@pytest.mark.parametrize("speed", ["fast", "standard"])
def test_anthropic_actual_service_controls_usage(
    speed: str, *, model_catalog: ModelCatalog
) -> None:
    message = AIMessage(content="answer", response_metadata={"speed": speed})
    expected = "claude-opus-5-5-fast" if speed == "fast" else "claude-opus-5-5"
    assert usage_model(message, "claude-opus-5-5-fast", catalog=model_catalog) == expected


@pytest.mark.parametrize("receipt", [{}, {"service_tier": "unexpected"}])
def test_fast_accounting_rejects_missing_or_unknown_receipt(
    receipt: dict[str, str], *, model_catalog: ModelCatalog
) -> None:
    with pytest.raises(ValueError):
        usage_model(
            AIMessage(content="answer", response_metadata=receipt),
            "gpt-6.1-sol-fast",
            catalog=model_catalog,
        )


def test_priority_token_details_keep_cache_discount_and_reasoning() -> None:
    message = AIMessage(
        content="answer",
        usage_metadata=cast(
            UsageMetadata,
            {
                "input_tokens": 100,
                "output_tokens": 40,
                "total_tokens": 140,
                "input_token_details": {"priority_cache_read": 80},
                "output_token_details": {"priority_reasoning": 30},
            },
        ),
    )
    assert tally_tokens([message]) == (100, 40, 80)
    assert extract_reasoning_tokens(message.usage_metadata) == 30


def test_fast_prices_use_full_request_long_context_tier(*, model_catalog: ModelCatalog) -> None:
    now = datetime(2026, 10, 7, 12, tzinfo=UTC)
    assert rates_at(
        "gpt-6.1-sol-fast", at=now, input_tokens=272_000, prices=model_catalog.prices
    ) == Rates(4, 0.2, 20)
    assert rates_at(
        "gpt-6.1-sol-fast", at=now, input_tokens=272_001, prices=model_catalog.prices
    ) == Rates(8, 0.4, 30)
    assert rates_at(
        "claude-opus-5-5-fast", at=now, input_tokens=900_000, prices=model_catalog.prices
    ) == Rates(8, 0.4, 40, cache_write_5m=10, cache_write_1h=16)


@pytest.mark.parametrize("target", ["unknown", "gpt-6.1-sol-fast", "claude-opus-5-5"])
def test_fast_links_reject_missing_nested_or_other_provider(
    target: str, *, model_catalog: ModelCatalog
) -> None:
    catalog = model_catalog
    models = dict(catalog.models)
    models["gpt-6.1-sol-fast"] = replace(models["gpt-6.1-sol-fast"], fast_of=target)
    with pytest.raises(RuntimeError, match="must name a registered"):
        validate_models(models, prices=catalog.prices)
