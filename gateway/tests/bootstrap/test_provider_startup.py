from __future__ import annotations

import pytest
from fastapi import FastAPI

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from gateway import app as gateway_app
from tests.fixtures.gateway_config import gateway_test_process


@pytest.mark.asyncio
async def test_gateway_startup_propagates_zero_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    failure = RuntimeError("no provider plugins enabled")

    def _fail_provider_load() -> None:
        raise failure

    monkeypatch.setattr(gateway_app, "build_model_catalog", _fail_provider_load)
    app = FastAPI()

    with (
        gateway_test_process(machine=machine_identity) as process,
        pytest.raises(RuntimeError, match="no provider plugins enabled") as exc_info,
    ):
        async with gateway_app.lifespan(app, process=process, process_ownership="borrowed"):
            pass

    assert exc_info.value is failure
    assert not hasattr(app.state, "ctx")
