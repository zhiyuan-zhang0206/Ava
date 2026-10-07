"""SSE endpoint: Redis `ava:events` → `text/event-stream` transparent forwarding.

**Strategy**: don't use TestClient's `stream()` — httpx sync client's back-pressure
handling for async generator doesn't mesh well with long-lived Redis pubsub, easy deadlock.
Directly `asyncio.run` call `event_stream()` async generator, using fake Request
(only needs `await is_disconnected() → False/True`), real Redis publish
real parsing, finer granularity.

Redis uses the session's instance; Redis no separate db, channel name isolation (settings.data_plane.events_channel
won't collide with running dev Ava Server — although test messages may leak into dev UI, dev
tailer filters by agent_id, test agent_ids are all newly created in tests).
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace

import psycopg
import pytest
import redis as sync_redis
from fastapi.testclient import TestClient
from redis.asyncio.client import PubSub

from base.config import settings
from base.db import create_agent
from base.events.live.bus import EventBus
from base.events.live.projection import (
    GLOBAL_ROLES,
    SYSTEM_ROLES,
    ChatDelta,
    CodeDelta,
    LabelUpdated,
)
from gateway.app import app
from gateway.events.sse import _decode_frames_for_test, event_stream


@dataclass
class _FakeRequest:
    """Minimal Request stub — `event_stream` only calls `await is_disconnected()`.

    `disconnected` flag is explicitly set True by tests to stop stream.
    """

    disconnected: bool = field(default=False)

    async def is_disconnected(self) -> bool:
        return self.disconnected


_HEARTBEAT_DATA_FRAME_PREFIX = b'data: {"role":"heartbeat"}'


async def _collect_frames(
    agent_id: int,
    publisher: sync_redis.Redis,
    payloads: list[str],
    n_data_frames: int,
    timeout: float = 5.0,
    channel: str | None = None,
    role_filter: frozenset[str] | None = None,
    broadcast: bool = False,
    count_heartbeats: bool = False,
    overall_timeout: float = 90.0,
) -> list[bytes]:
    """Start event_stream + async publish + stop after collecting enough n data frames.

    subscribe is lazy — once the async generator yields the first item (`: stream open`)
    subscribe is established; publish after that won't miss.

    `channel` / `role_filter` / `broadcast` forwarded to event_stream; when `broadcast=True`
    agent_id is ignored (forward all agent events, gated by role_filter).

    heartbeat data frames are **not collected nor counted** by default (`count_heartbeats=False`): heartbeats are
    sent by local idle timer; if redis delivery is blocked by host engine transient black hole (CI observed
    ~45s, see runbook §CI), heartbeats would first fill the `n_data_frames` quota, turning "wait for business
    payload" into "got a bunch of heartbeats and returned early". Tests specifically for heartbeat pass True.
    `overall_timeout` is the total deadline for the entire collection — during black hole, gen's comment/heartbeat frames
    keep streaming, per-frame `timeout` never fires; must have overall deadline to fail loud
    (90s same as e2e wait_for_status ceiling: covers worst-case observation recovery).
    """
    req = _FakeRequest()
    gen = event_stream(  # type: ignore[arg-type]
        EventBus.from_settings(),
        agent_id,
        req,  # type: ignore[arg-type]
        channel=channel,
        role_filter=role_filter,
        broadcast=broadcast,
    )

    # pull the opening frame to confirm subscribe is ready
    first = await anext(gen)
    assert first == b": stream open\n\n"

    # async publish — use the passed-in channel (default settings.data_plane.events_channel)
    _pub_channel = channel if channel is not None else settings.data_plane.events_channel

    async def _publish() -> None:
        await asyncio.sleep(0.05)
        for p in payloads:
            publisher.publish(_pub_channel, p)  # pyright: ignore[reportUnknownMemberType]

    pub_task = asyncio.create_task(_publish())

    frames: list[bytes] = [first]
    data_count = 0
    deadline = asyncio.get_running_loop().time() + overall_timeout
    try:
        while data_count < n_data_frames:
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(
                    f"collected {data_count}/{n_data_frames} data frames in "
                    f"{overall_timeout}s — events not delivered"
                )
            frame = await asyncio.wait_for(anext(gen), timeout=timeout)
            if not count_heartbeats and frame.startswith(_HEARTBEAT_DATA_FRAME_PREFIX):
                continue
            frames.append(frame)
            if frame.startswith(b"data:"):
                data_count += 1
    finally:
        req.disconnected = True
        await pub_task
        # let generator run through finally cleanup
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(gen.aclose(), timeout=2.0)
    return frames


@pytest.fixture
def redis_client() -> sync_redis.Redis:
    return sync_redis.Redis.from_url(settings.data_plane.redis_url, decode_responses=True)  # pyright: ignore[reportUnknownMemberType]


def test_sse_forwards_matching_thread_events(
    db_conn: psycopg.Connection,
    redis_client: sync_redis.Redis,
) -> None:
    tid = create_agent(db_conn)
    payloads = [
        CodeDelta(agent_id=tid, item_id="5.0", content="pri").model_dump_json(),
        CodeDelta(agent_id=tid, item_id="5.0", content="nt(1)").model_dump_json(),
        ChatDelta(agent_id=tid, item_id="5.0", content="done").model_dump_json(),
    ]
    frames = asyncio.run(_collect_frames(tid, redis_client, payloads, n_data_frames=3))
    decoded = _decode_frames_for_test(frames)
    assert [d["role"] for d in decoded] == ["code_delta", "code_delta", "chat_delta"]
    assert decoded[0]["content"] == "pri"
    assert decoded[1]["content"] == "nt(1)"
    assert decoded[2]["content"] == "done"


def test_sse_filters_by_agent_id(
    db_conn: psycopg.Connection,
    redis_client: sync_redis.Redis,
) -> None:
    """Another agent's event should not appear in the stream subscribed to tid=A."""
    tid_a = create_agent(db_conn)
    tid_b = create_agent(db_conn)
    payloads = [
        ChatDelta(agent_id=tid_b, item_id="5.0", content="for B").model_dump_json(),
        ChatDelta(agent_id=tid_a, item_id="5.0", content="for A").model_dump_json(),
    ]
    frames = asyncio.run(_collect_frames(tid_a, redis_client, payloads, n_data_frames=1))
    decoded = _decode_frames_for_test(frames)
    assert len(decoded) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert decoded[0]["content"] == "for A"


