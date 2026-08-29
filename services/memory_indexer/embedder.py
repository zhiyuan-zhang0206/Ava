"""Gemini Embedding 2 client — direct REST over httpx.

Single thin wrapper over the Gemini Developer API `batchEmbedContents`
endpoint. Both the indexer daemon (batch embed markdown files) and
`ava.memory.search` (embed query) call into this module.

Deliberately does NOT use the `google-genai` SDK: importing it eagerly
pulls an MCP + ASGI (starlette / uvicorn) + aiohttp stack into the
process (~50 MB resident) for what is a single HTTPS POST. This module
runs inside the always-on memory-indexer daemon, and the same import is
lazily pulled into the gateway on the first `ava.memory.search`, so that
weight is paid for the life of both processes. `httpx` is already a core
dependency. `google-genai` stays installed for `ava._understand` (media)
and `langchain-google-genai` (LLM factory), but nothing on the
indexer / gateway path imports it now.

API key is read from `GEMINI_API_KEY` env — prod injects it via
`~/.ava/.env`.

No module-level httpx client (sync or async): the sync path is
tested by patching `httpx.post`, which a pre-built client would bypass; an
`httpx.AsyncClient` is bound to the event loop it is first used on, and
this module runs inside both the daemon process and the gateway (plus
per-test loops), so a shared client would cross loops and raise. Per-call
construction cost is negligible next to a multi-second network call (task
#971 evaluation).
"""

from __future__ import annotations

from typing import Any

import httpx
import numpy as np

from shared.config import settings
from shared.resilience import ExponentialBackoff, Policy, aretry, http_classifier, retry

_MODEL_ID = "gemini-embedding-2"
DIM = 3072
"""3072-dim default for Gemini Embedding 2 (text mode). Not reduced —
12 KB/file is more than enough for memory pool scale (<10k files)."""

# Gemini Developer API (mldev). The google-genai SDK targets exactly this
# host / version / endpoint for an api-key client; this replicates that
# wire call. `models/<id>` is the resource name the endpoint expects.
_ENDPOINT = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{_MODEL_ID}:batchEmbedContents"
)
# Per-request ceiling is configurable (AVA_EMBED_TIMEOUT_SECONDS, task #698
# G8): a 32-file batch is one round-trip and the daemon's liveness timeout
# (180s) sits well above the default 60s, so a slow-but-legit batch does not
# trip the healthcheck. The retry policies are module constants (R2-D) —
# two of them, split by call site, see `_EMBED_POLICY` / `_QUERY_EMBED_POLICY`.


class EmbeddingAPIError(RuntimeError):
    """Gemini API call still failed after N retries. The daemon skips
    the current file and retries on the next watch event;
    `ava.memory.search` wraps this as `IndexerUnavailable` and raises
    to the caller."""


# R2-D shared retry primitives (idempotent read; http_classifier makes
# 4xx permanent and 429/5xx / transport retryable; Retry-After respected).
# Two policies deliberately split by call site (task #2003/B): the two embed
# paths answer to different masters.
#
# `_EMBED_POLICY` — the indexer daemon's document embeds. The daemon embeds
# once per batch and has no caller deadline, so its resilience IS the retry:
# 4 attempts, backoff 1→2→4→8s.
#
# `_QUERY_EMBED_POLICY` — search query embeds on the gateway. They run inside
# the search endpoint's own deadline (memory_search_deadline_seconds, default
# 15s), so the 4-attempt schedule could spend the whole budget retrying a 429
# the caller (passive recall / an explicit ava.memory.search) would simply
# watch expire — and the caller retries the *search*, not the embed. One
# retry with a 1s backoff still rides out a transient blip, and the caller is
# the one that decides how long a persistent outage is worth waiting for.
_EMBED_POLICY = Policy(
    max_attempts=4,
    backoff=ExponentialBackoff(base=1.0, factor=2.0, cap=8.0),
    classify=http_classifier,
)
_QUERY_EMBED_POLICY = Policy(
    max_attempts=2,
    backoff=ExponentialBackoff(base=1.0, factor=2.0, cap=2.0),
    classify=http_classifier,
)


def _api_key() -> str:
    if settings.lm.gemini_api_key is None:
        raise EmbeddingAPIError(
            "GEMINI_API_KEY not set — add GEMINI_API_KEY=<key> to "
            "`~/.ava/.env`; takes effect after restarting the service "
            "session to reload env."
        )
    return settings.lm.gemini_api_key.get_secret_value()


def _payload(texts: list[str], task_type: str) -> dict[str, Any]:
    """The batchEmbedContents request body — one request per text."""
    return {
        "requests": [
            {
                "model": f"models/{_MODEL_ID}",
                "content": {"parts": [{"text": t}]},
                "taskType": task_type,
                "outputDimensionality": DIM,
            }
            for t in texts
        ]
    }


