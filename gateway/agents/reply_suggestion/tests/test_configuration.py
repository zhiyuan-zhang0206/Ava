from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from base.config.domains.lm import LmSettings
from base.lm.catalog import ModelCatalog
from base.lm.effort import ReasoningEffort
from gateway.agents.reply_suggestion import configuration
from gateway.agents.reply_suggestion.configuration import reply_suggestion_service
from gateway.agents.reply_suggestion.service import validate_models
from gateway.agents.reply_suggestion.tests.support import Settings


def test_defaults() -> None:
    lm = LmSettings()

    assert lm.reply_suggestion_enabled is True
    assert lm.reply_suggestion_model == "deepseek-flash"
    assert lm.reply_suggestion_gate_model == "jev-1.13.0"
    assert lm.reply_suggestion_gate_threshold == 0.5
    assert lm.reply_suggestion_max_input_tokens == 1500


@pytest.mark.parametrize("threshold", [-0.1, 1.1])
def test_the_threshold_must_be_a_probability(threshold: float) -> None:
    with pytest.raises(ValidationError):
        LmSettings.model_validate({"AVA_REPLY_SUGGESTION_GATE_THRESHOLD": threshold})


def test_the_input_budget_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        LmSettings.model_validate({"AVA_REPLY_SUGGESTION_MAX_INPUT_TOKENS": 0})


def test_the_default_models_belong_to_their_catalogs(model_catalog: ModelCatalog) -> None:
    lm = LmSettings()
    settings = Settings()
    settings.model = lm.reply_suggestion_model
    settings.gate_model = lm.reply_suggestion_gate_model

    validate_models(settings.policy(), model_catalog)


def test_a_chat_model_cannot_be_the_gate(model_catalog: ModelCatalog) -> None:
    settings = Settings()
    settings.model = "deepseek-flash"
    settings.gate_model = "deepseek-flash"

    with pytest.raises(
        ValueError, match=r"reply_suggestion_gate_model.*not a registered decision model"
    ):
        validate_models(settings.policy(), model_catalog)


def test_a_decision_model_cannot_draft(model_catalog: ModelCatalog) -> None:
    settings = Settings()
    settings.model = "jev-1.13.0"
    settings.gate_model = "jev-1.13.0"

    with pytest.raises(ValueError, match=r"reply_suggestion_model.*not a registered chat model"):
        validate_models(settings.policy(), model_catalog)


def test_an_unknown_gate_model_is_refused(model_catalog: ModelCatalog) -> None:
    settings = Settings()
    settings.model = "deepseek-flash"
    settings.gate_model = "jev-0.0.0"

    with pytest.raises(ValueError, match=r"jev-0\.0\.0"):
        validate_models(settings.policy(), model_catalog)


def test_a_disabled_feature_does_not_check_its_models(model_catalog: ModelCatalog) -> None:
    settings = Settings()
    settings.enabled = False
    settings.model = "nope"
    settings.gate_model = "nope"

    validate_models(settings.policy(), model_catalog)


def test_the_draft_model_is_built_with_reasoning_off(
    monkeypatch: pytest.MonkeyPatch, model_catalog: ModelCatalog
) -> None:
    built: list[tuple[str, dict[str, Any]]] = []

    def fake_build(model: str, **kwargs: Any) -> object:
        built.append((model, kwargs))
        return object()

    monkeypatch.setattr(configuration, "build_chat_model", fake_build)
    lm = LmSettings()
    config = SimpleNamespace(view=SimpleNamespace(lm=lm))

    service = reply_suggestion_service(
        config,  # pyright: ignore[reportArgumentType]
        db=object(),  # pyright: ignore[reportArgumentType]
        catalog_reader=lambda: model_catalog,
        rendering=object(),  # pyright: ignore[reportArgumentType]
    )
    service._draft_model("deepseek-flash", model_catalog)

    ((model, kwargs),) = built
    assert model == "deepseek-flash"
    assert kwargs["reasoning_effort"] is ReasoningEffort.NONE
    assert kwargs["catalog"] is model_catalog