def test_sse_drops_invalid_payload_as_comment(
    db_conn: psycopg.Connection,
    redis_client: sync_redis.Redis,
) -> None:
    """wire drift: producer published JSON not recognized by Event union. Should be marked as comment
    frame instead of crashing stream or silently swallowing."""
    tid = create_agent(db_conn)
    payloads = [
        json.dumps({"role": "made_up_role", "agent_id": tid}),
        ChatDelta(agent_id=tid, item_id="5.0", content="after").model_dump_json(),
    ]
    frames = asyncio.run(_collect_frames(tid, redis_client, payloads, n_data_frames=1))

    text = b"".join(frames).decode()
    assert "dropped unparseable payload" in text
    # valid event still passes through — the bad one earlier didn't break the whole stream
    decoded = _decode_frames_for_test(frames)
    assert [d["role"] for d in decoded] == ["chat_delta"]
    assert decoded[0]["content"] == "after"


@pytest.mark.parametrize(
    ("path", "batched", "agent_id"),
    [
        ("/api/agents/1/events/stream", False, 1),
        ("/api/system/all", True, 0),
    ],
)
def test_sse_subscribe_failure_after_http_start_sends_error_frame(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    batched: bool,
    agent_id: int,
) -> None:
    """Exercise the real router, StreamingResponse, and Redis subscription path."""

    async def fail_subscribe(self: PubSub, *args: object, **kwargs: object) -> None:
        raise sync_redis.ConnectionError("Redis unavailable")

    monkeypatch.setattr(PubSub, "subscribe", fail_subscribe)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(path)

    assert response.status_code == 200  # StreamingResponse already sent response.start
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.content.startswith(b"data: ")
    assert response.content.endswith(b"\n\n")
    payload = json.loads(response.content[len(b"data: ") : -2])
    event = payload[0] if batched else payload
    assert event["role"] == "error"
    assert event["agent_id"] == agent_id
    assert event["content"] == "event stream interrupted: ConnectionError"


# --- task 10/11: new SSE endpoint + role_filter tests ---


def test_sse_role_filter_system_passes_code_delta(
    db_conn: psycopg.Connection,
    redis_client: sync_redis.Redis,
) -> None:
    """When role_filter=SYSTEM_ROLES, code_delta (system role) should pass."""
    tid = create_agent(db_conn)
    payloads = [
        CodeDelta(agent_id=tid, item_id="5.0", content="hello").model_dump_json(),
    ]
    frames = asyncio.run(
        _collect_frames(
            tid,
            redis_client,
            payloads,
            n_data_frames=1,
            role_filter=SYSTEM_ROLES,
        )
    )
    decoded = _decode_frames_for_test(frames)
    assert len(decoded) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert decoded[0]["role"] == "code_delta"


