from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from base.lm.catalog import ModelCatalog
from base.lm.stop import StopCategory, classify_stop


def _msg(metadata: dict) -> AIMessage:
    return AIMessage(content="x", response_metadata=metadata)


def test_anthropic_end_turn_normal(model_catalog: ModelCatalog):
    cat, raw = classify_stop(
        _msg({"model_provider": "anthropic", "stop_reason": "end_turn"}), stops=model_catalog.stops
    )
    assert cat is StopCategory.NORMAL and raw == "end_turn"


def test_anthropic_max_tokens_truncated(model_catalog: ModelCatalog):
    cat, _ = classify_stop(
        _msg({"model_provider": "anthropic", "stop_reason": "max_tokens"}),
        stops=model_catalog.stops,
    )
    assert cat is StopCategory.TRUNCATED


def test_anthropic_missing_corrupted(model_catalog: ModelCatalog):
    cat, _ = classify_stop(_msg({"model_provider": "anthropic"}), stops=model_catalog.stops)
    assert cat is StopCategory.CORRUPTED


def test_openai_stop_normal(model_catalog: ModelCatalog):
    cat, raw = classify_stop(
        _msg({"model_provider": "openai", "finish_reason": "stop"}), stops=model_catalog.stops
    )
    assert cat is StopCategory.NORMAL and raw == "stop"


def test_openai_length_truncated(model_catalog: ModelCatalog):
    cat, _ = classify_stop(
        _msg({"model_provider": "openai", "finish_reason": "length"}), stops=model_catalog.stops
    )
    assert cat is StopCategory.TRUNCATED


def test_openai_content_filter_unexpected(model_catalog: ModelCatalog):
    cat, _ = classify_stop(
        _msg({"model_provider": "openai", "finish_reason": "content_filter"}),
        stops=model_catalog.stops,
    )
    assert cat is StopCategory.UNEXPECTED


def test_openai_responses_status_completed_normal(model_catalog: ModelCatalog):
    """Responses API (use_responses_api=True) returns status='completed' instead of finish_reason."""
    cat, raw = classify_stop(
        _msg({"model_provider": "openai", "status": "completed"}), stops=model_catalog.stops
    )
    assert cat is StopCategory.NORMAL and raw == "completed"


def test_openai_responses_status_incomplete_truncated(model_catalog: ModelCatalog):
    cat, _ = classify_stop(
        _msg({"model_provider": "openai", "status": "incomplete"}), stops=model_catalog.stops
    )
    assert cat is StopCategory.TRUNCATED


def test_openai_finish_reason_precedes_responses_status(model_catalog: ModelCatalog):
    cat, raw = classify_stop(
        _msg(
            {
                "model_provider": "openai",
                "finish_reason": "length",
                "status": "completed",
            }
        ),
        stops=model_catalog.stops,
    )
    assert cat is StopCategory.TRUNCATED and raw == "length"


def test_openai_responses_status_unknown_corrupted(model_catalog: ModelCatalog):
    cat, raw = classify_stop(
        _msg({"model_provider": "openai", "status": "failed"}), stops=model_catalog.stops
    )
    assert cat is StopCategory.CORRUPTED and raw is None


def test_openai_responses_no_status_no_finish_reason_corrupted(model_catalog: ModelCatalog):
    cat, raw = classify_stop(_msg({"model_provider": "openai"}), stops=model_catalog.stops)
    assert cat is StopCategory.CORRUPTED and raw is None


def test_anthropic_ignores_openai_responses_status(model_catalog: ModelCatalog):
    cat, raw = classify_stop(
        _msg(
            {
                "model_provider": "anthropic",
                "stop_reason": "end_turn",
                "status": "incomplete",
            }
        ),
        stops=model_catalog.stops,
    )
    assert cat is StopCategory.NORMAL and raw == "end_turn"


def test_core_stop_classifier_has_no_openai_provider_branch():
    source = (Path(__file__).resolve().parents[4] / "base/lm/stop.py").read_text()
    assert 'provider == "openai"' not in source


def test_gemini_stop_normal(model_catalog: ModelCatalog):
    cat, raw = classify_stop(
        _msg({"model_provider": "google_genai", "finish_reason": "STOP"}), stops=model_catalog.stops
    )
    assert cat is StopCategory.NORMAL and raw == "STOP"


def test_gemini_max_tokens_truncated(model_catalog: ModelCatalog):
    cat, _ = classify_stop(
        _msg({"model_provider": "google_genai", "finish_reason": "MAX_TOKENS"}),
        stops=model_catalog.stops,
    )
    assert cat is StopCategory.TRUNCATED


def test_gemini_safety_unexpected(model_catalog: ModelCatalog):
    cat, _ = classify_stop(
        _msg({"model_provider": "google_genai", "finish_reason": "SAFETY"}),
        stops=model_catalog.stops,
    )
    assert cat is StopCategory.UNEXPECTED


def test_gemini_missing_corrupted(model_catalog: ModelCatalog):
    cat, _ = classify_stop(_msg({"model_provider": "google_genai"}), stops=model_catalog.stops)
    assert cat is StopCategory.CORRUPTED


def test_moonshot_stop_normal(model_catalog: ModelCatalog):
    cat, raw = classify_stop(
        _msg({"model_provider": "moonshot", "finish_reason": "stop"}), stops=model_catalog.stops
    )
    assert cat is StopCategory.NORMAL and raw == "stop"


def test_moonshot_tool_calls_normal(model_catalog: ModelCatalog):
    cat, _ = classify_stop(
        _msg({"model_provider": "moonshot", "finish_reason": "tool_calls"}),
        stops=model_catalog.stops,
    )
    assert cat is StopCategory.NORMAL


def test_moonshot_length_truncated(model_catalog: ModelCatalog):
    cat, _ = classify_stop(
        _msg({"model_provider": "moonshot", "finish_reason": "length"}), stops=model_catalog.stops
    )
    assert cat is StopCategory.TRUNCATED


def test_unknown_provider_raises(model_catalog: ModelCatalog):
    with pytest.raises(ValueError, match=r"ProviderBinding\.stop_spec"):
        classify_stop(
            _msg({"model_provider": "cohere", "finish_reason": "stop"}), stops=model_catalog.stops
        )


def test_missing_provider_raises(model_catalog: ModelCatalog):
    with pytest.raises(ValueError, match="unknown model_provider"):
        classify_stop(_msg({"stop_reason": "end_turn"}), stops=model_catalog.stops)
