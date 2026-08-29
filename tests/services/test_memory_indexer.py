"""Memory indexer unit tests — embedder mock; index / daemon use a session-scoped
milvus-lite standalone server (`milvus_client` fixture in tests/conftest.py).

The milvus-lite standalone server starts in ~3s, one shared per session; tests drop
the collection between them for isolation. Same backing as prod (standalone server), no in-process mixing.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest

from services.memory_indexer import daemon, embedder, index
from services.memory_indexer.backends.milvus import MilvusBackend


def _backend(client: Any) -> MilvusBackend:
    """Wrap the raw milvus fixture client in the backend adapter — the
    daemon now talks to backends, not raw clients."""
    return MilvusBackend(client=client)  # pyright: ignore[reportArgumentType]


# ── embedder (httpx REST mock) ───────────────────────────────────────────


class _FakeResponse:
    """Minimal stand-in for httpx.Response: raise_for_status + json."""

    def __init__(self, payload: dict[str, Any], *, status_ok: bool = True) -> None:
        self._payload = payload
        self._status_ok = status_ok

    def raise_for_status(self) -> None:
        if not self._status_ok:
            raise httpx.HTTPStatusError(
                "500 Server Error",
                request=httpx.Request("POST", embedder._ENDPOINT),
                response=httpx.Response(500),
            )

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakePost:
    """Records each httpx.post call; returns embeddings or raises (in call
    order) to simulate transient failures. `vectors` are the per-text
    embedding rows the batch response carries back."""

    def __init__(
        self,
        vectors: list[list[float]] | None = None,
        *,
        raises_times: int = 0,
        status_ok: bool = True,
    ) -> None:
        self._vectors = vectors or []
        self._raises_remaining = raises_times
        self._status_ok = status_ok
        self.call_count = 0
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> _FakeResponse:
        self.call_count += 1
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if self._raises_remaining > 0:
            self._raises_remaining -= 1
            raise httpx.ConnectError("simulated network failure")
        embeddings = [{"values": v} for v in self._vectors]
        return _FakeResponse({"embeddings": embeddings}, status_ok=self._status_ok)


def _patch_post(monkeypatch: pytest.MonkeyPatch, fake: _FakePost) -> None:
    monkeypatch.setattr(httpx, "post", fake)


@pytest.fixture(autouse=True)
def _dummy_gemini_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a key so `_api_key()` does not short-circuit the embedder.

    These tests mock the HTTP call (`httpx.post`), not auth — the key
    check runs before it, so without a key every success-path test would
    raise `EmbeddingAPIError` instead of exercising the request. CI has no
    GEMINI_API_KEY; running locally the prod `.env` leaked one in and hid
    the dependency. `test_embed_no_api_key_raises` overrides this back to
    None to keep the missing-key branch a tested behavior.
    """
    from pydantic import SecretStr

    from shared.config import settings

    monkeypatch.setattr(settings.lm, "gemini_api_key", SecretStr("test-gemini-key"))


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize shared.resilience backoff sleeps so retry-path tests do
    not hang; the retry loop itself is still exercised (call counts). The
    embedder's policy is a module constant (R2-D), no longer settings-driven.
    `_asleep` must be a REAL coroutine function: `aretry` awaits it, so a sync
    lambda turns every async retry into `TypeError: object NoneType can't be
    used in 'await' expression`."""
    monkeypatch.setattr("shared.resilience._sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

    async def _no_asleep(_s: float) -> None:
        return None

    monkeypatch.setattr("shared.resilience._asleep", _no_asleep)


@pytest.fixture(autouse=True)
def _watched_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the daemon's watched root at this test's sandbox.

    `_process_paths` now prunes any path outside the watched root (stale
    authoring-checkout leftovers), so tests that embed files under
    `tmp_path` must make `tmp_path` the root — otherwise every file they
    write would count as foreign and be deleted.
    """
    monkeypatch.setattr(daemon, "_MEMORY_ROOT", tmp_path)


def test_embed_documents_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePost(vectors=[[1.0] * embedder.DIM, [2.0] * embedder.DIM])
    _patch_post(monkeypatch, fake)
    result = embedder.embed_documents(["text1", "text2"])
    assert result.shape == (2, embedder.DIM)
    assert result.dtype == np.float32
    body = fake.calls[-1]["json"]
    assert [r["taskType"] for r in body["requests"]] == ["RETRIEVAL_DOCUMENT"] * 2


