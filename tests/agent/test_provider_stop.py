from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from shared.lm._plugin_providers import ensure_provider_plugins_loaded
from shared.lm.stop import StopCategory, classify_stop

ensure_provider_plugins_loaded()


def _msg(metadata: dict) -> AIMessage:
    return AIMessage(content="x", response_metadata=metadata)


def test_anthropic_end_turn_normal():
    cat, raw = classify_stop(_msg({"model_provider": "anthropic", "stop_reason": "end_turn"}))
    assert cat is StopCategory.NORMAL and raw == "end_turn"


def test_anthropic_max_tokens_truncated():
    cat, _ = classify_stop(_msg({"model_provider": "anthropic", "stop_reason": "max_tokens"}))
    assert cat is StopCategory.TRUNCATED


def test_anthropic_missing_corrupted():
    cat, _ = classify_stop(_msg({"model_provider": "anthropic"}))
    assert cat is StopCategory.CORRUPTED


def test_openai_stop_normal():
    cat, raw = classify_stop(_msg({"model_provider": "openai", "finish_reason": "stop"}))
    assert cat is StopCategory.NORMAL and raw == "stop"


def test_openai_length_truncated():
    cat, _ = classify_stop(_msg({"model_provider": "openai", "finish_reason": "length"}))
    assert cat is StopCategory.TRUNCATED


def test_openai_content_filter_unexpected():
    cat, _ = classify_stop(_msg({"model_provider": "openai", "finish_reason": "content_filter"}))
    assert cat is StopCategory.UNEXPECTED


def test_openai_responses_status_completed_normal():
    """Responses API (use_responses_api=True) returns status='completed' instead of finish_reason."""
    cat, raw = classify_stop(_msg({"model_provider": "openai", "status": "completed"}))
    assert cat is StopCategory.NORMAL and raw == "completed"


def test_openai_responses_status_incomplete_truncated():
    cat, _ = classify_stop(_msg({"model_provider": "openai", "status": "incomplete"}))
    assert cat is StopCategory.TRUNCATED


def test_openai_finish_reason_precedes_responses_status():
    cat, raw = classify_stop(
        _msg(
            {
                "model_provider": "openai",
                "finish_reason": "length",
                "status": "completed",
            }
        )
    )
    assert cat is StopCategory.TRUNCATED and raw == "length"


def test_openai_responses_status_unknown_corrupted():
    cat, raw = classify_stop(_msg({"model_provider": "openai", "status": "failed"}))
    assert cat is StopCategory.CORRUPTED and raw is None


def test_openai_responses_no_status_no_finish_reason_corrupted():
    cat, raw = classify_stop(_msg({"model_provider": "openai"}))
    assert cat is StopCategory.CORRUPTED and raw is None


def test_anthropic_ignores_openai_responses_status():
    cat, raw = classify_stop(
        _msg(
            {
                "model_provider": "anthropic",
                "stop_reason": "end_turn",
                "status": "incomplete",
            }
        )
    )
    assert cat is StopCategory.NORMAL and raw == "end_turn"


def test_core_stop_classifier_has_no_openai_provider_branch():
    source = (Path(__file__).resolve().parents[2] / "shared/lm/stop.py").read_text()
    assert 'provider == "openai"' not in source


def test_gemini_stop_normal():
    cat, raw = classify_stop(_msg({"model_provider": "google_genai", "finish_reason": "STOP"}))
    assert cat is StopCategory.NORMAL and raw == "STOP"


def test_gemini_max_tokens_truncated():
    cat, _ = classify_stop(_msg({"model_provider": "google_genai", "finish_reason": "MAX_TOKENS"}))
    assert cat is StopCategory.TRUNCATED


def test_gemini_safety_unexpected():
    cat, _ = classify_stop(_msg({"model_provider": "google_genai", "finish_reason": "SAFETY"}))
    assert cat is StopCategory.UNEXPECTED


def test_gemini_missing_corrupted():
    cat, _ = classify_stop(_msg({"model_provider": "google_genai"}))
    assert cat is StopCategory.CORRUPTED


def test_moonshot_stop_normal():
    cat, raw = classify_stop(_msg({"model_provider": "moonshot", "finish_reason": "stop"}))
    assert cat is StopCategory.NORMAL and raw == "stop"


def test_moonshot_tool_calls_normal():
    cat, _ = classify_stop(_msg({"model_provider": "moonshot", "finish_reason": "tool_calls"}))
    assert cat is StopCategory.NORMAL


def test_moonshot_length_truncated():
    cat, _ = classify_stop(_msg({"model_provider": "moonshot", "finish_reason": "length"}))
    assert cat is StopCategory.TRUNCATED


def test_unknown_provider_raises():
    with pytest.raises(ValueError, match="register_stop_spec"):
        classify_stop(_msg({"model_provider": "cohere", "finish_reason": "stop"}))


def test_missing_provider_raises():
    with pytest.raises(ValueError, match="unknown model_provider"):
        classify_stop(_msg({"stop_reason": "end_turn"}))