def test_sse_role_filter_system_passes_chat_streaming(
    db_conn: psycopg.Connection,
    redis_client: sync_redis.Redis,
) -> None:
    """chat_delta like code_delta is in SYSTEM_ROLES, both pass."""
    tid = create_agent(db_conn)
    payloads = [
        ChatDelta(agent_id=tid, item_id="5.0", content="agent \u56de\u590d").model_dump_json(),
        CodeDelta(
            agent_id=tid, item_id="5.0", content="\u4ee3\u7801\u7247\u6bb5"
        ).model_dump_json(),
    ]
    frames = asyncio.run(
        _collect_frames(
            tid,
            redis_client,
            payloads,
            n_data_frames=2,
            role_filter=SYSTEM_ROLES,
        )
    )
    decoded = _decode_frames_for_test(frames)
    assert len(decoded) == 2  # pyright: ignore[reportUnknownArgumentType]
    assert {d["role"] for d in decoded} == {"chat_delta", "code_delta"}


# --- fan-out split: the broadcast carries only GLOBAL_ROLES, the per-agent
# stream carries the full SYSTEM_ROLES for one agent (the wire-volume win) ---


def test_broadcast_drops_high_frequency_deltas_keeps_global_roles(
    db_conn: psycopg.Connection,
    redis_client: sync_redis.Redis,
) -> None:
    """The /api/system broadcast (GLOBAL_ROLES, broadcast=True) must NOT fan
    out the token-level deltas of any agent — only the low-frequency
    cross-agent lifecycle, for every agent. This is the wire-volume guard: with
    N clients x M agents, the chat/code deltas are exactly what the old
    everything-broadcast fanned out N*M-fold.

    Two agents each emit a chat_delta (high freq) then a label_updated
    (GLOBAL_ROLE). Publish order is preserved and the role filter is
    synchronous, so collecting the first 2 data frames yields the two labels iff
    the chat_deltas were dropped — a leaked delta would surface among them."""
    tid_a = create_agent(db_conn)
    tid_b = create_agent(db_conn)
    payloads = [
        ChatDelta(agent_id=tid_a, item_id="5.0", content="A streaming").model_dump_json(),
        ChatDelta(agent_id=tid_b, item_id="5.0", content="B streaming").model_dump_json(),
        LabelUpdated(agent_id=tid_a, label="agent A").model_dump_json(),
        LabelUpdated(agent_id=tid_b, label="agent B").model_dump_json(),
    ]
    frames = asyncio.run(
        _collect_frames(
            0,  # agent_id ignored in broadcast mode
            redis_client,
            payloads,
            n_data_frames=2,
            role_filter=GLOBAL_ROLES,
            broadcast=True,
        )
    )
    decoded = _decode_frames_for_test(frames)
    # Both label_updated come through (broadcast = all agents); zero chat_delta.
    assert [d["role"] for d in decoded] == ["label_updated", "label_updated"]
    assert {d["agent_id"] for d in decoded} == {tid_a, tid_b}


def test_per_agent_stream_carries_full_roles_for_one_agent_only(
    db_conn: psycopg.Connection,
    redis_client: sync_redis.Redis,
) -> None:
    """The /api/agents/{id}/system stream (SYSTEM_ROLES, broadcast=False) is the
    complement: it carries the FULL role set — including the high-frequency
    deltas the broadcast drops — but only for the one observed agent. Agent B's
    events (both the delta and the global-role label) never leak in.

    Same four payloads as the broadcast test; subscribed as agent A. The first 2
    data frames are A's chat_delta + A's label_updated, in publish order; B's two
    are filtered by agent_id."""
    tid_a = create_agent(db_conn)
    tid_b = create_agent(db_conn)
    payloads = [
        ChatDelta(agent_id=tid_a, item_id="5.0", content="A streaming").model_dump_json(),
        ChatDelta(agent_id=tid_b, item_id="5.0", content="B streaming").model_dump_json(),
        LabelUpdated(agent_id=tid_a, label="agent A").model_dump_json(),
        LabelUpdated(agent_id=tid_b, label="agent B").model_dump_json(),
    ]
    frames = asyncio.run(
        _collect_frames(
            tid_a,
            redis_client,
            payloads,
            n_data_frames=2,
            role_filter=SYSTEM_ROLES,
        )
    )
    decoded = _decode_frames_for_test(frames)
    assert [d["role"] for d in decoded] == ["chat_delta", "label_updated"]
    assert all(d["agent_id"] == tid_a for d in decoded)  # pyright: ignore[reportUnknownArgumentType]


