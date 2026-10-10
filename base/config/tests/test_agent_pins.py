"""`resolve_agent_config_pins` (base/config/agent_pins.py): the merge of an agent's two stored
config maps into its flat pin map — `config_overlay > birth_config`, unknown and plugin keys
dropped, exactly mirroring the process-boot merge."""

from __future__ import annotations

from base.config import resolve_agent_config_pins


def test_overlay_beats_birth() -> None:
    pins = resolve_agent_config_pins({"llm_model": "from-overlay"}, {"llm_model": "from-birth"})
    assert pins == {"llm_model": "from-overlay"}


def test_birth_fills_fields_overlay_omits() -> None:
    pins = resolve_agent_config_pins(
        {"llm_model": "from-overlay"}, {"reasoning_effort": "from-birth"}
    )
    assert pins == {"llm_model": "from-overlay", "reasoning_effort": "from-birth"}


def test_unknown_and_plugin_keys_are_dropped() -> None:
    pins = resolve_agent_config_pins(
        {"llm_model": "m", "ava_memory.pool_size": 3, "deleted_field": 1}, None
    )
    assert pins == {"llm_model": "m"}


def test_none_maps() -> None:
    assert resolve_agent_config_pins(None, None) == {}


def test_retired_gemini_cache_pins_do_not_reach_model_policy() -> None:
    pins = resolve_agent_config_pins(
        {"gemini_explicit_cache_enabled": True, "llm_model": "gemini-3.8-flash"},
        {"gemini_cache_timeout_seconds": 99},
    )
    assert pins == {"llm_model": "gemini-3.8-flash"}
