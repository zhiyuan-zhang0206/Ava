"""Redis pub/sub -> Server-Sent Events bridge.

Two streaming modes:

1. ``event_stream`` — per-agent or broadcast filtered stream.
   Browser ``new EventSource("/api/agents/42/system")`` -> this module filters
   each Redis pubsub event by agent_id and sends in SSE format
   (``data: {json}\n\n``). Native streaming: every ``code_delta`` is
   forwarded the moment it arrives, no buffering / batching.

2. ``throttled_event_stream`` — all-events broadcast with batched throttling.
   Pushes EVERY event for EVERY agent (no agent_id filter, no role filter),
   draining Redis messages in batches and flushing at a fixed cadence
   (default 25/sec). Each flush sends a JSON array of event raw payloads
   as a single SSE data frame: ``data: [{...}, ...]\n\n``.

Wire format for ``event_stream``: the SSE ``data:`` line is directly the
``model_dump_json()`` of an ``events.Event``. The frontend's ``JSON.parse``
gets the same ``{role, agent_id, content, ...}`` shape.

``event_stream`` accepts optional ``channel``, ``role_filter``, and
``broadcast`` parameters. ``broadcast=True`` disables agent_id filtering.
"""

import json
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any

import redis
import redis.asyncio as aioredis
from fastapi import Request
from pydantic import ValidationError

from shared.config import settings
from shared.live_events import EVENT_ADAPTER, Error
from shared.redis_client import _TransportAwareAsyncConnection

_log = logging.getLogger(__name__)

# Client disconnect-detection polling window; deltas shorter than this
# are negligible compared to network RTT. env override:
# `AVA_SSE_DISCONNECT_POLL_SECONDS`.
_DISCONNECT_POLL_SECONDS = settings.gateway.sse_disconnect_poll_seconds

# Exceptions Redis IO may raise — pubsub connection raises
# ConnectionError / TimeoutError on Redis restart / network jitter;
# OSError as a fallback for the socket layer (BrokenPipe etc.).
# TypeError: redis-py writes a connection whose asyncio transport already fired
# connection_lost (`TypeError: 'NoneType' object is not callable`, agent-2613
# crash 2026-08-04 — see shared/redis_client._TransportAwareAsyncConnection).
# The SSE stream is a per-request short-lived connection, so treat it as any
# other IO failure: emit an error frame and let the frontend reconnect.
_REDIS_IO_ERRORS = (redis.ConnectionError, redis.TimeoutError, OSError, TypeError)

# Heartbeat: after this much silence, emit a real `data:` event the browser
# EventSource `onmessage` can see — so a client-side watchdog can detect a
# half-dead connection (server graceful restart / a proxy hop that stays OPEN
# but stops delivering data). The `: hb` comment keeps the TCP / proxy hop warm
# but is invisible to `onmessage`, so it cannot drive the watchdog on its own.
_HEARTBEAT_SECONDS = 15.0
_HEARTBEAT_FRAME = b'data: {"role":"heartbeat"}\n\n'

# Keep-alive comment cadence: at most one `: hb` per second on paths that
# yield nothing else. A busy events channel must not turn the wire silent
# (read-timeout clients — httpx/curl — would disconnect), and a quiet one
# must not get a comment per poll tick.
_KEEPALIVE_COMMENT_INTERVAL_S = 1.0


def _warm_frame(
    now: float, last_comment: float, last_data_frame: float
) -> tuple[bytes | None, float, float]:
    """A frame that keeps the wire warm, or None.

    Heartbeat when the last data frame is older than ``_HEARTBEAT_SECONDS``
    (the visible event drives client watchdogs); otherwise a ``: hb`` comment
    at most once a second. Returns ``(frame, last_comment, last_data_frame)``
    — the caller stamps both clocks, so any yielded frame counts as traffic.
    """

    if now - last_data_frame >= _HEARTBEAT_SECONDS:
        return _HEARTBEAT_FRAME, now, now
    if now - last_comment >= _KEEPALIVE_COMMENT_INTERVAL_S:
        return b": hb\n\n", now, last_data_frame
    return None, now, last_data_frame


