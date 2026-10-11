"""Clock and model boot isolation for hosted wake recovery tests."""

from unittest.mock import AsyncMock

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from services.agent_runner.agent_host import dispatcher
from services.agent_runner.agent_host import runtime as runtime_module


def _accept_model_config(**_kwargs: object) -> str:
    return "test"


@pytest.fixture(autouse=True)
def isolated_clocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatcher, "CANCEL_UNWIND_TIMEOUT_S", 0.03)
    monkeypatch.setattr(runtime_module, "validate_model_config", _accept_model_config)
    monkeypatch.setattr(
        runtime_module,
        "boot_agent_scope",
        AsyncMock(return_value=(FakeListChatModel(responses=["unused"]), None)),
    )


__all__ = ["isolated_clocks"]
