"""The SDK model installation of tests that attach external plugin state (agent layer)."""

from typing import Any

import pytest

from ava.sdk_surface.installation import Installation
from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from tests.fixtures.unit.model_owner import installation_with_models

__all__ = ["model_installation"]


def _plugin_state_codec() -> Any:
    from agent.extensions import plugin_state_codec

    return plugin_state_codec


@pytest.fixture
def model_installation(
    model_catalog: ModelCatalog, config_authority: ConfigAuthority
) -> Installation:
    """Like `model_owner.model_installation`, with the agent layer's plugin state codec."""
    return installation_with_models(
        model_catalog, config_authority, external_state=_plugin_state_codec
    )
