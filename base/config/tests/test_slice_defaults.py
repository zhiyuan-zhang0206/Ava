"""`defaults` and `model_overrides`: the values a test hands in instead of patching `settings`."""

from __future__ import annotations

import pytest

from base.config import get_field, settings
from base.config.tests.configuration import defaults, model_overrides
from base.host.env.agent_slices import AgentSlices


def test_a_named_field_takes_the_given_value_and_the_rest_the_unit_environment() -> None:
    read = defaults(agent__prompt_codeact_enabled=False)

    assert read("agent", "prompt_codeact_enabled") is False
    assert read("lm", "reasoning_effort") == settings.lm.reasoning_effort


def test_a_named_field_neither_reads_nor_changes_the_global_settings() -> None:
    before = settings.agent.prompt_codeact_enabled

    slices = AgentSlices.resolve(default_reader=defaults(agent__prompt_codeact_enabled=not before))

    assert slices.prompt.prompt_codeact_enabled is (not before)
    assert settings.agent.prompt_codeact_enabled is before


def test_an_unknown_field_fails_at_construction() -> None:
    with pytest.raises(AttributeError):
        defaults(agent__no_such_field=1)
    with pytest.raises(AttributeError):
        defaults(no_such_domain__field=1)


def test_model_overrides_take_the_named_fields_and_the_unit_environment_for_the_rest() -> None:
    overrides = model_overrides(reasoning_effort="xhigh", auto_compact_fraction=0.25)

    assert overrides.reasoning_effort == "xhigh"
    assert overrides.auto_compact_fraction == 0.25
    assert overrides.compact_reminder_fraction == get_field("compact_reminder_fraction")
    assert get_field("reasoning_effort") != "xhigh"


def test_model_overrides_reject_a_name_that_is_not_an_override_field() -> None:
    with pytest.raises(TypeError, match="no_such_field"):
        model_overrides(no_such_field=1)
