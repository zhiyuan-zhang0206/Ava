"""Explicit SDK model installation owned by consumer-local tests."""

from collections.abc import Callable

import pytest

from ava.sdk_surface.installation import ExternalState, Installation
from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from base.packages.plugins.extensions import EMPTY

__all__ = ["installation_with_models", "model_installation", "sdk_model_owner"]


def installation_with_models(
    catalog: ModelCatalog,
    authority: ConfigAuthority,
    *,
    external_state: Callable[[], ExternalState] | None,
) -> Installation:
    """An SDK owner holding these model facts; `external_state` is the agent layer's codec."""
    return Installation(
        registry=EMPTY,
        expansions=(),
        wrap_layers={},
        skill_providers=(),
        metered=(),
        disabled=frozenset(),
        faces=False,
        undo=(),
        catalog=catalog,
        authority=authority,
        external_state=external_state,
    )


@pytest.fixture
def model_installation(
    model_catalog: ModelCatalog, config_authority: ConfigAuthority
) -> Installation:
    """Return an explicit SDK owner without plugin state; the caller chooses when to install it.

    Tests that attach external plugin state bind `agent_model_owner.model_installation`.
    """
    return installation_with_models(model_catalog, config_authority, external_state=None)


@pytest.fixture
def sdk_model_owner(monkeypatch: pytest.MonkeyPatch, model_installation: Installation) -> None:
    """Bind model facts for direct SDK consumers that do not exercise installation.

    Installation/rollback tests pass model facts to their real installer instead.
    This fixture is opt-in; it never supplies a catalog to an unbound production root.
    """
    import ava

    monkeypatch.setattr(ava, "__plugin_installation__", model_installation, raising=False)
