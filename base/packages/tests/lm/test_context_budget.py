from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from base.config.tests.configuration import model_overrides
from base.lm.catalog import ModelCatalog
from base.lm.context_budget import (
    UnknownModelWindowError,
    latest_input_tokens,
    resolve_context_budget,
)


def test_budget_is_thirty_forty_percent_of_a_1m_window(*, model_catalog: ModelCatalog) -> None:
    budget = resolve_context_budget(
        "claude-sonnet-5",
        catalog=model_catalog,
        overrides=model_overrides(),
    )
    assert budget.max_context_tokens == 1_000_000
    assert budget.hard_compact_tokens == 400_000
    assert budget.soft_compact_tokens == 300_000


def test_deepseek_budget_is_374k_soft_512k_hard(*, model_catalog: ModelCatalog) -> None:
    # ava:ref base/packages/tests/lm/docs/contracts.ava.okf.md#scaled-budget
    budget = resolve_context_budget(
        "deepseek-flash",
        catalog=model_catalog,
        overrides=model_overrides(),
    )
    assert budget.max_context_tokens == 1_000_000
    assert budget.soft_compact_tokens == 374_000
    assert budget.hard_compact_tokens == 512_000


def test_budget_scales_to_a_smaller_window(*, model_catalog: ModelCatalog) -> None:
    budget = resolve_context_budget(
        "claude-haiku-4-5-20251001",
        catalog=model_catalog,
        overrides=model_overrides(),
    )
    assert budget.max_context_tokens == 200_000
    assert budget.hard_compact_tokens == 80_000
    assert budget.soft_compact_tokens == 60_000


def test_every_non_deepseek_spawnable_model_runs_the_flat_thirty_forty_rule(
    *, model_catalog: ModelCatalog
) -> None:
    for models in model_catalog.supported_models.values():
        for model in models:
            if model == "deepseek-flash":
                continue
            budget = resolve_context_budget(
                model,
                catalog=model_catalog,
                overrides=model_overrides(),
            )
            window = budget.max_context_tokens
            assert budget.soft_compact_tokens == round(0.3 * window), model
            assert budget.hard_compact_tokens == round(0.4 * window), model


def test_fractions_are_configurable(*, model_catalog: ModelCatalog) -> None:
    budget = resolve_context_budget(
        "deepseek-flash",
        catalog=model_catalog,
        overrides=model_overrides(auto_compact_fraction=0.5, compact_reminder_fraction=0.25),
    )
    assert budget.hard_compact_tokens == 500_000
    assert budget.soft_compact_tokens == 250_000


def test_ceiling_caps_the_hard_threshold(*, model_catalog: ModelCatalog) -> None:
    # ava:ref base/packages/tests/lm/docs/contracts.ava.okf.md#scaled-budget
    budget = resolve_context_budget(
        "deepseek-flash",
        catalog=model_catalog,
        overrides=model_overrides(
            auto_compact_fraction=0.8,
            compact_reminder_fraction=0.6,
            auto_compact_ceiling_tokens=150_000,
        ),
    )
    assert budget.hard_compact_tokens == 150_000
    assert budget.soft_compact_tokens == 112_500


def test_ceiling_above_the_fraction_is_inert(*, model_catalog: ModelCatalog) -> None:
    capped = resolve_context_budget(
        "deepseek-flash",
        catalog=model_catalog,
        overrides=model_overrides(
            auto_compact_fraction=0.8,
            compact_reminder_fraction=0.6,
            auto_compact_ceiling_tokens=900_000,
        ),
    )
    uncapped = resolve_context_budget(
        "deepseek-flash",
        catalog=model_catalog,
        overrides=model_overrides(
            auto_compact_fraction=0.8, compact_reminder_fraction=0.6, auto_compact_ceiling_tokens=0
        ),
    )
    assert capped == uncapped
    assert uncapped.hard_compact_tokens == 800_000
    assert uncapped.soft_compact_tokens == 600_000


def test_soft_stays_below_hard_for_every_spawnable_model(*, model_catalog: ModelCatalog) -> None:
    for models in model_catalog.supported_models.values():
        for model in models:
            budget = resolve_context_budget(
                model,
                catalog=model_catalog,
                overrides=model_overrides(),
            )
            assert 0 < budget.soft_compact_tokens < budget.hard_compact_tokens, model
            assert budget.hard_compact_tokens <= budget.max_context_tokens, model


def test_unknown_model_raises(*, model_catalog: ModelCatalog) -> None:
    with pytest.raises(UnknownModelWindowError, match="no-such-model"):
        resolve_context_budget(
            "no-such-model",
            catalog=model_catalog,
            overrides=model_overrides(),
        )


def test_every_supported_model_resolves(*, model_catalog: ModelCatalog) -> None:
    for models in model_catalog.supported_models.values():
        for model in models:
            assert model in model_catalog.context_windows, (
                f"{model} is spawnable but missing from build_model_catalog().context_windows — "
                f"add it so its compaction thresholds can be derived"
            )
            resolve_context_budget(
                model,
                catalog=model_catalog,
                overrides=model_overrides(),
            )


def test_latest_input_tokens_reads_most_recent_usage() -> None:
    messages = [
        SystemMessage(content="sys"),
        AIMessage(
            content="a",
            usage_metadata={"input_tokens": 100, "output_tokens": 5, "total_tokens": 105},
        ),
        HumanMessage(content="hi"),
        AIMessage(
            content="b",
            usage_metadata={"input_tokens": 250, "output_tokens": 9, "total_tokens": 259},
        ),
    ]
    assert latest_input_tokens(messages) == 250


def test_latest_input_tokens_none_before_first_call() -> None:
    messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]
    assert latest_input_tokens(messages) is None
    assert latest_input_tokens([AIMessage(content="no usage")]) is None
