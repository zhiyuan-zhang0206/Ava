"""Provider doubles and media fixtures shared by understand contract tests."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ava.sdk_surface.batch import DEFAULT_BATCH_MAX_CONCURRENT
from ava.understand import answer, plans, wiring
from ava.understand.answer import UnderstandDeps
from base.lm.effort import ReasoningEffort

__all__ = [
    "ProviderCapture",
    "fake_image",
    "fake_pdf",
    "fake_video",
    "mock_deepseek",
    "mock_gemini",
    "run_understand",
    "understand_deps",
]


def run_understand(
    deps: UnderstandDeps,
    targets: list[dict[str, Any]],
    effort: str | ReasoningEffort = ReasoningEffort.MAX,
    max_concurrent: int | None = DEFAULT_BATCH_MAX_CONCURRENT,
) -> list[str]:
    """`ava.understand` with these deps: the same validation, then `answer.run`."""
    return answer.run(plans.normalize(targets, effort, max_concurrent), deps)


class ProviderCapture:
    """Capture factory options and expose a configurable provider invocation double.

    `deps` are this process's real understand deps with the provider factory replaced by
    `build`, no output directory (nothing is saved) and a live clock; `configure` changes any
    of them, and `understand` runs the SDK call with them.
    """

    def __init__(self, base: UnderstandDeps) -> None:
        self.deps = dataclasses.replace(
            base,
            build_chat_model=self.build,
            output_dir=lambda: None,
            now=lambda: datetime.now(UTC),
        )
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

    def configure(self, **changes: Any) -> None:
        """Replace fields of the deps the next `understand` call receives."""
        self.deps = dataclasses.replace(self.deps, **changes)

    def understand(
        self,
        targets: list[dict[str, Any]],
        effort: str | ReasoningEffort = ReasoningEffort.MAX,
        max_concurrent: int | None = DEFAULT_BATCH_MAX_CONCURRENT,
    ) -> list[str]:
        """Run `ava.understand` for `targets` against this capture's deps."""
        return run_understand(self.deps, targets, effort, max_concurrent)

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
def understand_deps(sdk_model_owner: None) -> UnderstandDeps:
    """The deps of this process's SDK installation: its configured models and the real factory."""
    return wiring.understand_deps()


@pytest.fixture
def mock_deepseek(understand_deps: UnderstandDeps) -> ProviderCapture:
    """The shared provider factory replaced for text routing tests."""
    return ProviderCapture(understand_deps)


@pytest.fixture
def mock_gemini(understand_deps: UnderstandDeps) -> ProviderCapture:
    """The shared provider factory replaced for media routing tests."""
    return ProviderCapture(understand_deps)