async def event_stream(
    redis_url: str,
    agent_id: int,  # ignored in broadcast mode
    request: Request,
    *,
    channel: str | None = None,
    role_filter: frozenset[str] | None = None,
    broadcast: bool = False,
    validator: Any = EVENT_ADAPTER,
) -> AsyncGenerator[bytes, None]:
    """Yield a stream of SSE frames (bytes) until the client disconnects
    or Redis goes down.

    Lifetime defenses:
    - `await pubsub.subscribe(...)` runs **before** the first yield, and
      is wrapped in try/finally — if subscribe itself raises (Redis
      unreachable), let cleanup run and then re-raise so FastAPI returns
      500 (EventSource `onerror` fires normally, instead of a 200+empty
      body "connected but no events" zombie state).
    - The `yield` is inside the try — when a client aborts on the first
      frame, `GeneratorExit` is raised; finally must run, otherwise the
      Redis pubsub socket leaks and a long run hits maxclients.
    - In the `while` loop, when `get_message` / `is_disconnected` raise
      (Redis restart / ASGI receive exception): publish a single `error`
      event frame to the client and return — the frontend sees a toast
      instead of just console noise.
    - In finally, each cleanup step is independently suppressed + logged
      so one failure does not swallow another.

    Args:
        redis_url: Redis connection string (`settings.data_plane.redis_url`). Each
            connection gets its own pubsub socket — FastAPI concurrent
            requests are naturally isolated.
        agent_id: only forward events where `event.agent_id == agent_id`
            (ignored in broadcast mode).
        request: Starlette `Request`; use `is_disconnected()` for quick
            exit (otherwise pubsub blocking holds until Redis heartbeat
            timeout).
        channel: Redis channel to subscribe to (default `settings.data_plane.events_channel`).
        role_filter: only forward events whose role is in this set; None
            or empty -> no filter.
        broadcast: True -> do not filter by agent_id, forward all.
        validator: the pydantic validator each frame must parse as (default
            EVENT_ADAPTER). A non-agent-events channel (e.g. the alerts
            stream) passes its own TypeAdapter so frames are validated
            against the channel's shape instead of being dropped.

    Yields:
        bytes: SSE frame (`data: ...\n\n`).
    """
    _channel = channel if channel is not None else settings.data_plane.events_channel
    client = aioredis.from_url(
        redis_url,
        decode_responses=True,
        # Dead-transport detection: see shared.redis_client._TransportAwareAsyncConnection.
        connection_class=_TransportAwareAsyncConnection,
    )
    pubsub = client.pubsub()  # pyright: ignore[reportUnknownMemberType]
    # Attach/detach bracket: a reconnect storm (client watchdog cycling) or a
    # subscriber that never detaches is invisible without the pair; the detach
    # line carries duration + frames so a half-open stream is distinguishable
    # from a healthy long-lived one.
    opened = time.monotonic()
    data_frames = 0
    _log.info("sse attach: agent_id=%s channel=%s broadcast=%s", agent_id, _channel, broadcast)
    try:
        await pubsub.subscribe(_channel)

        yield b": stream open\n\n"

        # Last time a real `data:` frame went out (business event or heartbeat).
        # The `: hb` comment does NOT count — the browser can't see it, so it
        # can't keep the client watchdog alive.
        last_data_frame = time.monotonic()
        last_comment = time.monotonic()

        while True:
            try:
                if await request.is_disconnected():
                    return
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=_DISCONNECT_POLL_SECONDS,
                )
            except _REDIS_IO_ERRORS as exc:
                _log.warning("sse pubsub read failed: %r agent_id=%s", exc, agent_id)
                yield _sse_frame(
                    Error(
                        agent_id=agent_id,
                        content=f"event stream interrupted: {type(exc).__name__}",
                    ).model_dump_json()
                )
                return

            now = time.monotonic()
            if msg is not None and isinstance(msg["data"], str):
                try:
                    event = validator.validate_json(msg["data"])
                except ValidationError:
                    yield f": dropped unparseable payload ({len(msg['data'])} bytes)\n\n".encode()
                    last_comment = now
                    continue
                # broadcast mode: do not filter by agent_id
                if (broadcast or event.agent_id == agent_id) and (
                    not role_filter or event.role in role_filter
                ):
                    yield _sse_frame(msg["data"])
                    data_frames += 1
                    last_data_frame = now
                    last_comment = now
                    continue

            # Nothing business-relevant this tick (quiet channel, a filtered
            # event, a non-str or unparseable payload): keep the wire warm —
            # heartbeat once the silence crosses _HEARTBEAT_SECONDS, otherwise
            # a throttled keep-alive comment.
            frame, last_comment, last_data_frame = _warm_frame(now, last_comment, last_data_frame)
            if frame is not None:
                yield frame
    finally:
        _log.info(
            "sse detach: agent_id=%s duration=%.1fs data_frames=%d",
            agent_id,
            time.monotonic() - opened,
            data_frames,
        )
        for step, coro_fn in (
            ("unsubscribe", lambda: pubsub.unsubscribe(_channel)),  # pyright: ignore[reportUnknownMemberType]
            ("pubsub.aclose", pubsub.aclose),
            ("client.aclose", client.aclose),
        ):
            try:
                await coro_fn()
            except Exception as exc:
                _log.warning("sse cleanup %s failed: %r", step, exc)


