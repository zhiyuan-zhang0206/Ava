"""Memory search cases: event loop isolation."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.routers.tests.test_memory_search import (
    _SEARCH_PERMITS,
    _asgi_client,
    _assert_semaphore_locked,
    _never_returns,
    _search,
    _stub_search_backend,
)
from gateway.routers.tests.test_memory_search import (
    _app_db as _app_db,
)


class TestEventLoopIsolation:
    """The embed + backend calls are synchronous clients; the endpoint must run
    them off the event loop so a slow backend cannot stall the whole gateway
    (the 2026-08-03 freeze: 13 gateway restarts in 8h, three of the five
    examined freezes ended with a gemini-embedding POST as the last MainThread
    log line — the sync call had blocked healthz for minutes)."""

    def test_slow_embed_does_not_block_healthz(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import threading
        import time

        import gateway.routers.memory as _gw_memory
        import services.derived.memory_indexer.backends.factory as _factory
        import services.derived.memory_indexer.embeddings.factory as _embedding_factory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "a.md").write_text("---\ntype: Memory\n---\nx\n")

        in_flight = threading.Event()

        class _SlowProvider:
            dim = 768
            fingerprint = "fake:provider:dim=768"

            @staticmethod
            async def embed_query_async(_q: str) -> list[float]:
                in_flight.set()
                await asyncio.sleep(1.0)
                return [0.0] * 768

        monkeypatch.setattr(_embedding_factory, "get_provider", _SlowProvider)

        class _FakeBackend:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def search_topk_async(self, _v: object, _k: int, *, timeout: float) -> list[str]:
                assert timeout > 0  # the handler must hand the backend a real deadline
                return [str(tmp_path / "a.md")]

        monkeypatch.setattr(_factory, "get_backend", _FakeBackend)

        with TestClient(app) as client:
            outcome: dict[str, object] = {}

            def _search() -> None:
                outcome["resp"] = client.post("/api/memory/search", json={"query": "x", "k": 1})

            t = threading.Thread(target=_search)
            t.start()
            # Deterministic handshake instead of a fixed sleep: wait until the
            # search request has actually entered the slow embed (audit
            # round-2 cc-docs-tests P2 — a 0.2s sleep could either fire too
            # early under load or waste time when the embed is instant).
            assert in_flight.wait(5.0), "search never reached the slow embed"
            t0 = time.monotonic()
            health = client.get("/api/health")
            health_elapsed = time.monotonic() - t0
            t.join(timeout=5)

        assert health.status_code == 200
        assert health_elapsed < 0.8, (
            f"healthz took {health_elapsed:.2f}s while a memory search was in "
            "flight — the event loop is blocked"
        )
        assert outcome["resp"].status_code == 200  # type: ignore[union-attr]


class TestWedgedBackendReleasesPermits:
    """Every search finishes, and its permit comes back — whatever the backend does.

    The evening of 2026-08-03: the handler held one of two permits across an
    unbounded backend await. Both permits were pinned, every later request
    parked in `acquire` with no deadline, and `curl` on the route returned
    neither a response nor an error. Seven agents were stuck in passive recall
    without producing a single LLM turn, and force-killing them only restarted
    the same wait. These stub a stalled backend and assert the property the
    handler owes regardless.
    """

    async def test_wedged_backend_answers_503_instead_of_hanging(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _stub_search_backend(monkeypatch, tmp_path, search=_never_returns)

        async with _asgi_client() as client:
            resp = await asyncio.wait_for(_search(client), timeout=10)

        assert resp.status_code == 503
        assert resp.json()["reason"] == "indexer_unavailable"

    async def test_more_wedged_requests_than_permits_all_finish(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The overflow request never reaches the backend — it waits in
        `acquire`, which the deadline has to cover too."""
        _stub_search_backend(monkeypatch, tmp_path, search=_never_returns)

        async with _asgi_client() as client:
            responses = await asyncio.wait_for(
                asyncio.gather(*(_search(client) for _ in range(_SEARCH_PERMITS + 1))),
                timeout=10,
            )

        assert [r.status_code for r in responses] == [503] * (_SEARCH_PERMITS + 1)

    async def test_a_wedged_embed_is_covered_too(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The deadline spans both phases, not just the backend one.

        The backend stalled in the incident, but the embed runs under the same
        permit — a deadline covering only one phase would leave the other able to
        pin the endpoint the same way.
        """
        import services.derived.memory_indexer.embeddings.factory as _embedding_factory

        class _StuckProvider:
            dim = 768
            fingerprint = "fake:provider:dim=768"
            embed_query_async = _never_returns

        _stub_search_backend(monkeypatch, tmp_path, search=_never_returns)
        monkeypatch.setattr(_embedding_factory, "get_provider", _StuckProvider)

        async with _asgi_client() as client:
            responses = await asyncio.wait_for(
                asyncio.gather(*(_search(client) for _ in range(_SEARCH_PERMITS + 1))),
                timeout=10,
            )

        assert [r.status_code for r in responses] == [503] * (_SEARCH_PERMITS + 1)

    async def test_a_cancelled_holder_gives_its_permit_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A client that disconnects mid-search must not cost a permit.

        This is the other candidate mechanism for the outage. uvicorn cancels
        the handler task when the client goes away, and agents were being
        force-killed in batches that day — each kill cancelling whatever search
        that agent had in flight. If a cancelled holder kept its permit, a
        handful of kills would retire every permit and park the endpoint, with
        no thread and no socket left to show for it. That matches the dump
        (14 idle OS threads, nothing on 19530 or 443) exactly as well as a
        stalled backend does, and the recorded evidence cannot separate them —
        a suspended coroutine lives on no thread, so faulthandler cannot see
        which await it stopped at.
        """
        import services.derived.memory_indexer.backends.factory as _factory

        sem = _stub_search_backend(monkeypatch, tmp_path, search=_never_returns)

        async with _asgi_client() as client:
            holders = [asyncio.create_task(_search(client)) for _ in range(_SEARCH_PERMITS)]
            # Without this the test could pass vacuously, cancelling requests
            # that had not yet taken a permit.
            await _assert_semaphore_locked(sem)
            for task in holders:
                task.cancel()
            for task in holders:
                with pytest.raises(asyncio.CancelledError):
                    await task

            class _Healthy:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    pass

                async def search_topk_async(
                    self, _v: object, _k: int, *, timeout: float
                ) -> list[str]:
                    return [str(tmp_path / "a.md")]

            monkeypatch.setattr(_factory, "get_backend", _Healthy)
            recovered = await asyncio.wait_for(_search(client), timeout=10)

        # A 503 here would mean the permits never came back and this request
        # sat in acquire until its own deadline.
        assert recovered.status_code == 200

    async def test_permits_return_so_a_later_search_still_works(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Enough wedged requests to pin every permit, then a healthy one."""
        import services.derived.memory_indexer.backends.factory as _factory

        _stub_search_backend(monkeypatch, tmp_path, search=_never_returns)

        async with _asgi_client() as client:
            await asyncio.wait_for(
                asyncio.gather(*(_search(client) for _ in range(_SEARCH_PERMITS + 1))),
                timeout=10,
            )

            class _Healthy:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    pass

                async def search_topk_async(
                    self, _v: object, _k: int, *, timeout: float
                ) -> list[str]:
                    return [str(tmp_path / "a.md")]

            monkeypatch.setattr(_factory, "get_backend", _Healthy)
            recovered = await asyncio.wait_for(_search(client), timeout=10)

        assert recovered.status_code == 200
        assert recovered.json()["paths"] == ["a.md"]


class TestAcquireFastFail:
    """A congested query-embed gate answers 503 in ~1s, not after the search
    deadline (task #2003/E): a fleet wake that saturates the gate should fail
    fast — passive recall's own ~5s deadline then degrades in ~1s, and an
    explicit search learns immediately instead of queueing behind the gate.

    Before this, a deep acquire queue under the (15s) deadline made endpoint
    latency scale with queue length: the 2026-08-29 storm queued 18 searches
    and the recalled agent's first LLM turn waited out the whole queue.
    """

    async def test_congested_gate_fails_fast_with_503(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """One permit held by a wedged search; the next request 503s on the
        acquire budget, well before the search deadline."""
        from base.config import settings

        sem = _stub_search_backend(monkeypatch, tmp_path, search=_never_returns, permits=1)
        # A tiny acquire budget so the failure is provably the fast-fail, and a
        # deadline far above it so the deadline is NOT what answered.
        monkeypatch.setattr(settings.services, "memory_search_acquire_timeout_seconds", 0.05)
        monkeypatch.setattr(settings.services, "memory_search_deadline_seconds", 5.0)

        async with _asgi_client() as client:
            holder = asyncio.create_task(_search(client))
            await _assert_semaphore_locked(sem)
            start = time.monotonic()
            overflow = await asyncio.wait_for(_search(client), timeout=3.0)
            elapsed = time.monotonic() - start
            holder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await holder

        assert overflow.status_code == 503
        assert overflow.json()["reason"] == "indexer_unavailable"
        assert "gate" in overflow.json()["detail"]
        assert elapsed < 1.0, f"congested gate answered in {elapsed:.2f}s, expected ~1s"


class TestSemaphoreCancelSafety:
    """The permit-accounting the handler leans on, pinned rather than assumed.

    `asyncio.Semaphore` has historically been able to lose a permit outright
    when a waiter is cancelled in the window after it has been granted one but
    before it resumes (gh-90155). CPython 3.12 compensates for that in
    `acquire`'s `except CancelledError` branch — `if not fut.cancelled():
    self._value += 1`. These assert the behaviour on the interpreter actually
    running the gateway, so an interpreter change cannot quietly reopen it.

    The handler does not *depend* on this holding: its deadline covers the
    acquire, so a lost permit degrades searches to 503 instead of parking them
    forever. That is the property the outage needed and did not have.
    """

    async def test_cancel_after_grant_does_not_lose_a_permit(self) -> None:
        sem = asyncio.Semaphore(1)
        await sem.acquire()

        waiter = asyncio.create_task(sem.acquire())
        await asyncio.sleep(0)  # let it park in the waiter deque
        assert sem.locked()

        # Hand the permit over, then cancel before the waiter can resume: the
        # permit is charged to a task that will never use it.
        sem.release()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        # Back in the pool, not stranded on the dead task.
        await asyncio.wait_for(sem.acquire(), timeout=1)

    async def test_cancel_while_parked_does_not_lose_a_permit(self) -> None:
        """The plainer case: cancelled while still waiting, never granted."""
        sem = asyncio.Semaphore(1)
        await sem.acquire()

        waiter = asyncio.create_task(sem.acquire())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        sem.release()
        await asyncio.wait_for(sem.acquire(), timeout=1)


def test_semaphore_sized_from_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The query-embed concurrency gate reads `memory_search_max_concurrency`
    (env AVA_MEMORY_SEARCH_MAX_CONCURRENCY) — a knob, not a hardcoded constant."""
    import gateway.routers.memory as _gw_memory
    from base.config import settings

    monkeypatch.setattr(settings.services, "memory_search_max_concurrency", 7)
    assert _gw_memory.build_search_gate()._value == 7


class TestMemoryNoteEndpoint:
    """GET /api/memory/note — one parsed note by relative path."""

    def test_note_returns_parsed_body_without_frontmatter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The body is the markdown with the YAML frontmatter stripped, and
        frontmatter values arrive as structured fields."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "alpha.md").write_text(
            """---
type: Memory
title: Alpha
description: First note
tags: [ava-internal, tech-ops]
timestamp: '2026-06-18T10:00:00Z'
ava_agent: '7'
ava_machine: test-host
---

# Alpha

Body **with** markdown.
""",
            encoding="utf-8",
        )

        with TestClient(app) as client:
            resp = client.get("/api/memory/note", params={"path": "alpha.md"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["path"] == "alpha.md"
        assert body["title"] == "Alpha"
        assert body["description"] == "First note"
        assert body["tags"] == ["ava-internal", "tech-ops"]
        assert body["timestamp"] == "2026-06-18T10:00:00Z"
        assert body["ava_agent"] == "7"
        assert body["ava_machine"] == "test-host"
        # The parser contract: body starts right after the closing fence
        # (a leading blank line is normal for a note's body).
        assert body["body"] == "\n# Alpha\n\nBody **with** markdown.\n"
        assert "---" not in body["body"]

    def test_note_in_subdirectory(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Nested notes resolve by their relative path (the graph's node path)."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "health").mkdir()
        (tmp_path / "health" / "overview.md").write_text(
            """---
type: Memory
title: Overview
tags: [health]
---

# Body
""",
            encoding="utf-8",
        )

        with TestClient(app) as client:
            resp = client.get("/api/memory/note", params={"path": "health/overview.md"})

        assert resp.status_code == 200
        assert resp.json()["path"] == "health/overview.md"
        assert resp.json()["body"] == "\n# Body\n"

    def test_note_missing_returns_404(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/memory/note", params={"path": "missing.md"})
        assert resp.status_code == 404

    def test_note_traversal_returns_404(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Traversal paths are rejected, not resolved — no filesystem leak."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "secret.md").write_text(
            """---
type: Memory
title: Secret
---

# S
""",
            encoding="utf-8",
        )
        # NOTE: no literal "%2F" in `bad` — httpx percent-encodes it again
        # (%252F), so the server sees a plain filename and the test would pass
        # against any implementation (empty pin). "..%2Fsecret.md" on the wire
        # (from "../secret.md") exercises the real encoded-traversal case.
        for bad in ("../secret.md", "/etc/passwd", "alpha"):
            with TestClient(app) as client:
                resp = client.get("/api/memory/note", params={"path": bad})
            assert resp.status_code == 404, bad

    def test_note_without_frontmatter_is_404(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A .md file that is not a note (no frontmatter) is not a note."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "plain.md").write_text("# Just a heading\n", encoding="utf-8")
        with TestClient(app) as client:
            resp = client.get("/api/memory/note", params={"path": "plain.md"})
        assert resp.status_code == 404

    def test_note_null_byte_path_is_404_not_500(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A path with an embedded null byte must be 404 (QA #1169 F3).

        `Path.resolve()` / `is_file()` raise ValueError on such paths
        (lstat: embedded null character); the endpoint's failure surface must
        map every unresolvable path to 404 rather than escaping as a 500.
        """
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        with TestClient(app) as client:
            resp = client.get("/api/memory/note", params={"path": "ok\x00.md"})
        assert resp.status_code == 404

    def test_note_traversal_slash_rules(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`foo.md/../bar.md` resolves inside the root and is a plain read —
        a non-canonical-but-in-root path is legal, only escapes are 404."""
        import gateway.routers.memory as _gw_memory

        monkeypatch.setattr(_gw_memory, "gateway_memory_dir", lambda: tmp_path)
        (tmp_path / "bar.md").write_text(
            """---
type: Memory
title: Bar
tags: [tech-ops]
---

# Bar
""",
            encoding="utf-8",
        )
        with TestClient(app) as client:
            resp = client.get("/api/memory/note", params={"path": "bar.md/../bar.md"})
        assert resp.status_code == 200
        assert resp.json()["title"] == "Bar"
