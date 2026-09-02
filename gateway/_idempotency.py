"""AtLeastOnceWithKey dedup middleware — doorplate ① server side (R3).

Routes that declare `Idempotency.AT_LEAST_ONCE_WITH_KEY` promise a keyed
exactly-once effect. Most routes implement that by storing/replaying the first
successful response in `api_idempotency`. A route marked
`transactional_idempotency` instead owns the key in its business transaction
and bypasses this middleware: message delivery stores `client_message_id` on
the inbound row and retries return its stable inbound id. Its delivery-time
status is allowed to reflect current state rather than replaying old bytes.

Concurrency: the first request INSERTs a placeholder row (status NULL =
executing); a same-key retry that finds the placeholder polls until the
owner completes, then replays. A non-2xx outcome deletes the row so a later
retry executes afresh (a transient 5xx is not a result worth replaying —
the client will retry and must get a real execution). Rows live 7 days
(far beyond any retry window) and are pruned opportunistically on each
claim.

Failure containment: a placeholder whose owner died mid-execution (status
NULL past the retention window) is pruned with the same 7-day sweep and
stolen by the next claim, so no key can brick forever; every failure path
between claim and store releases the row. The claim is scoped to
(method, path): a same key reused on a different route is treated as
absent and re-claimed, so two endpoints sharing a key never replay each
other's responses.

The middleware only engages for non-transactional routes whose contract
declares AT_LEAST_ONCE_WITH_KEY **and** requests that actually carry an
Idempotency-Key header — everything else passes through untouched.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from psycopg_pool import ConnectionPool

from gateway.error_envelope import error_response
from shared import contracts
from shared.contracts import Idempotency
from shared.db_transaction import write_transaction

_MAX_WAIT_SECONDS = 15.0
_POLL_INITIAL_S = 0.1
_POLL_FACTOR = 1.75
_POLL_MAX_S = 1.0
_RETENTION_DAYS = 7


def _monotonic() -> float:
    """Follower-wait clock seam that leaves asyncio's own scheduler untouched."""
    return time.monotonic()


# ── DB helpers (synchronous; callers wrap with asyncio.to_thread) ──────


def _claim(pool: ConnectionPool, key: str, method: str, path: str) -> bool:
    """Try to claim `key` for execution. True = this request executes;
    False = another request with the same key owns it (or already
    completed and left a row).

    Also prunes expired rows opportunistically — one cheap DELETE per
    claim keeps the table bounded without a maintenance daemon. The sweep
    covers BOTH completed rows and placeholders (status NULL) past the
    retention window — a row whose owner died mid-execution must not keep
    its key bricked forever (its `completed_at` is NULL, so a
    completed-only predicate would never touch it).

    A conflict is re-claimed (row overwritten) exactly when the stored row
    is not a live completed outcome for THIS route: a different method/path
    under the same key (the client reused the key across endpoints — treat
    as absent, per the key-scoping contract) or a placeholder past the
    retention window (stolen from its dead owner). A live placeholder
    (status NULL, fresh) or a completed same-route row is left alone —
    those mean "someone is executing" / "replay this", respectively.
    """
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM api_idempotency "
            "WHERE completed_at < now() - make_interval(days => %s) "
            "OR (status IS NULL AND created_at < now() - make_interval(days => %s))",
            (_RETENTION_DAYS, _RETENTION_DAYS),
        )
        cur.execute(
            "INSERT INTO api_idempotency (key, method, path) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (key) DO UPDATE SET method = EXCLUDED.method, "
            "    path = EXCLUDED.path, status = NULL, response_body = NULL, "
            "    response_headers = NULL, completed_at = NULL, created_at = now() "
            "WHERE api_idempotency.method <> EXCLUDED.method "
            "    OR api_idempotency.path <> EXCLUDED.path "
            "    OR (api_idempotency.status IS NULL AND api_idempotency.created_at "
            "        < now() - make_interval(days => %s)) "
            "RETURNING key",
            (key, method, path, _RETENTION_DAYS),
        )
        # RETURNING yields a row exactly when the INSERT inserted or the
        # DO UPDATE actually ran — a DO UPDATE skipped by its WHERE returns
        # nothing, so this is the reliable "did we win the claim" signal.
        return cur.fetchone() is not None


