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
/meta for the numpy service, a read-only `pg_available_extensions` check
for pgvector — not a bare TCP connect
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


def _probe_pgvector() -> ProbeResult:
    """The cluster Postgres must carry the pgvector extension binaries.

    Missing extension = permanent (fatal): no amount of retrying installs
    binaries, and pgvector is v2 / fallback-only until the vendored-runtime
    provisioning lands (2026-08-29 decision) — the message names the fix and
    the backends that work today. Postgres unreachable = transient (the
    retry loop owns the wait, like milvus / numpy booting). Read-only —
    `pg_available_extensions`, never CREATE EXTENSION (a probe must not
    mutate). Dial bound (QA nit #1012, 2026-08-30): `shared.db.connect()`
    is the sanctioned single entry point for DB dials and carries its own
    resilience kwargs — the connect is bounded by
    `PG_KEEPALIVE_KWARGS["connect_timeout"]` (5s) and the catalog query by
    `PG_STATEMENT_TIMEOUT_KWARGS` (60s), so the probe is bounded even against
    a black-holed peer. `_PROBE_TIMEOUT_S` (3s) intentionally applies only to
    the RPC/HTTP probes (milvus, numpy): it is the probe layer's own timeout,
    while the pgvector probe's bound comes from the DB layer by design, and a
    probe-specific `connect_timeout` knob on `connect()` was deliberately not
    added for a 2s difference in a one-shot boot check."""
    import psycopg

    import shared.db

    try:
        with shared.db.connect() as conn:
            row = conn.execute(
                "SELECT count(*) FROM pg_available_extensions WHERE name = 'vector'"
            ).fetchone()
        if row is not None and int(row[0]) > 0:
            return ProbeResult(message=None)
        return ProbeResult(
            message=(
                "pgvector is not installed in the cluster Postgres — the runtime "
                "Postgres is the vendored relocatable build (~/.ava/runtime/pg/<ver>), "
                "which scripts/provision/database.sh and CI's install-pg-redis do not "
                "cover yet. pgvector is fallback-only / v2 until that provisioning "
                "lands. Use AVA_MEMORY_SEARCH_BACKEND=milvus (default) or numpy "
                "instead"
            ),
            fatal=True,
        )
    except psycopg.OperationalError as exc:
        return ProbeResult(
            message=(
                f"cluster Postgres is not reachable ({type(exc).__name__}: {exc}) — it "
                "may still be booting (`ava start` starts it before the indexer); if it "
                "stays down, check the cluster DB session, or set "
                "AVA_MEMORY_SEARCH_BACKEND=milvus or numpy"
            )
        )
    except Exception as exc:
        return ProbeResult(
            message=(
                f"pgvector preflight could not run against the cluster Postgres "
                f"({type(exc).__name__}: {exc}) — pgvector is fallback-only / v2; set "
                "AVA_MEMORY_SEARCH_BACKEND=milvus or numpy"
            ),
            fatal=True,
        )


_PROBES: dict[str, Callable[[], ProbeResult]] = {
    "milvus": _probe_milvus,
    "numpy": _probe_numpy,
    "pgvector": _probe_pgvector,
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
