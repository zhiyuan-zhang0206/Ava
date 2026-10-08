"""Shared fixtures and helpers for the ava.understand test files; split from ava/tests/understand/test_understand.py (task #4922)."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from base.config import settings

understand_mod = cast(Any, importlib.import_module("ava.understand"))


@pytest.fixture
def fake_image(tmp_path: Path) -> Path:
    p = tmp_path / "img.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    return p


@pytest.fixture
def fake_video(tmp_path: Path) -> Path:
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"\x00" * 32)
    return p


@pytest.fixture
def fake_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.7\n" + b"\x00" * 32)
    return p


@pytest.fixture
def mock_deepseek(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch `base.lm.factory.build_chat_model` (the text path's provider) → fake llm.
    Captures the model id it was asked to build and the message content."""
    llm = MagicMock(name="deepseek_chat_model")
    response = MagicMock()
    response.content = "fake answer"
    response.response_metadata = {}
    llm.invoke.return_value = response
    captured: dict[str, Any] = {"llm": llm}

    def _fake_build(model: str, **kwargs: object):
        captured["model"] = model
        captured["reasoning_effort"] = kwargs.get("reasoning_effort")
        return llm

    monkeypatch.setattr("base.lm.factory.build_chat_model", _fake_build)
    return captured


@pytest.fixture
def mock_gemini(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch `base.lm.factory.build_chat_model` (the media path's provider
    factory) → fake llm. Captures the model id and the media-path kwargs the
    media path passes (media_resolution / media_thinking_level / base_url)."""
    monkeypatch.setattr(settings.lm, "gemini_api_key", SecretStr("fake-key-for-test"))
    llm = MagicMock(name="media_chat_model")
    response = MagicMock()
    response.content = "fake answer"
    response.response_metadata = {}
    llm.invoke.return_value = response
    captured: dict[str, Any] = {"llm": llm}

    def _fake_build(model: str, **kwargs: object):
        captured["model"] = model
        captured["kwargs"] = kwargs
        return llm

    monkeypatch.setattr("base.lm.factory.build_chat_model", _fake_build)
    return captured


def _content(captured: dict[str, Any]) -> list[Any]:
    """The content list of the single HumanMessage passed to invoke."""
    return captured["llm"].invoke.call_args[0][0][0].content
