"""Transport shape and caller-owned catalog validation have distinct boundaries."""

from collections.abc import Mapping
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, Request
from pydantic import BaseModel

from base.agents import InvalidModelConfig
from base.lm.catalog import ModelCatalog
from base.lm.pricing import PriceBook
from base.packages.plugins import config_registration
from gateway.agents import lifecycle as gateway_lifecycle
from ops import lifecycle
from ops.rpc_schemas import RestartAgentRequest


@pytest.fixture
def no_plugin_overlay_schemas(monkeypatch: pytest.MonkeyPatch) -> None:
    def classes(_configs: Mapping[str, BaseModel] | None = None) -> dict[str, type[BaseModel]]:
        return {}

    monkeypatch.setattr(config_registration, "overlay_config_classes", classes)


def _catalog() -> ModelCatalog:
    return ModelCatalog(models={}, bindings={}, stops={}, prices=PriceBook({}, {}))


def _request() -> Request:
    return Request(
        {"type": "http", "app": SimpleNamespace(state=SimpleNamespace(catalog=_catalog()))}
    )


def test_dto_shape_does_not_construct_a_catalog(no_plugin_overlay_schemas: None) -> None:
    request = RestartAgentRequest(config_overlay={"llm_model": "test-provider-model"})
    assert request.config_overlay == {"llm_model": "test-provider-model"}
    assert request.config_overlay is not None
    with pytest.raises(config_registration.InvalidConfigOverlay, match="not a registered model"):
        config_registration.validate_config_overlay(request.config_overlay, models={})
    config_registration.validate_config_overlay(
        request.config_overlay, models={"test-provider-model": object()}
    )


def test_runner_catalog_refusal_precedes_transaction(
    no_plugin_overlay_schemas: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from base.db import transaction

    def unexpected_transaction(pool: object) -> None:
        pytest.fail("catalog admission must precede transaction acquisition")

    monkeypatch.setattr(transaction, "write_transaction", unexpected_transaction)
    request = RestartAgentRequest(config_overlay={"llm_model": "unknown-model"})
    with pytest.raises(InvalidModelConfig, match="not a registered model"):
        lifecycle._restart_blocking(
            MagicMock(), MagicMock(), 1, request, MagicMock(), catalog=_catalog()
        )


@pytest.mark.asyncio
async def test_gateway_membership_refusal_is_422_before_forward(
    no_plugin_overlay_schemas: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unexpected_forward(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid overlay must not be forwarded")

    monkeypatch.setattr(gateway_lifecycle, "forward_to_home_machine", unexpected_forward)
    request = _request()
    body = RestartAgentRequest(config_overlay={"llm_model": "unknown-model"})
    with pytest.raises(HTTPException) as failure:
        await gateway_lifecycle.post_agent_restart(1, request, body)
    assert failure.value.status_code == 422


@pytest.mark.asyncio
async def test_gateway_preserves_runner_typed_400(
    no_plugin_overlay_schemas: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    refusal = InvalidModelConfig("provider configuration is invalid")

    async def refused_forward(*args: object, **kwargs: object) -> None:
        raise refusal

    monkeypatch.setattr(gateway_lifecycle, "forward_to_home_machine", refused_forward)
    request = _request()
    with pytest.raises(InvalidModelConfig) as failure:
        await gateway_lifecycle.post_agent_restart(1, request, RestartAgentRequest())
    assert failure.value is refusal
    assert failure.value.http_status == 400
