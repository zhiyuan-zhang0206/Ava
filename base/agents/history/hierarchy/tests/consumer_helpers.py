"""Explicit configuration inputs for real hierarchy consumer tests."""

from base.agents.history.hierarchy.group_consumer import UnderstandingReadInputs
from base.clock import Clock
from base.config import settings


def read_inputs() -> UnderstandingReadInputs:
    """Keep the test's existing field mutations visible at each real read point."""
    return UnderstandingReadInputs(
        enabled=lambda: settings.agent.understanding_enabled,
        default_model=lambda: settings.lm.llm_model,
        hierarchy_model=lambda: settings.lm.hierarchy_model,
        group_model=lambda: settings.agent.understanding_group_model,
        check_open=lambda: settings.agent.understanding_group_check_open,
        check_decay=lambda: settings.agent.understanding_group_check_decay,
        reasoning=lambda: settings.agent.understanding_group_reasoning,
        corrections=lambda: settings.agent.understanding_group_corrections,
        clock_factory=Clock.from_settings,
        timestamps_enabled=lambda: settings.general.message_timestamps,
    )
