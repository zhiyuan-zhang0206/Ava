"""Shared urllib fakes and the web harness for the ava.web test files; split from ava/tests/namespaces/web/test_web.py (task #4922)."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any
from unittest.mock import MagicMock

import pytest

from ava.sdk_surface.batch import DEFAULT_BATCH_MAX_CONCURRENT
from ava.web import SearchResult, plans, runner, wiring
from ava.web.deps import FetchDeps, SearchDeps
from base.lm.effort import ReasoningEffort


class FakeResp:
    """Minimum urllib.request.urlopen return object — supports `with` + `.read()`."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> FakeResp:
        return self

    def __exit__(self, *_args: Any) -> None:
        pass

    def read(self) -> bytes:
        return self._payload


class FakeModelResponse:
    """Minimal AIMessage stand-in: a string `.content` and empty `.response_metadata`."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.response_metadata: dict[str, Any] = {}


class SummaryModel:
    """A chat model that answers "SUMMARY" (or raises `error`), calling `on_invoke` first."""

    def __init__(
        self, on_invoke: Callable[[], None] | None = None, error: Exception | None = None
    ) -> None:
        self._on_invoke = on_invoke
        self._error = error

    def invoke(self, messages: list[Any]) -> FakeModelResponse:
        if self._on_invoke is not None:
            self._on_invoke()
        if self._error is not None:
            raise self._error
        return FakeModelResponse("SUMMARY")

    def build(self, model: str, **kwargs: Any) -> SummaryModel:
        """The provider-factory shape: hand the model back whatever the arguments."""
        return self


class _UrlopenAnswer:
    """What the network answers inside one `with` block; the mock is reset on exit."""

    def __init__(self, urlopen: MagicMock, options: dict[str, Any]) -> None:
        self._urlopen = urlopen
        self._options = options

    def __enter__(self) -> None:
        for name, value in self._options.items():
            setattr(self._urlopen, name, value)

    def __exit__(self, *_exc: object) -> None:
        self._urlopen.reset_mock(return_value=True, side_effect=True)


class WebHarness:
    """`ava.web` over this process's real deps with the network replaced by `urlopen`, a mock.

    `set_brave` / `set_jina` / `set_model` change the config values of the deps, `build_chat_model`
    replaces the provider factory, `urlopen_returns` sets what the network answers for a `with`
    block, and `search` / `fetch` run the SDK calls against them.
    """

    def __init__(self, search: SearchDeps, fetch: FetchDeps) -> None:
        self.urlopen = MagicMock(name="urlopen")
        self.search_deps = dataclasses.replace(search, open_url=self._open)
        self.fetch_deps = dataclasses.replace(fetch, open_url=self._open)

    def _open(self, request: Any, timeout: float) -> Any:
        return self.urlopen(request, timeout)

    def set_brave(self, **changes: Any) -> None:
        self.search_deps = dataclasses.replace(
            self.search_deps, brave=dataclasses.replace(self.search_deps.brave, **changes)
        )

    def set_jina(self, **changes: Any) -> None:
        self.fetch_deps = dataclasses.replace(
            self.fetch_deps, jina=dataclasses.replace(self.fetch_deps.jina, **changes)
        )

    def set_model(self, **changes: Any) -> None:
        self.fetch_deps = dataclasses.replace(
            self.fetch_deps,
            fetch_model=dataclasses.replace(self.fetch_deps.fetch_model, **changes),
        )

    def build_chat_model(self, build: Callable[..., Any]) -> None:
        self.fetch_deps = dataclasses.replace(self.fetch_deps, build_chat_model=build)

    def urlopen_returns(self, **mock_options: Any) -> AbstractContextManager[None]:
        """For a `with` block, answer the network with `return_value=` / `side_effect=`."""
        return _UrlopenAnswer(self.urlopen, mock_options)

    def search(
        self,
        queries: list[str],
        count: int = 10,
        max_concurrent: int | None = DEFAULT_BATCH_MAX_CONCURRENT,
    ) -> list[list[Any]]:
        return runner.run_search(
            plans.plan_search(queries, count, max_concurrent), self.search_deps, SearchResult
        )

    def fetch(
        self,
        targets: list[tuple[str, str]],
        max_chars: int = 50_000,
        effort: str | ReasoningEffort | None = None,
        max_concurrent: int | None = DEFAULT_BATCH_MAX_CONCURRENT,
    ) -> list[str]:
        return runner.run_fetch(
            plans.plan_fetch(targets, max_chars, effort, max_concurrent), self.fetch_deps
        )


@pytest.fixture
def web_harness(sdk_model_owner: None) -> WebHarness:
    """The real web deps of the test SDK installation, with the network a mock."""
    return WebHarness(wiring.search_deps(), wiring.fetch_deps())


__all__ = ["FakeModelResponse", "FakeResp", "SummaryModel", "WebHarness", "web_harness"]