def _vectors_from_body(body: dict[str, Any], texts: list[str]) -> np.ndarray:
    """Validate the response shape and return (N, DIM) float32."""
    embeddings: list[dict[str, Any]] = body.get("embeddings") or []
    try:
        vectors = np.array([e["values"] for e in embeddings], dtype=np.float32)
    except (KeyError, TypeError, ValueError) as exc:
        raise EmbeddingAPIError(f"malformed embed response: {exc!r}") from exc
    if vectors.shape != (len(texts), DIM):
        raise EmbeddingAPIError(f"unexpected shape {vectors.shape}, expected ({len(texts)}, {DIM})")
    return vectors


def _embed(texts: list[str], task_type: str, *, policy: Policy = _EMBED_POLICY) -> np.ndarray:
    """Single batched `batchEmbedContents` call with retry; returns
    (N, DIM) float32. Raises after retries.

    `task_type` is RETRIEVAL_DOCUMENT or RETRIEVAL_QUERY — Gemini uses
    different internal projections for docs vs queries to improve
    retrieval quality.

    Each text becomes its own request in the batch (`requests[].content`
    = a single text part), so N texts yield N independent embeddings in
    order.

    Retry semantics (R2-D): `policy` is the caller's retry schedule —
    `_EMBED_POLICY` for indexer documents, `_QUERY_EMBED_POLICY` for search
    queries (see those constants). Backoff + jitter; 4xx is permanent
    (fails immediately), 429/5xx and transport failures retry; Retry-After
    is respected.
    """
    if not texts:
        return np.empty((0, DIM), dtype=np.float32)
    headers = {"x-goog-api-key": _api_key()}
    payload = _payload(texts, task_type)
    timeout_s = settings.services.memory_embed_timeout_seconds

    def _call() -> dict[str, Any]:
        response = httpx.post(_ENDPOINT, json=payload, headers=headers, timeout=timeout_s)
        response.raise_for_status()
        return response.json()

    try:
        body = retry(policy)(_call)
    except Exception as exc:
        raise EmbeddingAPIError(
            f"Gemini embed failed after {policy.max_attempts} attempts: {exc!r}"
        ) from exc
    return _vectors_from_body(body, texts)


async def _embed_async(
    texts: list[str], task_type: str, *, policy: Policy = _EMBED_POLICY
) -> np.ndarray:
    """Async twin of ``_embed`` — native non-blocking I/O over
    ``httpx.AsyncClient`` with ``asyncio.sleep`` backoff. Same timeout /
    retry / validation contract. The gateway's ``/api/memory/search`` uses
    this so a slow Gemini API can never freeze the event loop (2026-08-03:
    the sync version blocked the whole gateway for up to ~4.5 min per call,
    causing 13 restarts in 8h). ``policy`` carries the same call-site split
    as ``_embed``: query embeds pass ``_QUERY_EMBED_POLICY``."""
    if not texts:
        return np.empty((0, DIM), dtype=np.float32)
    headers = {"x-goog-api-key": _api_key()}
    payload = _payload(texts, task_type)
    timeout_s = settings.services.memory_embed_timeout_seconds
    try:
        client = httpx.AsyncClient(timeout=timeout_s)
    except Exception as exc:
        # AsyncClient construction can raise on its own (bad timeout config,
        # transport setup) — wrap it to keep the module contract: only
        # EmbeddingAPIError escapes.
        raise EmbeddingAPIError(f"embed HTTP client init failed: {exc!r}") from exc
    try:
        async with client:

            async def _call() -> dict[str, Any]:
                response = await client.post(_ENDPOINT, json=payload, headers=headers)
                response.raise_for_status()
                return response.json()

            body = await aretry(policy)(_call)
    except EmbeddingAPIError:
        raise
    except Exception as exc:
        raise EmbeddingAPIError(
            f"Gemini embed failed after {policy.max_attempts} attempts: {exc!r}"
        ) from exc
    return _vectors_from_body(body, texts)


def embed_documents(texts: list[str]) -> np.ndarray:
    """Batch embed markdown file contents for indexing. (N, DIM) float32."""
    return _embed(texts, task_type="RETRIEVAL_DOCUMENT")


def embed_query(text: str) -> np.ndarray:
    """Embed single query for search. (DIM,) float32."""
    return _embed([text], task_type="RETRIEVAL_QUERY", policy=_QUERY_EMBED_POLICY)[0]


async def embed_query_async(text: str) -> np.ndarray:
    """Async embed of a single query for search. (DIM,) float32."""
    return (await _embed_async([text], task_type="RETRIEVAL_QUERY", policy=_QUERY_EMBED_POLICY))[0]
