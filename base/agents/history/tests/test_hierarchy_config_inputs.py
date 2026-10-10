"""Real configuration defaults and independent roots feeding the hierarchy component."""

import time
from unittest.mock import MagicMock

from base.agents.history.hierarchy import group_consumer as gc
from base.agents.history.hierarchy.tests.consumer_helpers import read_inputs
from base.config import settings
from base.lm.catalog import ModelCatalog


def test_owned_group_model_readers_are_lazy_isolated_and_live(
    model_catalog: ModelCatalog,
) -> None:
    """Configured grouping never evaluates unrelated defaults, even after a live edit."""
    import os
    from dataclasses import replace
    from unittest.mock import patch

    from base.config import ConfigBoot

    def unused() -> str:
        raise AssertionError("a configured grouping model bypasses the fallback readers")

    try:
        with patch.dict(os.environ):
            first, second = ConfigBoot(), ConfigBoot()
            first.set_field("understanding_group_model", "first-model")
            second.set_field("understanding_group_model", "second-model")
            first_inputs = replace(
                read_inputs(),
                group_model=lambda: first.view.agent.understanding_group_model,
                hierarchy_model=unused,
                default_model=unused,
            )
            second_inputs = replace(
                first_inputs, group_model=lambda: second.view.agent.understanding_group_model
            )
            assert not first.is_full() and not second.is_full()
            assert (
                gc._group_model(MagicMock(), 7, catalog=model_catalog, inputs=first_inputs)[0]
                == "first-model"
            )
            assert (
                gc._group_model(MagicMock(), 7, catalog=model_catalog, inputs=second_inputs)[0]
                == "second-model"
            )
            first.set_field("understanding_group_model", "first-update")
            assert (
                gc._group_model(MagicMock(), 7, catalog=model_catalog, inputs=first_inputs)[0]
                == "first-update"
            )
            assert (
                gc._group_model(MagicMock(), 7, catalog=model_catalog, inputs=second_inputs)[0]
                == "second-model"
            )
    finally:
        time.tzset()


def test_the_decay_defaults_to_three() -> None:
    assert type(settings.agent).model_fields["understanding_group_check_decay"].default == 3


def test_the_check_cadence_defaults_to_sixty_open_nodes() -> None:
    field = type(settings.agent).model_fields["understanding_group_check_open"]
    assert field.default == 60