async def _drain_redis_messages(
    pubsub,  # noqa: ANN001  # redis-asyncio PubSub
    request: Request,
    flush_interval: float,
) -> tuple[list[str], bool]:
    """Drain all available messages from Redis within one flush window.

    Returns (buffer, disconnected). If disconnected is True, the caller
    should stop the stream.

    The client-disconnect check runs once per drain cycle (not on every
    message) — during a burst of hundreds of chat_delta / code_delta /
    exec_output_chunk events the inner loop can iterate fast, and
    ``request.is_disconnected()`` (``await self._receive()`` under uvicorn)
    adds non-trivial per-iteration overhead that stacks into seconds of
    cumulative lag when called on every event.
    """
    # Check disconnect once per drain cycle — a disconnect detected on
    # the next cycle (≤ flush_interval away) is a rounding error.
    if await request.is_disconnected():
        return [], True

    buffer: list[str] = []
    drain_deadline = time.monotonic() + flush_interval

    while time.monotonic() < drain_deadline:
        remaining = drain_deadline - time.monotonic()
        if remaining <= 0:
            break
        poll = min(remaining, 0.1)
        msg = await pubsub.get_message(  # pyright: ignore[reportUnknownMemberType]
            ignore_subscribe_messages=True,
            timeout=poll,
        )
        if msg is None:
            break
        raw = msg["data"]
        if not isinstance(raw, str):
            continue
        try:
            EVENT_ADAPTER.validate_json(raw)
        except ValidationError:
            continue
        buffer.append(raw)

    return buffer, False


