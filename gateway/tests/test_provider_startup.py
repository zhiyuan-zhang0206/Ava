"""Gateway startup contract for provider-plugin availability."""

from __future__ import annotations

import importlib

import pytest
from fastapi import FastAPI


@pytest.mark.asyncio
async def test_gateway_startup_propagates_zero_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_app = importlib.import_module("gateway.app")
    failure = RuntimeError("no provider plugins enabled")

    def _fail_provider_load() -> None:
        raise failure

    monkeypatch.setattr(gateway_app, "ensure_provider_plugins_loaded", _fail_provider_load)
    app = FastAPI()

    with pytest.raises(RuntimeError, match="no provider plugins enabled") as exc_info:
        async with gateway_app.lifespan(app):
            pass

    assert exc_info.value is failure
    assert not hasattr(app.state, "ctx")
