"""Explicit SDK model installation owned by consumer-local tests."""

from typing import Any

import pytest

from ava.sdk_surface.installation import Installation
from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from base.packages.plugins.extensions import EMPTY


def _plugin_state_codec() -> Any:
    from agent.extensions import plugin_state_codec

    return plugin_state_codec


@pytest.fixture
def model_installation(
    model_catalog: ModelCatalog, config_authority: ConfigAuthority
) -> Installation:
    """Return an explicit SDK owner; the caller chooses when to install it."""
    return Installation(
        registry=EMPTY,
        expansions=(),
        wrap_layers={},
        skill_providers=(),
        metered=(),
        disabled=frozenset(),
        faces=False,
        undo=(),
        catalog=model_catalog,
        authority=config_authority,
        external_state=_plugin_state_codec,
    )


@pytest.fixture
def sdk_model_owner(monkeypatch: pytest.MonkeyPatch, model_installation: Installation) -> None:
    """Bind model facts for direct SDK consumers that do not exercise installation.

    Installation/rollback tests pass model facts to their real installer instead.
    This fixture is opt-in; it never supplies a catalog to an unbound production root.
    """
    import ava

    monkeypatch.setattr(ava, "__plugin_installation__", model_installation, raising=False)