def _fetch(
    pool: ConnectionPool, key: str, method: str, path: str
) -> tuple[int, object, dict[str, Any]] | None:
    """The completed (status, body, headers) for `key` on THIS (method, path),
    or None when it is still executing (status NULL), belongs to a different
    route (same key reused across endpoints), or is gone (deleted by a failed
    owner)."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, response_body, response_headers FROM api_idempotency "
            "WHERE key = %s AND method = %s AND path = %s",
            (key, method, path),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return (row[0], row[1], row[2] or {})


def _store(
    pool: ConnectionPool, key: str, status: int, body: object, headers: dict[str, str]
) -> None:
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE api_idempotency SET status = %s, response_body = %s, "
            "response_headers = %s, completed_at = now() WHERE key = %s",
            (status, json.dumps(body), json.dumps(headers), key),
        )


def _release(pool: ConnectionPool, key: str) -> None:
    """Drop the row for `key` — the owner failed without a replayable
    outcome, so a retry must be able to execute afresh."""
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM api_idempotency WHERE key = %s", (key,))


async def _drain_and_store(resp: Response, pool: ConnectionPool, key: str) -> tuple[bytes, str]:
    """Drain a streaming response into the stored payload and persist it.

    Returns (body_bytes, content_type). Raises TypeError when the response
    is not a streaming body — the route contract lied — and lets any drain
    error propagate; the middleware's owner-section try/except releases the
    key on either, so a failed owner can never brick the key.
    """
    body_iter = getattr(resp, "body_iterator", None)
    if body_iter is None:
        # Only ALWK routes reach this point, and they are JSON API endpoints;
        # a non-streaming response here would mean the response was already
        # consumed (or the route contract lied).
        raise TypeError(f"unexpected non-streaming response: {type(resp).__name__}")

    def _as_bytes(chunk: Any) -> bytes:
        if isinstance(chunk, bytes):
            return chunk
        if isinstance(chunk, memoryview):
            return chunk.tobytes()
        return str(chunk).encode()

    chunks = [chunk async for chunk in body_iter]
    body_bytes = b"".join(_as_bytes(chunk) for chunk in chunks)
    body: object = json.loads(body_bytes) if body_bytes else None
    headers = {"content-type": resp.headers.get("content-type", "application/json")}
    await asyncio.to_thread(_store, pool, key, resp.status_code, body, headers)
    return body_bytes, headers["content-type"]


# ── middleware ─────────────────────────────────────────────────────────


async def idempotency_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Dedup requests to AT_LEAST_ONCE_WITH_KEY routes by Idempotency-Key.

    FastAPI `@app.middleware("http")` wrapper — registered in app.py right
    BEFORE the pause middleware, so a paused cluster answers 503 before this
    engages (the client's same-key retry lands after the pause and executes;
    the pause window also never sees a placeholder claimed and released by
    this middleware).

    A follower starts by checking every 100ms, then backs off exponentially
    to at most once per second while the 15-second total wait remains fixed.
    This preserves prompt replay for short owner work without turning a retry
    storm into roughly 150 database SELECTs per follower.
    """
    contract = contracts.contract_for(request.method, request.url.path)
    if (
        contract is None
        or contract.idempotency is not Idempotency.AT_LEAST_ONCE_WITH_KEY
        or contract.transactional_idempotency
    ):
        # A transactionally idempotent handler owns the key at the same commit
        # as its business row. Putting a response-cache placeholder in front of
        # it would recreate the crash window that transactional ownership closes:
        # committed business row, fresh placeholder, retries blocked for days.
        return await call_next(request)
    key = request.headers.get("Idempotency-Key")
    if not key:
        # Legacy client without a key: pass through unchanged (server
        # cannot dedup what it cannot identify). SDK and IM bridge send
        # keys; behavior for key-less callers is today's behavior.
        return await call_next(request)

    pool = request.app.state.db_pool
    if not await asyncio.to_thread(_claim, pool, key, request.method, request.url.path):
        # Another request owns the key (or already completed). Poll for the
        # owner's outcome and replay it; if the owner failed and released
        # the row, re-claim and execute ourselves.
        deadline = _monotonic() + _MAX_WAIT_SECONDS
        poll_delay = _POLL_INITIAL_S
        while _monotonic() < deadline:
            done = await asyncio.to_thread(_fetch, pool, key, request.method, request.url.path)
            if done is not None:
                return _replay(done)
            if await asyncio.to_thread(_claim, pool, key, request.method, request.url.path):
                break  # owner released (failed): we execute instead
            await asyncio.sleep(poll_delay)
            poll_delay = min(poll_delay * _POLL_FACTOR, _POLL_MAX_S)
        else:
            # Owner still executing past the wait bound — do NOT double-execute.
            # 503 with a short Retry-After: the client's retry loop picks it up.
            return error_response(
                request,
                code="idempotency_in_flight",
                status=503,
                detail="idempotent request still in flight, retry shortly",
                retryable=True,
                headers={"Retry-After": "1"},
            )

    # We own the key: execute, store the outcome, replay on same-key retries.
    # The whole owner section is one try/except so EVERY failure path between
    # claim and store releases the row — a dead/errored owner must never leave
    # a placeholder that bricks the key for the retention window (a non-2xx
    # outcome releases deliberately; a drained body / bad JSON / disconnected
    # client all release too, so the client's retry executes afresh).
    stored = False
    try:
        resp = await call_next(request)
        if resp.status_code < 200 or resp.status_code >= 300:
            # Not a replayable outcome — release so a retry executes afresh.
            await asyncio.to_thread(_release, pool, key)
            return resp
        # FastAPI wraps handler responses in a streaming body; drain it into
        # the stored payload and rebuild a plain response (ALWK routes are
        # JSON API endpoints, never SSE/file streams).
        body_bytes, content_type = await _drain_and_store(resp, pool, key)
        stored = True
        return Response(
            content=body_bytes,
            status_code=resp.status_code,
            headers={"content-type": content_type},
        )
    except BaseException:
        # Release only when nothing was stored: a stored row is a valid
        # outcome to replay; anything before it is a failed owner.
        if not stored:
            await asyncio.to_thread(_release, pool, key)
        raise


def _replay(done: tuple[int, object, dict[str, Any]]) -> Response:
    """Reconstruct the stored response for a same-key retry."""
    status, body, headers = done
    return JSONResponse(
        status_code=status,
        content=body,
        headers={k: str(v) for k, v in headers.items()},
    )
