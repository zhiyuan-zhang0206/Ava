"""Shared urllib fakes for the ava.web test files; split from ava/tests/web/test_web.py (task #4922)."""

from __future__ import annotations

from typing import Any


class _FakeResp:
    """Minimum urllib.request.urlopen return object — supports `with` + `.read()`."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *_args: Any) -> None:
        pass

    def read(self) -> bytes:
        return self._payload
