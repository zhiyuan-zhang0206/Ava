"""Memory search cases: the work one search may cost, and stopping it when nobody waits.

One search issues at most `memory_rerank_candidates` decision requests, whatever `k` or the
exclusion list it carries, and the work stops when the caller's deadline expires or the caller
disconnects.
"""

from __future__ import annotations

import asyncio
import time
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from base.config import settings
from gateway.app import app
from gateway.routers import memory as memory_router
from gateway.schemas.memory import MemorySearchRequest
from gateway.tests.routers.test_memory_search import (
    Judge,
    StubProvider,
    asgi_client,
    memory_root,
    patch_provider,
)
from gateway.tests.routers.test_memory_search import (
    judge as judge,
)
from gateway.tests.routers.test_memory_search import (
    memory_search_app_db as memory_search_app_db,
)
from services.derived.memory_indexer import rerank
from services.derived.memory_indexer.backends.base import ChunkHit

_CANDIDATES = 30
_POOL_SIZE = 150


def _name(index: int) -> str:
    return f"notes/n{index:03d}.md"


class _Pool:
    """A pool of notes whose vector order is their index, and a backend that returns the
    first `k` of it, recording each `k` asked."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
        from services.derived.memory_indexer.backends import factory

        (root / "notes").mkdir()
        for index in range(_POOL_SIZE):
            (root / _name(index)).write_text(f"passage {index:03d}\n", encoding="utf-8")
        self.asked: list[int] = []
        hits = [
            ChunkHit(str(root / _name(i)), "body", 0, 1.0 - i / 1000) for i in range(_POOL_SIZE)
        ]
        asked = self.asked

        class _Backend:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def search_topk_async(
                self, _vec: object, k: int, *, timeout: float
            ) -> list[ChunkHit]:
                del timeout
                asked.append(k)
                return hits[:k]

        monkeypatch.setattr(factory, "get_backend", _Backend)
        monkeypatch.setattr(memory_router, "gateway_memory_dir", partial(memory_root, root))
        patch_provider(monkeypatch, StubProvider)
        monkeypatch.setattr(settings.services, "memory_rerank_candidates", _CANDIDATES)
        # The handler reads its gate from `app.state`, which only the gateway lifespan fills; a
        # fresh one per test (a semaphore binds to the loop of its first contended acquire).
        monkeypatch.setattr(app.state, "memory_search_gate", asyncio.Semaphore(2), raising=False)


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Pool:
    return _Pool(monkeypatch, tmp_path)


def _search(**fields: Any) -> dict[str, Any]:
    return {"query": "q", "k": 3, "purpose": "recall", **fields}


def _judged_numbers(judge: Judge) -> list[str]:
    """The note numbers the decision model was shown, in request order."""
    return [state.split("passage ")[1][:3] for _model, state, _question in judge.calls]


# ── the cost of one search ──


@pytest.mark.parametrize("excluded", [0, 10, 60])
def test_decision_requests_do_not_grow_with_the_exclusion_list(
    pool: _Pool, judge: Judge, excluded: int
) -> None:
    """A long-lived session excludes more notes every turn; the ranking work stays the same."""
    with TestClient(app) as client:
        resp = client.post(
            "/api/memory/search",
            json=_search(exclude=[_name(index) for index in range(excluded)]),
        )

    assert resp.status_code == 200
    assert len(judge.calls) == _CANDIDATES
    assert len(resp.json()["results"]) == 3
    # the closest notes that are not excluded are the ones ranked
    assert sorted(_judged_numbers(judge)) == [f"{i:03d}" for i in range(excluded, excluded + 30)]
    # vector search is asked for enough notes to fill the candidates after exclusion
    assert pool.asked == [_CANDIDATES + excluded]


def test_excluded_notes_never_reach_the_decision_model_or_the_response(
    pool: _Pool, judge: Judge
) -> None:
    del pool
    judge.rules = {"passage 001": 0.99, "passage 002": 0.98, "passage 031": 0.97}
    excluded = [_name(1), _name(2)]

    with TestClient(app) as client:
        resp = client.post("/api/memory/search", json=_search(exclude=excluded))

    assert resp.status_code == 200
    assert resp.json()["results"][0]["path"] == _name(31)
    shown = " ".join(state for _model, state, _question in judge.calls)
    assert "passage 001" not in shown
    assert "passage 002" not in shown
    # the candidates are filled to the configured count from the next-closest notes
    assert len(judge.calls) == _CANDIDATES
    assert "passage 031" in shown and "passage 032" not in shown
    assert not {r["path"] for r in resp.json()["results"]} & set(excluded)


@pytest.mark.parametrize("k", [31, 60, 100])
def test_a_large_k_does_not_raise_the_decision_request_count(
    pool: _Pool, judge: Judge, k: int
) -> None:
    """Asking for more results than there are candidates ranks the candidates, not `k` notes."""
    with TestClient(app) as client:
        resp = client.post("/api/memory/search", json=_search(purpose="search", k=k))

    assert resp.status_code == 200
    assert len(judge.calls) == _CANDIDATES
    assert len(resp.json()["results"]) == _CANDIDATES
    assert pool.asked == [_CANDIDATES]


def test_dedup_ranks_its_own_smaller_candidate_count_whatever_k_is(
    pool: _Pool, judge: Judge, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.services, "memory_dedup_candidates", 7)

    with TestClient(app) as client:
        resp = client.post("/api/memory/search", json=_search(purpose="dedup", k=50))

    assert resp.status_code == 200
    assert len(judge.calls) == 7
    assert pool.asked == [7]


# ── the exclusion list is validated at the boundary ──

_TOO_MANY = [f"n{i}.md" for i in range(801)]
_VALID_PATHS = ["a.md", "notes/n001.md", "a b/c-d_e.md", "ünï/çode.md"]


@pytest.mark.parametrize(
    "exclude",
    [
        "a.md",
        [1],
        [None],
        [""],
        ["/etc/passwd"],
        ["../outside.md"],
        ["a/../b.md"],
        ["a/./b.md"],
        ["./a.md"],
        ["a//b.md"],
        ["a/"],
        ["a\\b.md"],
        ["a\x00b.md"],
        ["a\nb.md"],
        ["x" * 1025],
        _TOO_MANY,
    ],
    ids=lambda value: repr(value)[:40],
)
def test_an_invalid_exclusion_list_is_rejected(pool: _Pool, exclude: Any) -> None:
    with TestClient(app) as client:
        resp = client.post("/api/memory/search", json=_search(exclude=exclude))

    assert resp.status_code == 422
    assert pool.asked == []


def test_the_largest_exclusion_list_is_accepted(pool: _Pool) -> None:
    exclude = [f"n{i}.md" for i in range(800)]
    with TestClient(app) as client:
        resp = client.post("/api/memory/search", json=_search(exclude=exclude))

    assert resp.status_code == 200
    # the vector service takes at most 1000 notes per query
    assert pool.asked == [_CANDIDATES + 800] and pool.asked[0] <= 1000


def test_the_exclusion_limit_fits_the_vector_service_query_limit() -> None:
    """Vector search is asked for the candidates plus every excluded note in one query."""
    from base.api_contracts.memory_search import MEMORY_SEARCH_MAX_EXCLUDE
    from services.derived.memory_search.app import MAX_K

    most_candidates = type(settings.services).model_fields["memory_rerank_candidates"]
    limit = next(m.le for m in most_candidates.metadata if hasattr(m, "le"))
    assert MEMORY_SEARCH_MAX_EXCLUDE + limit <= MAX_K


def test_valid_paths_are_accepted(pool: _Pool) -> None:
    with TestClient(app) as client:
        resp = client.post("/api/memory/search", json=_search(exclude=_VALID_PATHS))

    assert resp.status_code == 200
    assert pool.asked == [_CANDIDATES + len(_VALID_PATHS)]


@pytest.mark.parametrize("deadline", [0, -1, "soon", "inf"])
def test_an_invalid_deadline_is_rejected(pool: _Pool, deadline: Any) -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/api/memory/search",
            content=_json({"deadline_seconds": deadline}),
            headers={"content-type": "application/json"},
        )

    assert resp.status_code == 422
    assert pool.asked == []


def _json(fields: dict[str, Any]) -> str:
    import json

    return json.dumps(_search(**fields))


# ── the work stops when the caller stops waiting ──


class _Blocked:
    """A decision model that never answers and notes which requests were cancelled."""

    def __init__(self) -> None:
        self.started = 0
        self.cancelled = 0
        self.all_started = asyncio.Event()

    async def __call__(self, *_args: object, **_kwargs: object) -> float:
        self.started += 1
        if self.started == _CANDIDATES:
            self.all_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        raise AssertionError("unreachable")


async def test_the_caller_deadline_bounds_the_search_below_the_gateway_deadline(
    pool: _Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    del pool
    blocked = _Blocked()
    monkeypatch.setattr(rerank, "ask_relevance", blocked)
    monkeypatch.setattr(settings.services, "memory_search_deadline_seconds", 30.0)

    started = time.monotonic()
    async with asgi_client() as client:
        resp = await asyncio.wait_for(
            client.post("/api/memory/search", json=_search(deadline_seconds=0.3)), timeout=10
        )
    elapsed = time.monotonic() - started

    assert resp.status_code == 503
    assert resp.json()["reason"] == "indexer_unavailable"
    assert "0.3s deadline" in resp.json()["detail"]
    assert elapsed < 5
    # every decision request that was in flight was cancelled
    assert blocked.started == _CANDIDATES
    assert blocked.cancelled == _CANDIDATES


async def test_a_caller_deadline_above_the_gateway_deadline_does_not_extend_it(
    pool: _Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    del pool
    monkeypatch.setattr(rerank, "ask_relevance", _Blocked())
    monkeypatch.setattr(settings.services, "memory_search_deadline_seconds", 0.3)

    async with asgi_client() as client:
        resp = await asyncio.wait_for(
            client.post("/api/memory/search", json=_search(deadline_seconds=600)), timeout=10
        )

    assert resp.status_code == 503
    assert "0.3s deadline" in resp.json()["detail"]


def _request_that_disconnects_when(gone: asyncio.Event) -> Request:
    """A request whose client leaves once `gone` is set, as uvicorn reports it."""

    async def receive() -> dict[str, str]:
        if gone.is_set():
            return {"type": "http.disconnect"}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    return Request({"type": "http", "app": app}, receive)


async def test_a_disconnect_cancels_the_decision_requests_still_in_flight(
    pool: _Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    del pool
    blocked = _Blocked()
    monkeypatch.setattr(rerank, "ask_relevance", blocked)
    gone = asyncio.Event()
    handler = asyncio.create_task(
        memory_router.post_memory_search(
            _request_that_disconnects_when(gone), MemorySearchRequest(**_search())
        )
    )
    try:
        await asyncio.wait_for(blocked.all_started.wait(), timeout=10)
        assert not handler.done()

        gone.set()
        # The handler ends by itself: a still-running ranking would keep it parked here.
        response = await asyncio.wait_for(asyncio.shield(handler), timeout=5)
    finally:
        handler.cancel()
        await asyncio.gather(handler, return_exceptions=True)

    assert response.results == []
    assert blocked.cancelled == _CANDIDATES


async def test_a_connected_caller_gets_the_ranking(pool: _Pool, judge: Judge) -> None:
    del pool
    gone = asyncio.Event()

    response = await memory_router.post_memory_search(
        _request_that_disconnects_when(gone), MemorySearchRequest(**_search())
    )

    assert len(response.results) == 3
    assert len(judge.calls) == _CANDIDATES


async def test_cancelling_the_search_cancels_every_decision_request(
    pool: _Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation of the handler's task reaches the decision calls (no shielded work)."""
    del pool
    blocked = _Blocked()
    monkeypatch.setattr(rerank, "ask_relevance", blocked)
    handler = asyncio.create_task(
        memory_router.post_memory_search(
            _request_that_disconnects_when(asyncio.Event()), MemorySearchRequest(**_search())
        )
    )
    await asyncio.wait_for(blocked.all_started.wait(), timeout=10)

    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler

    assert blocked.cancelled == _CANDIDATES
