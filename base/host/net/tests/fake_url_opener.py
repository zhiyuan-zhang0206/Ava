from __future__ import annotations

import io
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from http.client import HTTPMessage
from typing import Any


@dataclass(frozen=True)
class Answer:
    body: bytes | str = b""
    status: int = 200
    headers: Mapping[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True)
class Opened:
    url: str
    timeout: float
    method: str
    headers: Mapping[str, str]
    data: bytes | None


class FakeResponse:
    def __init__(self, answer: Answer, url: str) -> None:
        self.status = answer.status
        self.url = url
        self.headers = _message(answer.headers)
        self._body = io.BytesIO(_bytes(answer.body))

    def read(self, amount: int = -1) -> bytes:
        return self._body.read(amount)

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        self._body.close()


@dataclass
class _Route:
    matches: Callable[[str], bool]
    label: str
    answers: list[Answer | BaseException]
    repeat: bool


def _bytes(body: bytes | str) -> bytes:
    return body if isinstance(body, bytes) else body.encode()


def _message(headers: Mapping[str, str]) -> HTTPMessage:
    message = HTTPMessage()
    for name, value in headers.items():
        message[name] = value
    return message


def _starts_with(prefix: str) -> Callable[[str], bool]:
    return lambda url: url.startswith(prefix)


class FakeUrlOpener:
    def __init__(self) -> None:
        self.calls: list[Opened] = []
        self._routes: list[_Route] = []

    @property
    def urls(self) -> list[str]:
        return [call.url for call in self.calls]

    def on(
        self,
        match: str | Callable[[str], bool],
        *answers: Answer | BaseException,
        repeat: bool = False,
    ) -> None:
        if not answers:
            raise ValueError("a route needs at least one answer")
        if callable(match):
            matches, label = match, "<predicate>"
        else:
            matches, label = _starts_with(match), match
        self._routes.append(_Route(matches, label, list(answers), repeat))

    def _next_answer(self, url: str) -> Answer | BaseException:
        route = next((r for r in self._routes if r.matches(url)), None)
        if route is None:
            raise AssertionError(f"unscripted request: {url}")
        if not route.answers:
            raise AssertionError(f"answers for {route.label} ran out: {url}")
        keep_last = route.repeat and len(route.answers) == 1
        return route.answers[0] if keep_last else route.answers.pop(0)

    def __call__(
        self, target: str | urllib.request.Request, timeout: float
    ) -> AbstractContextManager[Any]:
        request = target if isinstance(target, urllib.request.Request) else None
        url = request.full_url if request is not None else str(target)
        data = request.data if request is not None else None
        self.calls.append(
            Opened(
                url=url,
                timeout=timeout,
                method=request.get_method() if request is not None else "GET",
                headers=dict(request.header_items()) if request is not None else {},
                data=data if isinstance(data, bytes) or data is None else None,
            )
        )
        answer = self._next_answer(url)
        if isinstance(answer, BaseException):
            raise answer
        if not 200 <= answer.status < 300:
            raise urllib.error.HTTPError(
                url,
                answer.status,
                f"HTTP {answer.status}",
                _message(answer.headers),
                io.BytesIO(_bytes(answer.body)),
            )
        return FakeResponse(answer, url)
