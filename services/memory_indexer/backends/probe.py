"""Startup preflight probes — one per backend, one dispatcher (CTO ruling
2026-08-30, direction ②).

The indexer daemon runs the selected backend's probe BEFORE the connect
retry loop. Two outcomes matter:

- `fatal=True` — the backend can never work, no matter how long we retry
  (e.g. the cluster Postgres lacks the pgvector extension binaries). The
  daemon exits immediately with an actionable message instead of a 30s
  retry storm followed by an opaque crash and a 503 search surface.
- `fatal=False` — the backend is not reachable right now, but might be
  booting (`ava start` spawns the storage services just before the
  indexer). The message is logged as a warning and appended to the retry
  loop's terminal error, so the operator still gets the actionable fix
  ("what is missing / how to fix / which backend to switch to") when the
  retries exhaust.

Probes traverse what they certify: a real RPC for milvus, a real GET
/meta for the numpy service — not a bare TCP connect
(`conventions/defensive-patterns.md`).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

import httpx

from shared.config import settings

_PROBE_TIMEOUT_S = 3.0


@dataclass(frozen=True)
class ProbeResult:
    """`message` is the human-actionable failure explanation (None = healthy);
    `fatal` says retrying cannot fix it."""

    message: str | None
    fatal: bool = False


def _probe_milvus() -> ProbeResult:
    """Milvus must answer a real RPC (`list_collections`) at the configured
    URI. Not reachable = transient (the session may still be booting) — the
    retry loop owns the wait; the message rides along for its terminal error."""
    from pymilvus import MilvusClient  # lazy — the probe runs once at daemon boot

    uri = settings.services.milvus_uri
    client: MilvusClient | None = None
    try:
        client = MilvusClient(uri=uri, timeout=_PROBE_TIMEOUT_S)
        client.list_collections()  # pyright: ignore[reportUnknownMemberType, reportUnusedCoroutine]
        return ProbeResult(message=None)
    except Exception as exc:
        return ProbeResult(
            message=(
                f"milvus is not reachable at {uri} ({type(exc).__name__}: {exc}) — "
                "the ava-milvus session may still be booting (`ava start` spawns it "
                "before the indexer); if it stays down, ensure the session is not "
                "disabled, or set AVA_MEMORY_SEARCH_BACKEND=numpy to use the local "
                "exact-search service instead"
            )
        )
    finally:
        if client is not None:
            with suppress(Exception):
                client.close()


def _probe_numpy() -> ProbeResult:
    """The memory_search service must answer a real GET /meta at the
    configured URI. Not reachable = transient (the session may still be
    booting) — same retry semantics as milvus."""
    uri = settings.services.memory_search_uri
    try:
        resp = httpx.get(f"{uri}/meta", timeout=_PROBE_TIMEOUT_S)
        resp.raise_for_status()
        return ProbeResult(message=None)
    except Exception as exc:
        return ProbeResult(
            message=(
                f"memory_search service is not reachable at {uri} "
                f"({type(exc).__name__}: {exc}) — the memory-search session may "
                "still be booting (`ava start` spawns it before the indexer); if "
                "it stays down, ensure the session is not disabled, or set "
                "AVA_MEMORY_SEARCH_BACKEND=milvus (default)"
            )
        )


_PROBES: dict[str, Callable[[], ProbeResult]] = {
    "milvus": _probe_milvus,
    "numpy": _probe_numpy,
}


def probe_backend(name: str) -> ProbeResult:
    """The preflight verdict for backend `name`.

    Unknown names are a fatal verdict too (with the fix): an unrecognized
    `AVA_MEMORY_SEARCH_BACKEND` must not silently fall back to milvus."""
    probe = _PROBES.get(name)
    if probe is None:
        known = ", ".join(sorted(_PROBES))
        return ProbeResult(
            message=(
                f"unknown memory search backend {name!r} (known: {known}) — "
                f"set AVA_MEMORY_SEARCH_BACKEND to one of: {known}"
            ),
            fatal=True,
        )
    return probe()
