"""Provider doubles and media fixtures shared by understand contract tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from base.config import settings

__all__ = [
    "ProviderCapture",
    "fake_image",
    "fake_pdf",
    "fake_video",
    "mock_deepseek",
    "mock_gemini",
]


class ProviderCapture:
    """Capture factory options and expose a configurable provider invocation double."""

    def __init__(self) -> None:
        self.model: str | None = None
        self.kwargs: dict[str, object] = {}
        self.invoke = MagicMock(name="provider_invoke")
        self._response = MagicMock()
        self._response.content = "fake answer"
        self._response.response_metadata = {}
        self.invoke.return_value = self._response
        self._model = MagicMock(name="chat_model", invoke=self.invoke)

    def build(self, model: str, **kwargs: object) -> MagicMock:
        """Factory replacement that records the requested model and options."""
        self.model = model
        self.kwargs = kwargs
        return self._model

    def respond_with(self, content: str | list[dict[str, str]]) -> None:
        """Set the default provider response while retaining its metadata shape."""
        self._response.content = content

    @property
    def content(self) -> list[Any]:
        """Content of the single HumanMessage passed to the latest invocation."""
        return self.invoke.call_args[0][0][0].content


@pytest.fixture
def fake_image(tmp_path: Path) -> Path:
    path = tmp_path / "img.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    return path


@pytest.fixture
def fake_video(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00" * 32)
    return path


@pytest.fixture
def fake_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "doc.pdf"
    path.write_bytes(b"%PDF-1.7\n" + b"\x00" * 32)
    return path


@pytest.fixture
def mock_deepseek(monkeypatch: pytest.MonkeyPatch) -> ProviderCapture:
    """Replace the shared provider factory for text routing tests."""
    capture = ProviderCapture()
    monkeypatch.setattr("base.lm.factory.build_chat_model", capture.build)
    return capture


@pytest.fixture
def mock_gemini(monkeypatch: pytest.MonkeyPatch) -> ProviderCapture:
    """Replace the shared provider factory with media credentials available."""
    monkeypatch.setattr(settings.lm, "gemini_api_key", SecretStr("fake-key-for-test"))
    capture = ProviderCapture()
    monkeypatch.setattr("base.lm.factory.build_chat_model", capture.build)
    return capture
