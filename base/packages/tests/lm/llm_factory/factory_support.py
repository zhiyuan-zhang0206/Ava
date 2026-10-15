import sys
import types

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatResult


class FakeLLM(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "fake"

    def _generate(self, *_args: object, **_kwargs: object) -> ChatResult:
        raise NotImplementedError


def install_fake_module(monkeypatch: pytest.MonkeyPatch, name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


class _SpyClient:
    def __init__(self, *, async_close: bool = False) -> None:
        self.closed = 0
        self._async_close = async_close

    def close(self) -> object:
        if self._async_close:
            return self._aclose()
        self.closed += 1
        return None

    async def _aclose(self) -> None:
        self.closed += 1


__all__ = ["FakeLLM", "_SpyClient"]