def test_embed_query_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePost(vectors=[[1.0] * embedder.DIM])
    _patch_post(monkeypatch, fake)
    result = embedder.embed_query("hello")
    assert result.shape == (embedder.DIM,)
    body = fake.calls[-1]["json"]
    assert body["requests"][0]["taskType"] == "RETRIEVAL_QUERY"


def test_embed_request_payload_and_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire contract: endpoint, api-key header, per-text request shape."""
    fake = _FakePost(vectors=[[1.0] * embedder.DIM])
    _patch_post(monkeypatch, fake)
    embedder.embed_documents(["hello world"])
    call = fake.calls[-1]
    assert call["url"] == embedder._ENDPOINT
    assert call["headers"]["x-goog-api-key"]  # non-empty key forwarded
    req = call["json"]["requests"][0]
    assert req["model"] == f"models/{embedder._MODEL_ID}"
    assert req["outputDimensionality"] == embedder.DIM
    assert req["content"]["parts"][0]["text"] == "hello world"


def test_embed_documents_empty_short_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePost(vectors=[])
    _patch_post(monkeypatch, fake)
    result = embedder.embed_documents([])
    assert result.shape == (0, embedder.DIM)
    assert fake.call_count == 0


def test_embed_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePost(vectors=[[1.0] * embedder.DIM], raises_times=2)
    _patch_post(monkeypatch, fake)
    result = embedder.embed_documents(["hello"])
    assert result.shape == (1, embedder.DIM)
    assert fake.call_count == 3


def test_embed_raises_after_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePost(vectors=[[1.0] * embedder.DIM], raises_times=100)
    _patch_post(monkeypatch, fake)
    with pytest.raises(embedder.EmbeddingAPIError, match="failed after"):
        embedder.embed_documents(["hello"])


def test_embed_http_error_status_retries_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-2xx (raise_for_status) is a retryable failure; exhausting retries raises."""
    fake = _FakePost(vectors=[[1.0] * embedder.DIM], status_ok=False)
    _patch_post(monkeypatch, fake)
    with pytest.raises(embedder.EmbeddingAPIError, match="failed after"):
        embedder.embed_query("hello")

    assert fake.call_count == embedder._QUERY_EMBED_POLICY.max_attempts


def test_query_embed_policy_is_lighter_than_document_policy() -> None:
    """Query embeds and indexer document embeds answer to different masters
    (task #2003/B): a search query sits inside the gateway's own search
    deadline, so the indexer's 4-attempt schedule (1->2->4->8s) could spend
    the whole budget retrying a 429 the caller watches expire — the caller
    retries the *search*, the daemon has no caller and needs the resilience.
    Locks the split, so a future consolidation cannot quietly reunite them."""
    assert embedder._QUERY_EMBED_POLICY.max_attempts < embedder._EMBED_POLICY.max_attempts


def test_embed_query_async_uses_the_query_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway's async query embed retries on the lighter query schedule
    (2 attempts), not the indexer's 4: a 429 during a fleet wake must not burn
    the search deadline on retries."""

    class _FailingClient:
        """AsyncClient stand-in whose POST always raises a transient error.

        A class-level counter: the client instance is constructed inside
        `embed_query_async`, so the test cannot reach the instance — and a
        class (not a lambda) keeps pyright happy about `AsyncClient`'s type.
        """

        post_calls: int = 0

        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _FailingClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, *args: object, **kwargs: object) -> None:
            _FailingClient.post_calls += 1
            raise httpx.ConnectError("simulated network failure")

    monkeypatch.setattr(httpx, "AsyncClient", _FailingClient)
    with pytest.raises(embedder.EmbeddingAPIError, match="failed after 2 attempts"):
        asyncio.run(embedder.embed_query_async("hello"))
    assert _FailingClient.post_calls == embedder._QUERY_EMBED_POLICY.max_attempts


def test_embed_4xx_fails_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deterministic 4xx is permanent: one attempt, then EmbeddingAPIError
    (R2-D classify; the pre-R2 loop wasted its whole budget retrying 400/403 —
    audit 06 Q4)."""

    class _FourHundred:
        def raise_for_status(self) -> None:
            raise httpx.HTTPStatusError(
                "400 Bad Request",
                request=httpx.Request("POST", embedder._ENDPOINT),
                response=httpx.Response(400),
            )

        def json(  # pyright: ignore[reportUnknownParameterType]
            self,
        ) -> dict:  # pragma: no cover — never reached  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
            return {}  # pyright: ignore[reportUnknownVariableType]

    calls: list[str] = []

    def _post(url: str, *, json: dict, headers: dict, timeout: float) -> _FourHundred:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
        calls.append(url)
        return _FourHundred()

    monkeypatch.setattr(httpx, "post", _post)  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(embedder.EmbeddingAPIError, match="failed after"):
        embedder.embed_query("hello")
    assert len(calls) == 1  # 4xx → permanent → single attempt


