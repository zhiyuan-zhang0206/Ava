"""Provider factory test doubles, with no catalog ownership."""

import sys
import types

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatResult


class _FakeLLM(BaseChatModel):
    """Minimal stub that passes the BaseChatModel isinstance check — _resolve_override
    success path must return a BaseChatModel subclass. At runtime LangChain won't
    actually call _generate; it's only used for type checking at the build_chat_model
    exit point."""

    @property
    def _llm_type(self) -> str:
        return "fake"

    def _generate(self, *_args: object, **_kwargs: object) -> ChatResult:
        raise NotImplementedError


def _install_fake_module(monkeypatch: pytest.MonkeyPatch, name: str) -> types.ModuleType:
    """Inject a fake module into sys.modules so that importlib.import_module can find it —
    cleaner than writing a real module to disk and cleaning up; monkeypatch automatically
    restores."""
    mod = types.ModuleType(name)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


class _SpyClient:
    """Provider-client stand-in recording `close()` calls (sync or async)."""

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


__all__ = ["_FakeLLM", "_SpyClient"]