def test_sse_system_endpoint_response_headers(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/system SSE endpoint should have correct Content-Type + Cache-Control etc. headers."""

    async def fake_stream(*_args: object, **_kwargs: object) -> AsyncIterator[bytes]:
        yield b": stream open\n\n"

    from gateway.events import system as system_router

    monkeypatch.setattr(system_router, "event_stream", fake_stream)

    with TestClient(app) as client, client.stream("GET", "/api/agents/1/system") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.headers["x-accel-buffering"] == "no"


def test_sse_emits_heartbeat_data_event_when_idle(
    db_conn: psycopg.Connection,
    redis_client: sync_redis.Redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idle (no business event) for _HEARTBEAT_SECONDS -> a visible
    `data: {"role":"heartbeat"}` frame goes out. The client watchdog needs a real
    data frame (the `: hb` comment is invisible to EventSource.onmessage), so this
    pins that one actually reaches the client. Advance the stream's clock past its
    normal heartbeat interval on the first idle tick."""
    from gateway.events import sse as sse_mod

    now = 0.0

    def monotonic() -> float:
        nonlocal now
        now += 20.0
        return now

    monkeypatch.setattr(sse_mod, "time", SimpleNamespace(monotonic=monotonic))
    tid = create_agent(db_conn)
    # no payloads -> the stream sits idle -> the heartbeat is the first data frame (count_heartbeats=True: this test is
    # ABOUT the heartbeat — the default filters them out as load noise)
    frames = asyncio.run(
        _collect_frames(tid, redis_client, [], n_data_frames=1, count_heartbeats=True)
    )
    decoded = _decode_frames_for_test(frames)
    assert decoded[0] == {"role": "heartbeat"}


# --- throttled_event_stream tests ---


def test_busy_channel_still_emits_keepalive_comments(
    db_conn: psycopg.Connection, redis_client: sync_redis.Redis
) -> None:
    """A channel flooded with other agents' events must not go silent.

    Regression (2026-08-03): event_stream only emitted ``: hb`` on the msg-None path, so a continuously busy
    ``ava:events`` channel (in a live cluster, a message every ~0.1s) made the stream yield nothing at all — read-
    timeout clients (httpx, curl) disconnected every 30s and the im_bridge subscription reconnected in a loop.
    """
    tid = create_agent(db_conn)  # this subscriber's agent
    other = create_agent(db_conn)  # whose events flood the channel

    req = _FakeRequest()
    gen = event_stream(  # type: ignore[arg-type]
        EventBus.from_settings(),
        tid,
        req,  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        first = await anext(gen)
        assert first == b": stream open\n\n"

        # Burst of events for OTHER agents — every one hits the filtered path
        # and yields nothing; before the fix the stream then went silent.
        for _ in range(20):
            redis_client.publish(  # pyright: ignore[reportUnknownMemberType]
                settings.data_plane.events_channel,
                ChatDelta(agent_id=other, item_id="5.0", content="noise").model_dump_json(),
            )

        # The wire must stay warm: a keep-alive comment lands within ~3s
        # (the 1s throttle), and keeps coming under the sustained flood.
        for _ in range(2):
            frame = await asyncio.wait_for(anext(gen), timeout=3.0)
            assert frame == b": hb\n\n"

        req.disconnected = True
        with contextlib.suppress(StopAsyncIteration):
            await anext(gen)

    asyncio.run(scenario())


def test_sse_survives_redis_typeerror(monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent-2613 redis dead-transport TypeError ('NoneType' object is not
    callable) is treated as an IO failure: the stream emits an error frame and
    returns cleanly instead of raising out of the generator (which would surface
    as a 500 on the SSE endpoint)."""
    from redis.asyncio.client import PubSub as _RedisPubSub

    async def _boom(self: object, *args: object, **kwargs: object) -> None:
        raise TypeError("'NoneType' object is not callable")

    monkeypatch.setattr(_RedisPubSub, "get_message", _boom)

    async def _drive() -> list[bytes]:
        req = _FakeRequest()
        gen = event_stream(  # type: ignore[arg-type]
            EventBus.from_settings(),
            4242,
            req,  # type: ignore[arg-type]
        )
        frames: list[bytes] = []
        try:
            frames.append(await anext(gen))  # ": stream open"
            frames.append(await asyncio.wait_for(anext(gen), timeout=5.0))  # error frame
            with pytest.raises(StopAsyncIteration):
                await anext(gen)  # generator ended, did not hang
        finally:
            req.disconnected = True
            with contextlib.suppress(TimeoutError, StopAsyncIteration):
                await asyncio.wait_for(gen.aclose(), timeout=2.0)
        return frames

    frames = asyncio.run(_drive())
    assert frames[0] == b": stream open\n\n"
    assert frames[1].startswith(b"data: "), f"expected an error frame, got {frames[1]!r}"
    assert b"event stream interrupted" in frames[1], frames[1]


# --- frame encoding: Unicode line separators that are legal raw in JSON ---
#
# U+0085 / U+2028 / U+2029 may appear unescaped inside a JSON string (pydantic model_dump_json emits them raw;
# JSON.parse accepts them), so str.splitlines() must never split a data payload: the client rejoins the data lines
# with "\n", planting a raw newline inside the JSON literal, and JSON.parse fails with "Bad control character in
# string literal".