async def throttled_event_stream(
    redis_url: str,
    request: Request,
    *,
    channel: str | None = None,
    throttle_rate: float,
) -> AsyncGenerator[bytes, None]:
    """Yield a throttled, batched SSE stream of ALL events.

    Broadcast mode, no agent_id filter, no role filter. Events are
    drained from Redis in batches and flushed at a fixed cadence
    (``1 / throttle_rate`` seconds). Each flush sends a JSON array of
    raw event payloads as a single SSE data frame:

        data: [{...}, {...}, ...]\n\n

    Lifetime defenses mirror ``event_stream``: subscribe-before-yield,
    GeneratorExit-safe finally, Redis-IO recovery, client disconnect
    detection.

    Args:
        redis_url: Redis connection string.
        request: Starlette ``Request`` for disconnect detection.
        channel: Redis channel (default ``settings.data_plane.events_channel``).
        throttle_rate: SSE pushes per second — required (the sole caller passes
            ``settings.gateway.sse_throttle_rate``).

    Yields:
        bytes: SSE frame (``data: [...]\n\n``).
    """
    _channel = channel if channel is not None else settings.data_plane.events_channel
    client = aioredis.from_url(
        redis_url,
        decode_responses=True,
        # Dead-transport detection: see shared.redis_client._TransportAwareAsyncConnection.
        connection_class=_TransportAwareAsyncConnection,
    )
    pubsub = client.pubsub()  # pyright: ignore[reportUnknownMemberType]
    opened = time.monotonic()
    data_frames = 0
    total_events = 0
    flush_interval = 1.0 / throttle_rate
    _log.info(
        "sse throttle attach: channel=%s throttle_rate=%.1f/s",
        _channel,
        throttle_rate,
    )
    try:
        await pubsub.subscribe(_channel)

        yield b": stream open\n\n"

        last_flush = time.monotonic()

        while True:
            try:
                buffer, disconnected = await _drain_redis_messages(pubsub, request, flush_interval)
            except _REDIS_IO_ERRORS as exc:
                _log.warning("sse throttle pubsub read failed: %r", exc)
                yield _sse_batch_frame(
                    [
                        Error(
                            agent_id=0,
                            content=f"event stream interrupted: {type(exc).__name__}",
                        ).model_dump_json()
                    ]
                )
                return

            if disconnected:
                return

            now = time.monotonic()
            if buffer:
                yield _sse_batch_frame(buffer)
                data_frames += 1
                total_events += len(buffer)
                last_flush = now
            elif now - last_flush >= _HEARTBEAT_SECONDS:
                yield _HEARTBEAT_FRAME
                last_flush = now
            else:
                yield b": hb\n\n"
    finally:
        _log.info(
            "sse throttle detach: duration=%.1fs data_frames=%d total_events=%d",
            time.monotonic() - opened,
            data_frames,
            total_events,
        )
        for step, coro_fn in (
            ("unsubscribe", lambda: pubsub.unsubscribe(_channel)),  # pyright: ignore[reportUnknownMemberType]
            ("pubsub.aclose", pubsub.aclose),
            ("client.aclose", client.aclose),
        ):
            try:
                await coro_fn()
            except Exception as exc:
                _log.warning("sse throttle cleanup %s failed: %r", step, exc)


def _sse_frame(payload: str) -> bytes:
    """Wrap a data frame per the SSE protocol.

    Multi-line payloads must prefix each line with `data:` to be
    compliant; our event JSON is single-line, but we splitlines
    defensively. The trailing empty line (`\n\n`) is the SSE frame
    separator.
    """
    lines = payload.splitlines() or [""]
    return ("".join(f"data: {line}\n" for line in lines) + "\n").encode()


def _sse_batch_frame(events: list[str]) -> bytes:
    """Wrap a batch of already-serialized event JSON strings into ONE SSE
    frame carrying a JSON array of objects: `data: [{...},{...}]`.

    Each element of `events` is ALREADY valid JSON (a `model_dump_json()`
    payload straight off Redis), so the array is built by joining the raw
    strings — NOT `json.dumps(events)`, which would re-encode each element
    as a JSON string and emit `data: ["{...}","{...}"]` (an array of
    strings). The browser does a single `JSON.parse`, so a string element
    has no `.role` / `.agent_id`, fails the active-thread guard, and is
    silently dropped — the live timeline / token / pending streams go dark
    while only the separate `/api/system` object-stream (last-active) keeps
    working. Join raw so the client folds real objects.
    """
    return _sse_frame("[" + ",".join(events) + "]")


# --- For tests only ---


def _decode_frames_for_test(chunks: list[bytes]) -> list[dict]:
    """Test helper: slice an SSE byte stream into frames + parse the JSON
    on data lines.

    Ignores `:` comment frames (heartbeat / stream open / dropped).
    """
    out: list[dict[str, Any]] = []
    text = b"".join(chunks).decode()
    for frame in text.split("\n\n"):
        data_lines = [
            line[len("data: ") :] for line in frame.splitlines() if line.startswith("data: ")
        ]
        if not data_lines:
            continue
        out.append(json.loads("".join(data_lines)))  # pyright: ignore[reportUnknownArgumentType]
    return out


__all__ = ["event_stream", "throttled_event_stream"]
