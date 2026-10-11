"""A decision-model overlay field is checked against the catalog's decision table.

`skill_recall_model` names a decision model, which never appears in the chat table
(`catalog.models`), so the chat-model membership check cannot serve it. An id outside the
decision table would pass the type check, persist, and fail every recall pass at runtime.
"""

import pytest

from base.config import settings
from base.lm.catalog import ModelCatalog
from base.packages.plugins.config_registration import InvalidConfigOverlay, validate_config_overlay


def test_the_default_skill_recall_model_is_a_registered_decision_model(
    model_catalog: ModelCatalog,
) -> None:
    assert settings.agent.skill_recall_model in model_catalog.decision_models
    validate_config_overlay(
        {"skill_recall_model": settings.agent.skill_recall_model},
        models=model_catalog.models,
        decision_models=model_catalog.decision_models,
    )


def test_a_chat_model_is_not_a_decision_model(model_catalog: ModelCatalog) -> None:
    with pytest.raises(InvalidConfigOverlay, match="not a registered decision model") as exc:
        validate_config_overlay(
            {"skill_recall_model": "deepseek-flash"},
            models=model_catalog.models,
            decision_models=model_catalog.decision_models,
        )

    assert "skill_recall_model" in str(exc.value)
    assert settings.agent.skill_recall_model in str(exc.value)


def test_an_unknown_id_is_rejected(model_catalog: ModelCatalog) -> None:
    with pytest.raises(InvalidConfigOverlay, match="not a registered decision model"):
        validate_config_overlay(
            {"skill_recall_model": "jev-0.0"},
            models=model_catalog.models,
            decision_models=model_catalog.decision_models,
        )


def test_a_caller_without_the_decision_table_cannot_admit_the_field(
    model_catalog: ModelCatalog,
) -> None:
    """The check is not silently skipped when the table is not supplied."""
    with pytest.raises(InvalidConfigOverlay, match="no decision-model table"):
        validate_config_overlay({"skill_recall_model": "jev-1.13.0"}, models=model_catalog.models)


def test_an_overlay_without_the_field_needs_no_decision_table(model_catalog: ModelCatalog) -> None:
    validate_config_overlay({"skill_recall_enabled": False}, models=model_catalog.models)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("skill_recall_threshold", 0.0),
        ("skill_recall_threshold", 1.5),
        ("skill_recall_threshold", float("nan")),
        ("skill_recall_max", -1),
        ("skill_recall_deadline_seconds", 0.0),
        ("skill_recall_deadline_seconds", float("inf")),
    ],
)
def test_the_numeric_fields_are_range_checked(
    model_catalog: ModelCatalog, field: str, value: float
) -> None:
    with pytest.raises(InvalidConfigOverlay):
        validate_config_overlay(
            {field: value},
            models=model_catalog.models,
            decision_models=model_catalog.decision_models,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("skill_recall_enabled", False),
        ("skill_recall_threshold", 0.75),
        ("skill_recall_max", 3),
        ("skill_recall_deadline_seconds", 6.0),
    ],
)
def test_sane_values_are_admitted(model_catalog: ModelCatalog, field: str, value: object) -> None:
    validate_config_overlay(
        {field: value}, models=model_catalog.models, decision_models=model_catalog.decision_models
    )
