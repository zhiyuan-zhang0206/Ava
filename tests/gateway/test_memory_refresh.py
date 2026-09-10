"""`POST /api/memory/refresh` endpoint unit tests — trigger gateway checkout
fast-forward to origin/main, return HEAD sha after pull."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import pytest
from fastapi.testclient import TestClient

from gateway.app import app


class TestRefreshPath:
    """refresh calls shared.memory_repo.pull_main(), passing the returned sha through to the caller."""

    def test_refresh_returns_head_sha(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """stub pull_main → verify 200 + head returned, and pull_main called once."""
        import shared.memory_repo as _memory_repo

        calls = {"n": 0}

        def _fake_pull_main() -> str:
            calls["n"] += 1
            return "abc1234"

        monkeypatch.setattr(_memory_repo, "pull_main", _fake_pull_main)

        with TestClient(app) as client:
            resp = client.post("/api/memory/refresh")
        assert resp.status_code == 200
        assert resp.json()["head"] == "abc1234"
        assert calls["n"] == 1


class TestRefreshOffEventLoop:
    """P2 #2102: `pull_main` shells out to git (network fetch) — it must run on
    a worker thread, never the gateway's event loop, or a dead remote freezes
    the whole gateway until the watchdog kills it (2026-09-10 04:06-04:35)."""

    def test_refresh_runs_pull_off_the_event_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio as _asyncio
        import threading

        import shared.memory_repo as _memory_repo

        real_to_thread = cast("Callable[..., object]", _asyncio.to_thread)
        handoffs: list[tuple[object, int]] = []  # (fn, event-loop thread ident)
        pull_threads: list[int] = []

        def _fake_pull_main() -> str:
            pull_threads.append(threading.get_ident())
            return "abc1234"

        def _probe_to_thread(fn: Callable[..., object], *args: object, **kwargs: object) -> object:
            # Runs on the handler's own thread — i.e. the event loop's thread
            # (the handler is async and awaits this call).
            handoffs.append((fn, threading.get_ident()))
            return real_to_thread(fn, *args, **kwargs)

        monkeypatch.setattr(_memory_repo, "pull_main", _fake_pull_main)
        monkeypatch.setattr(_asyncio, "to_thread", _probe_to_thread)

        with TestClient(app) as client:
            resp = client.post("/api/memory/refresh")
        assert resp.status_code == 200
        assert resp.json()["head"] == "abc1234"
        assert pull_threads, "pull_main never ran"
        assert handoffs, "the handler never routed pull_main through asyncio.to_thread"
        # The property under test: the git call ran on a worker thread, NOT on
        # the event-loop thread the handler awaited from.
        assert pull_threads[0] != handoffs[0][1]
