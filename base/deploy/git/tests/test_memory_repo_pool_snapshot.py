from __future__ import annotations

from typing import ClassVar

import pytest

from base.deploy.git import memory_repo


def test_download_pool_snapshot_streams_and_enforces_cap_midstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    monkeypatch.setattr(memory_repo, "POOL_SNAPSHOT_MAX_BYTES", 16)

    class _FakeResp:
        status_code: ClassVar[int] = 200
        headers: ClassVar[dict[str, str]] = {}

        def iter_bytes(self) -> object:
            yield from [b"x" * 8, b"x" * 8, b"x" * 8]

    class _FakeStreamCtx:
        def __enter__(self) -> _FakeResp:
            return _FakeResp()

        def __exit__(self, *exc: object) -> bool:
            return False

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def stream(self, *args: object, **kwargs: object) -> _FakeStreamCtx:
            return _FakeStreamCtx()

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    with pytest.raises(memory_repo.MemoryPoolBootstrapFailed, match="exceeds"):
        memory_repo.download_pool_snapshot("http://gw/api/memory/pool", {})


def test_download_pool_snapshot_declared_oversize_rejected_before_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    monkeypatch.setattr(memory_repo, "POOL_SNAPSHOT_MAX_BYTES", 16)

    class _FakeResp:
        status_code: ClassVar[int] = 200
        headers: ClassVar[dict[str, str]] = {"Content-Length": "999999999"}

        def iter_bytes(self) -> object:
            raise AssertionError("body must not be read")

    class _FakeStreamCtx:
        def __enter__(self) -> _FakeResp:
            return _FakeResp()

        def __exit__(self, *exc: object) -> bool:
            return False

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def stream(self, *args: object, **kwargs: object) -> _FakeStreamCtx:
            return _FakeStreamCtx()

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    with pytest.raises(memory_repo.MemoryPoolBootstrapFailed, match="declared"):
        memory_repo.download_pool_snapshot("http://gw/api/memory/pool", {})


def test_download_pool_snapshot_transport_error_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    class _FailingClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def __enter__(self) -> _FailingClient:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def stream(self, *args: object, **kwargs: object) -> object:
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "Client", _FailingClient)

    with pytest.raises(memory_repo.MemoryPoolBootstrapFailed, match="failed: boom"):
        memory_repo.download_pool_snapshot("http://gw/api/memory/pool", {})