def test_embed_timeout_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-request timeout comes from config (AVA_EMBED_TIMEOUT_SECONDS,
    task #698 G8); the retry policy is a module constant (R2-D)."""
    from shared.config import settings

    monkeypatch.setattr(settings.services, "memory_embed_timeout_seconds", 12.5)
    fake = _FakePost(vectors=[[1.0] * embedder.DIM], raises_times=1)
    _patch_post(monkeypatch, fake)

    embedder.embed_documents(["hello"])

    assert fake.call_count == 2  # 1 failure + 1 retry
    for call in fake.calls:
        assert call["timeout"] == 12.5


def test_embed_no_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing key -> EmbeddingAPIError with actionable guidance, before any POST.

    Overrides the autouse dummy key back to None so this branch stays a
    tested behavior even though every other embedder test injects a key.
    """
    from shared.config import settings

    monkeypatch.setattr(settings.lm, "gemini_api_key", None)
    # No key must mean no network attempt — this makes that observable.
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *_a, **_k: pytest.fail("must not POST without an API key"),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    with pytest.raises(embedder.EmbeddingAPIError) as exc_info:
        embedder.embed_query("hello")
    message = str(exc_info.value)
    assert "GEMINI_API_KEY" in message
    assert ".env" in message  # actionable: points the operator at where to set it


def test_embed_async_client_construction_failure_wraps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AsyncClient construction errors wrap into EmbeddingAPIError (#971).

    The client is built outside the retry loop; if construction itself
    fails (bad timeout config, transport setup), the module contract
    still holds: only EmbeddingAPIError escapes.
    """

    class _Boom:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise httpx.ConnectError("client init failed")

    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    with pytest.raises(embedder.EmbeddingAPIError, match="client init failed"):
        asyncio.run(embedder.embed_query_async("hello"))


def test_embed_response_shape_mismatch_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePost(vectors=[[1.0] * embedder.DIM] * 3)
    _patch_post(monkeypatch, fake)
    with pytest.raises(embedder.EmbeddingAPIError, match="unexpected shape"):
        embedder.embed_documents(["a", "b"])


def test_embed_malformed_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Embedding entries missing `values` -> EmbeddingAPIError, not a crash."""

    def bad_post(url: str, *, json: Any, headers: Any, timeout: float) -> _FakeResponse:
        return _FakeResponse({"embeddings": [{"nope": []}]})

    monkeypatch.setattr(httpx, "post", bad_post)
    with pytest.raises(embedder.EmbeddingAPIError, match="malformed"):
        embedder.embed_query("hello")


def test_content_hash_deterministic() -> None:
    assert index.content_hash("hello") == index.content_hash("hello")
    assert index.content_hash("hello") != index.content_hash("world")


# ── index (milvus standalone server backed) ──────────────────────────────


def _vec(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(embedder.DIM).astype(np.float32)


def test_index_connect_creates_collection(milvus_client) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """connect() idempotent: collection exists, not recreated; _COLLECTION name matches."""
    assert milvus_client.has_collection(index._COLLECTION)  # pyright: ignore[reportUnknownMemberType]


def test_index_upsert_then_all_meta(milvus_client) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    index.upsert(milvus_client, "/a/b.md", 1.0, "hash1", _vec(0))  # pyright: ignore[reportUnknownArgumentType]
    index.upsert(milvus_client, "/c/d.md", 2.0, "hash2", _vec(1))  # pyright: ignore[reportUnknownArgumentType]
    meta = index.all_meta(milvus_client)  # pyright: ignore[reportUnknownArgumentType]
    assert meta == {"/a/b.md": (1.0, "hash1"), "/c/d.md": (2.0, "hash2")}


def test_index_upsert_overwrite(milvus_client) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """Same path = update (by primary key path), not insert."""
    index.upsert(milvus_client, "/a.md", 1.0, "hash1", _vec(0))  # pyright: ignore[reportUnknownArgumentType]
    index.upsert(milvus_client, "/a.md", 2.0, "hash2", _vec(1))  # pyright: ignore[reportUnknownArgumentType]
    meta = index.all_meta(milvus_client)  # pyright: ignore[reportUnknownArgumentType]
    assert meta == {"/a.md": (2.0, "hash2")}


def test_index_delete(milvus_client) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    index.upsert(milvus_client, "/a.md", 1.0, "h", _vec(0))  # pyright: ignore[reportUnknownArgumentType]
    index.delete(milvus_client, "/a.md")  # pyright: ignore[reportUnknownArgumentType]
    assert index.all_meta(milvus_client) == {}  # pyright: ignore[reportUnknownArgumentType]


def test_index_delete_missing_noop(milvus_client) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    index.delete(
        milvus_client,  # pyright: ignore[reportUnknownArgumentType]
        "/never_existed.md",  # pyright: ignore[reportUnknownArgumentType]
    )  # no raise  # pyright: ignore[reportUnknownArgumentType]


def test_index_search_topk_returns_sorted_by_cosine(milvus_client) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """Insert 3 known vectors, query matches one of them, that path ranks first."""
    target = np.ones(embedder.DIM, dtype=np.float32)
    orthogonal = np.zeros(embedder.DIM, dtype=np.float32)
    orthogonal[0] = 1.0
    opposite = -np.ones(embedder.DIM, dtype=np.float32)

    index.upsert(milvus_client, "/target.md", 1.0, "h1", target)  # pyright: ignore[reportUnknownArgumentType]
    index.upsert(milvus_client, "/orthogonal.md", 2.0, "h2", orthogonal)  # pyright: ignore[reportUnknownArgumentType]
    index.upsert(milvus_client, "/opposite.md", 3.0, "h3", opposite)  # pyright: ignore[reportUnknownArgumentType]

    results = index.search_topk(milvus_client, target, k=3)  # pyright: ignore[reportUnknownArgumentType]
    assert results[0] == "/target.md"
    assert results[-1] == "/opposite.md"


def test_index_search_topk_empty_collection(milvus_client) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    results = index.search_topk(milvus_client, _vec(0), k=5)  # pyright: ignore[reportUnknownArgumentType]
    assert results == []


def test_index_search_topk_respects_k(milvus_client) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    for i in range(10):
        index.upsert(milvus_client, f"/{i}.md", float(i), f"h{i}", _vec(i))  # pyright: ignore[reportUnknownArgumentType]
    results = index.search_topk(milvus_client, _vec(0), k=3)  # pyright: ignore[reportUnknownArgumentType]
    assert len(results) == 3


# ── daemon helpers ──────────────────────────────────────────────────────


def test_scan_disk_finds_md_only(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.md").write_text("c")

    result = daemon._scan_disk(tmp_path)
    assert set(result.keys()) == {(tmp_path / "a.md").resolve(), (sub / "c.md").resolve()}


def test_scan_disk_skips_symlinks(tmp_path: Path) -> None:
    real = tmp_path / "real.md"
    real.write_text("a")
    link = tmp_path / "link.md"
    link.symlink_to(real)

    result = daemon._scan_disk(tmp_path)
    assert link.resolve() not in result or set(result.keys()) == {real.resolve()}


def test_scan_disk_missing_root_returns_empty(tmp_path: Path) -> None:
    result = daemon._scan_disk(tmp_path / "nonexistent")
    assert result == {}


def test_process_paths_embeds_new_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    milvus_client,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
) -> None:
    f1 = tmp_path / "a.md"
    f1.write_text("content A")
    f2 = tmp_path / "b.md"
    f2.write_text("content B")

    def fake_embed(texts: list[str]) -> np.ndarray:
        return np.array([[float(len(t))] * embedder.DIM for t in texts], dtype=np.float32)

    monkeypatch.setattr(embedder, "embed_documents", fake_embed)
    daemon._process_paths(_backend(milvus_client), {f1.resolve(), f2.resolve()})
    meta = index.all_meta(milvus_client)  # pyright: ignore[reportUnknownArgumentType]
    assert str(f1.resolve()) in meta
    assert str(f2.resolve()) in meta


def test_process_paths_skips_unchanged_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    milvus_client,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
) -> None:
    f = tmp_path / "a.md"
    f.write_text("content")

    call_count = 0

    def fake_embed(texts: list[str]) -> np.ndarray:
        nonlocal call_count
        call_count += 1
        return np.array([[1.0] * embedder.DIM for _ in texts], dtype=np.float32)

    monkeypatch.setattr(embedder, "embed_documents", fake_embed)
    daemon._process_paths(_backend(milvus_client), {f.resolve()})
    assert call_count == 1

    daemon._process_paths(_backend(milvus_client), {f.resolve()})
    assert call_count == 1  # hash unchanged, no re-embed


def test_process_paths_deletes_missing_files(tmp_path: Path, milvus_client) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """Path enters dirty set but file not on disk — index row is deleted."""
    ghost = str(
        tmp_path / "ghost_nonexistent.md"
    )  # under tmp_path, definitely does not exist (never written)
    index.upsert(milvus_client, ghost, 1.0, "h", _vec(0))  # pyright: ignore[reportUnknownArgumentType]
    daemon._process_paths(_backend(milvus_client), {Path(ghost)})
    assert index.all_meta(milvus_client) == {}  # pyright: ignore[reportUnknownArgumentType]


def test_event_handler_pushes_md_paths_only() -> None:
    import queue as q

    dirty: q.Queue[Path] = q.Queue()
    handler = daemon._MarkdownEventHandler(dirty)

    class E:
        def __init__(self, src: str, *, is_dir: bool = False) -> None:
            self.src_path = src
            self.is_directory = is_dir

    handler.on_created(E("/a.md"))  # type: ignore[arg-type]
    handler.on_modified(E("/b.txt"))  # type: ignore[arg-type]
    handler.on_deleted(E("/c.md"))  # type: ignore[arg-type]
    handler.on_created(E("/dir", is_dir=True))  # type: ignore[arg-type]

    pushed = []
    while not dirty.empty():
        pushed.append(dirty.get_nowait())  # pyright: ignore[reportUnknownMemberType]
    assert pushed == [Path("/a.md"), Path("/c.md")]


def test_event_handler_on_moved_pushes_both_ends() -> None:
    import queue as q

    dirty: q.Queue[Path] = q.Queue()
    handler = daemon._MarkdownEventHandler(dirty)

    class MoveEvent:
        src_path = "/old.md"
        dest_path = "/new.md"
        is_directory = False

    handler.on_moved(MoveEvent())  # type: ignore[arg-type]
    pushed = []
    while not dirty.empty():
        pushed.append(dirty.get_nowait())  # pyright: ignore[reportUnknownMemberType]
    assert pushed == [Path("/old.md"), Path("/new.md")]


def test_process_paths_deletes_foreign_paths_even_when_file_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    milvus_client,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
) -> None:
    """Rows outside the watched root are pruned even when the file still
    exists on disk — the stale authoring-checkout leftovers that surface
    as duplicate search hits (e.g. user-profile.md ×2)."""
    watched = tmp_path / "watched"
    watched.mkdir()
    monkeypatch.setattr(daemon, "_MEMORY_ROOT", watched)

    foreign_dir = tmp_path / "foreign"
    foreign_dir.mkdir()
    foreign = foreign_dir / "note.md"  # exists on disk, outside watched root
    foreign.write_text("content")

    index.upsert(milvus_client, str(foreign), 1.0, "h", _vec(0))  # pyright: ignore[reportUnknownArgumentType]
    daemon._process_paths(_backend(milvus_client), {foreign})
    assert index.all_meta(milvus_client) == {}  # pyright: ignore[reportUnknownArgumentType]


def test_cold_start_reconcile_prunes_foreign_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    milvus_client,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
) -> None:
    """Cold-start reconcile deletes rows whose path is outside the watched
    root — the durable fix for the 11 stale authoring-checkout entries."""
    root = tmp_path / "watched"
    root.mkdir()
    watched = root / "a.md"
    watched.write_text("watched content")
    foreign = tmp_path / "foreign.md"  # exists on disk, outside root
    foreign.write_text("foreign content")

    monkeypatch.setattr(daemon, "_MEMORY_ROOT", root)
    # Both rows pre-exist in the index (e.g. from an era before the
    # gateway-checkout split).
    index.upsert(milvus_client, str(watched.resolve()), 1.0, "h", _vec(0))  # pyright: ignore[reportUnknownArgumentType]
    index.upsert(milvus_client, str(foreign.resolve()), 1.0, "h", _vec(0))  # pyright: ignore[reportUnknownArgumentType]

    def fake_embed(texts: list[str]) -> np.ndarray:
        return np.array([[1.0] * embedder.DIM for _ in texts], dtype=np.float32)

    monkeypatch.setattr(embedder, "embed_documents", fake_embed)
    daemon._cold_start_reconcile(_backend(milvus_client))
    meta = index.all_meta(milvus_client)  # pyright: ignore[reportUnknownArgumentType]
    assert str(foreign.resolve()) not in meta
    assert str(watched.resolve()) in meta


def test_refresh_gateway_checkout_fast_forwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Safety net: pulls origin/main into the gateway checkout and logs
    loudly when HEAD moved (a post-merge refresh was missed)."""
    import logging

    subprocess.run(  # noqa: S603 — fixed argv, test sandbox
        ["git", "init", "-q", str(tmp_path)], check=True
    )
    subprocess.run(  # noqa: S603 — fixed argv, test sandbox
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True
    )
    subprocess.run(  # noqa: S603 — fixed argv, test sandbox
        ["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True
    )
    (tmp_path / "a.md").write_text("x")
    subprocess.run(  # noqa: S603 — fixed argv, test sandbox
        ["git", "-C", str(tmp_path), "add", "-A"], check=True
    )
    subprocess.run(  # noqa: S603 — fixed argv, test sandbox
        ["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True
    )
    before = subprocess.check_output(  # noqa: S603 — fixed argv, test sandbox
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()

    from shared import memory_repo

    monkeypatch.setattr(memory_repo, "gateway_memory_dir", lambda: tmp_path)
    monkeypatch.setattr(memory_repo, "pull_main", lambda: "abc1234")

    with caplog.at_level(logging.INFO, logger="services.memory_indexer.daemon"):
        daemon._refresh_gateway_checkout()
    assert "fast-forwarded" in caplog.text
    assert before != "abc1234"


def test_refresh_gateway_checkout_failure_logs_and_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed pull is logged at ERROR and never raised — the drain loop
    retries next cycle instead of letting the daemon die."""
    import logging

    from shared import memory_repo

    monkeypatch.setattr(memory_repo, "gateway_memory_dir", lambda: tmp_path)

    def _boom() -> str:
        raise RuntimeError("network down")

    monkeypatch.setattr(memory_repo, "pull_main", _boom)

    with caplog.at_level(logging.ERROR, logger="services.memory_indexer.daemon"):
        daemon._refresh_gateway_checkout()  # must not raise
    assert "refresh failed" in caplog.text


# ── chunk splitting (recall-v2: description + body chunks) ───────────────


def test_split_note_extracts_description_and_body() -> None:
    content = (
        "---\ntype: Memory\ndescription: A note about the user's health\n"
        "---\n\n# Health\n\nbody content"
    )
    desc, body = daemon._split_note(content)
    assert desc == "A note about the user's health"
    assert "# Health" in body
    assert "description" not in body


def test_split_note_no_frontmatter_returns_full_body() -> None:
    content = "# Just a heading\n\nNo YAML."
    desc, body = daemon._split_note(content)
    assert desc is None
    assert body == content


def test_split_note_blank_description_is_none() -> None:
    desc, body = daemon._split_note("---\ndescription: \n---\n\nbody")
    assert desc is None
    assert body == "\nbody"  # the shared parser keeps the blank line after the fence


def test_chunk_body_short_text_single_chunk() -> None:
    assert daemon._chunk_body("short body") == ["short body"]
    assert daemon._chunk_body("  \n\n  ") == []


def test_chunk_body_splits_at_paragraph_boundaries() -> None:
    paras = [f"paragraph-{i} " + "word " * 80 for i in range(6)]  # 412 chars each
    body = "\n\n".join(paras)
    chunks = daemon._chunk_body(body, max_chars=1800, overlap_chars=200)
    assert len(chunks) == 2
    assert all(len(c) <= 1800 for c in chunks)
    assert chunks[0].startswith("paragraph-0")
    assert chunks[1].startswith("paragraph-4")  # 4×412 = 1654 fits; 5×412 would not
    # every paragraph survives whole (chunking strips paragraph whitespace)
    assert all(p.strip() in "".join(chunks) for p in paras)


def test_chunk_body_overlap_carries_trailing_paragraphs() -> None:
    paras = [f"paragraph-{i} " + "word " * 80 for i in range(6)]
    body = "\n\n".join(paras)
    chunks = daemon._chunk_body(body, max_chars=1400, overlap_chars=600)
    # the previous chunk's tail paragraph re-opens the next chunk
    assert chunks[1].startswith("paragraph-2")
    assert "paragraph-2" in chunks[0]


def test_chunk_body_hard_splits_oversized_paragraph() -> None:
    para = "x" * 3000
    chunks = daemon._chunk_body(para, max_chars=1000, overlap_chars=100)
    assert chunks == ["x" * 1000, "x" * 1000, "x" * 1000, "x" * 300]


def test_file_rows_desc_plus_body_chunks() -> None:
    content = "---\ntype: Memory\ndescription: hand off to 402\n---\n\n" + "\n\n".join(
        f"paragraph-{i} " + "word " * 80 for i in range(6)
    )
    rows = daemon._file_rows(content)
    assert rows[0] == ("desc", 0, "hand off to 402")
    assert [k for k, _, _ in rows] == ["desc", "body", "body"]


def test_file_rows_no_frontmatter_no_desc() -> None:
    assert daemon._file_rows("# heading\n\nshort body") == [("body", 0, "# heading\n\nshort body")]


def test_process_paths_indexes_desc_and_body_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    milvus_client,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
) -> None:
    """One file with a description + a long body lands as 1 desc row + N body
    chunks; all_meta still reports the file once."""
    f = tmp_path / "long.md"
    f.write_text(
        "---\ntype: Memory\ndescription: hand off to 402\n---\n\n"
        + "\n\n".join(f"paragraph-{i} " + "word " * 80 for i in range(6)),
        encoding="utf-8",
    )

    def fake_embed(texts: list[str]) -> np.ndarray:
        return np.array([[float(len(t))] * embedder.DIM for t in texts], dtype=np.float32)

    monkeypatch.setattr(embedder, "embed_documents", fake_embed)
    daemon._process_paths(_backend(milvus_client), {f.resolve()})
    assert str(f.resolve()) in index.all_meta(milvus_client)  # pyright: ignore[reportUnknownArgumentType]
    rows = milvus_client.query(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        collection_name=index._COLLECTION,
        filter=f'path == "{f.resolve()!s}"',
        output_fields=["kind", "chunk_idx"],
        limit=100,
    )
    kinds = sorted((r["kind"], r["chunk_idx"]) for r in rows)  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
    assert kinds == [("body", 0), ("body", 1), ("desc", 0)]


# ── chunk-aware index (recall-v2: aggregation, delete, migration) ────────


def test_upsert_chunk_rows_meta_stays_per_path(milvus_client) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    for idx in range(3):
        index.upsert(milvus_client, "/a.md", 1.0, "ha", _vec(idx), kind="body", chunk_idx=idx)  # pyright: ignore[reportUnknownArgumentType]
    index.upsert(milvus_client, "/a.md", 1.0, "ha", _vec(9), kind="desc", chunk_idx=0)  # pyright: ignore[reportUnknownArgumentType]
    index.upsert(milvus_client, "/b.md", 2.0, "hb", _vec(0), kind="body", chunk_idx=0)  # pyright: ignore[reportUnknownArgumentType]
    assert index.all_meta(milvus_client) == {  # pyright: ignore[reportUnknownArgumentType]
        "/a.md": (1.0, "ha"),
        "/b.md": (2.0, "hb"),
    }


def test_search_topk_aggregates_chunks_by_path(milvus_client) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """Chunk rows of one path collapse to a single hit; the best chunk per
    path decides the rank."""
    ones = np.ones(embedder.DIM, dtype=np.float32)
    index.upsert(milvus_client, "/a.md", 1.0, "ha", ones, kind="body", chunk_idx=0)  # pyright: ignore[reportUnknownArgumentType]
    index.upsert(milvus_client, "/a.md", 1.0, "ha", 0.9 * ones, kind="body", chunk_idx=1)  # pyright: ignore[reportUnknownArgumentType]
    index.upsert(milvus_client, "/a.md", 1.0, "ha", 0.5 * ones, kind="body", chunk_idx=2)  # pyright: ignore[reportUnknownArgumentType]
    index.upsert(milvus_client, "/b.md", 2.0, "hb", -ones, kind="body", chunk_idx=0)  # pyright: ignore[reportUnknownArgumentType]
    results = index.search_topk(milvus_client, ones, k=5)  # pyright: ignore[reportUnknownArgumentType]
    assert results == ["/a.md", "/b.md"]  # no duplicate paths


def test_delete_removes_all_chunks_of_path(milvus_client) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    for idx in range(3):
        index.upsert(milvus_client, "/a.md", 1.0, "ha", _vec(idx), kind="body", chunk_idx=idx)  # pyright: ignore[reportUnknownArgumentType]
    index.upsert(milvus_client, "/a.md", 1.0, "ha", _vec(9), kind="desc", chunk_idx=0)  # pyright: ignore[reportUnknownArgumentType]
    index.upsert(milvus_client, "/b.md", 2.0, "hb", _vec(0), kind="body", chunk_idx=0)  # pyright: ignore[reportUnknownArgumentType]
    index.delete(milvus_client, "/a.md")  # pyright: ignore[reportUnknownArgumentType]
    assert index.all_meta(milvus_client) == {"/b.md": (2.0, "hb")}  # pyright: ignore[reportUnknownArgumentType]
    assert index.search_topk(milvus_client, _vec(0), k=5) == ["/b.md"]  # pyright: ignore[reportUnknownArgumentType]


def test_connect_migrates_legacy_schema(milvus_client) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """A legacy single-row-per-file collection (path PK, no kind/chunk_idx)
    is dropped and recreated by connect(); its rows are gone — cold-start
    rebuilds them chunked."""
    from pymilvus import DataType

    client = milvus_client  # pyright: ignore[reportUnknownVariableType]
    client.drop_collection(index._COLLECTION)  # pyright: ignore[reportUnknownMemberType]
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    schema.add_field("path", DataType.VARCHAR, is_primary=True, max_length=1024)  # pyright: ignore[reportUnknownMemberType]
    schema.add_field("mtime", DataType.DOUBLE)  # pyright: ignore[reportUnknownMemberType]
    schema.add_field("content_hash", DataType.VARCHAR, max_length=128)  # pyright: ignore[reportUnknownMemberType]
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=embedder.DIM)  # pyright: ignore[reportUnknownMemberType]
    legacy_idx = client.prepare_index_params()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    legacy_idx.add_index(field_name="vector", metric_type="COSINE", index_type="AUTOINDEX")  # pyright: ignore[reportUnknownMemberType]
    client.create_collection(  # pyright: ignore[reportUnknownMemberType]
        collection_name=index._COLLECTION, schema=schema, index_params=legacy_idx
    )
    client.insert(  # pyright: ignore[reportUnknownMemberType]
        collection_name=index._COLLECTION,
        data=[
            {
                "path": "/old.md",
                "mtime": 1.0,
                "content_hash": "h",
                "vector": [0.0] * embedder.DIM,
            }
        ],
    )

    new_client = index.connect()
    try:
        info = new_client.describe_collection(collection_name=index._COLLECTION)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        fields = info["fields"] if isinstance(info, dict) else getattr(info, "fields", [])  # pyright: ignore[reportUnknownArgumentType]
        names = {f["name"] for f in fields}
        assert names >= index._EXPECTED_FIELDS
        assert index.all_meta(new_client) == {}  # legacy row dropped with the collection
    finally:
        new_client.close()


def test_schema_current_detects_legacy_layout(milvus_client) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """_schema_current is True on the chunked schema, False on the legacy one."""
    assert index._schema_current(milvus_client)  # pyright: ignore[reportUnknownArgumentType]

    from pymilvus import DataType

    client = milvus_client  # pyright: ignore[reportUnknownVariableType]
    client.drop_collection(index._COLLECTION)  # pyright: ignore[reportUnknownMemberType]
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    schema.add_field("path", DataType.VARCHAR, is_primary=True, max_length=1024)  # pyright: ignore[reportUnknownMemberType]
    schema.add_field("mtime", DataType.DOUBLE)  # pyright: ignore[reportUnknownMemberType]
    schema.add_field("content_hash", DataType.VARCHAR, max_length=128)  # pyright: ignore[reportUnknownMemberType]
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=embedder.DIM)  # pyright: ignore[reportUnknownMemberType]
    idx = client.prepare_index_params()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    idx.add_index(field_name="vector", metric_type="COSINE", index_type="AUTOINDEX")  # pyright: ignore[reportUnknownMemberType]
    client.create_collection(collection_name=index._COLLECTION, schema=schema, index_params=idx)  # pyright: ignore[reportUnknownMemberType]
    assert not index._schema_current(client)  # pyright: ignore[reportUnknownArgumentType]
