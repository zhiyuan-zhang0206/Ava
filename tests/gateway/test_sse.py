"""SSE endpoint: Redis `ava:events` → `text/event-stream` transparent forwarding.

**Strategy**: don't use TestClient's `stream()` — httpx sync client's back-pressure
handling for async generator doesn't mesh well with long-lived Redis pubsub, easy deadlock.
Directly `asyncio.run` call `event_stream()` async generator, using fake Request
(only needs `await is_disconnected() → False/True`), real Redis publish
real parsing, finer granularity.

Redis uses the docker compose one; Redis no separate db, channel name isolation (settings.data_plane.events_channel
won't collide with running dev Ava Server — although test messages may leak into dev UI, dev
tailer filters by agent_id, test agent_ids are all newly created in tests).
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import psycopg
import pytest
import redis as sync_redis
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.sse import (
    _decode_frames_for_test,
    event_stream,
    throttled_event_stream,
)
from shared.config import settings
from shared.db import create_agent
from shared.live_events import GLOBAL_ROLES, SYSTEM_ROLES, ChatDelta, CodeDelta, LabelUpdated


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
        settings.data_plane.redis_url,
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


def test_sse_endpoint_response_headers(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSE endpoint HTTP layer smoke: Content-Type + X-Accel-Buffering and Cache-Control needed
    by nginx/cloudflare. Without these two, SSE completely breaks when deployed behind a proxy
    (already written in `gateway/app.py`), verified via TestClient + mock short generator.

    Use mock event_stream (immediate yield + end) to bypass real Redis pubsub — header fields
    are decided by FastAPI StreamingResponse wrapper, unrelated to event_stream concrete output.
    """

    async def fake_stream(*_args: object, **_kwargs: object) -> AsyncIterator[bytes]:
        yield b": stream open\n\n"

    from gateway.routers import agent_events as agent_events_router

    monkeypatch.setattr(agent_events_router, "event_stream", fake_stream)

    with TestClient(app) as client, client.stream("GET", "/api/agents/1/events/stream") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.headers["x-accel-buffering"] == "no"


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

    from gateway.routers import system as system_router

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
    pins that one actually reaches the client. _HEARTBEAT_SECONDS=0 fires it on the
    first idle tick instead of waiting the real 15s."""
    from gateway import sse as sse_mod

    monkeypatch.setattr(sse_mod, "_HEARTBEAT_SECONDS", 0.0)
    tid = create_agent(db_conn)
    # no payloads -> the stream sits idle -> the heartbeat is the first data frame
    # (count_heartbeats=True: this test is ABOUT the heartbeat — the default
    # filters them out as load noise)
    frames = asyncio.run(
        _collect_frames(tid, redis_client, [], n_data_frames=1, count_heartbeats=True)
    )
    decoded = _decode_frames_for_test(frames)
    assert decoded[0] == {"role": "heartbeat"}


# --- throttled_event_stream tests ---


async def _collect_throttled_frames(
    publisher: sync_redis.Redis,
    payloads: list[str],
    n_data_frames: int,
    timeout: float = 5.0,
    channel: str | None = None,
    throttle_rate: float = 10.0,
    overall_timeout: float = 90.0,
    min_events: int | None = None,
    agent_filter: set[int] | None = None,
) -> list[bytes]:
    """Run throttled_event_stream + async publish + collect frames.

    Stops after ``n_data_frames`` data frames, or — when ``min_events`` is set —
    after that many business events have arrived across however many frames they
    land in. The event-count mode is race-free under load: a time-windowed
    throttle can split near-simultaneous publishes across frames, so stopping on
    a frame count (not an event count) makes a batching assertion flaky.
    """
    req = _FakeRequest()
    gen = throttled_event_stream(
        settings.data_plane.redis_url,
        req,  # type: ignore[arg-type]
        channel=channel,
        throttle_rate=throttle_rate,
        agent_filter=agent_filter,
    )

    # Pull the opening frame
    first = await anext(gen)
    assert first == b": stream open\n\n"

    _pub_channel = channel if channel is not None else settings.data_plane.events_channel

    async def _publish() -> None:
        await asyncio.sleep(0.05)
        for p in payloads:
            publisher.publish(_pub_channel, p)  # pyright: ignore[reportUnknownMemberType]

    pub_task = asyncio.create_task(_publish())

    frames: list[bytes] = [first]
    data_count = 0
    event_count = 0
    deadline = asyncio.get_running_loop().time() + overall_timeout
    try:
        while (data_count < n_data_frames) if min_events is None else (event_count < min_events):
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(
                    f"collected {data_count} frame(s) / {event_count} event(s) in "
                    f"{overall_timeout}s — events not delivered"
                )
            frame = await asyncio.wait_for(anext(gen), timeout=timeout)
            frames.append(frame)
            if frame.startswith(b"data:"):
                data_count += 1
                if min_events is not None:
                    # Count business events so `min_events` can span frames; a
                    # heartbeat frame decodes to an object, not a list — skip it.
                    parsed = json.loads(frame.decode().removeprefix("data: ").strip())
                    if isinstance(parsed, list):
                        event_count += len(parsed)  # pyright: ignore[reportUnknownArgumentType]
    finally:
        req.disconnected = True
        await pub_task
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(gen.aclose(), timeout=2.0)
    return frames


def _decode_throttled_frames(chunks: list[bytes]) -> list[list[dict]]:
    """Parse throttled SSE frames exactly like the browser does — ONE
    `json.parse` per `data:` line, yielding a JSON array of event OBJECTS.

    This deliberately does NOT re-parse string elements. The frontend
    (`useEventStream.tsx`) parses each frame once and fans the array
    elements out as objects; if the server double-encodes (`json.dumps`
    over a list of raw JSON strings), each element arrives as a string,
    `elem["role"]` below raises, and the test fails — which is the whole
    point: this helper must model the client so a double-encode regression
    can never pass green again.
    """
    out: list[list[dict]] = []
    text = b"".join(chunks).decode()
    for frame in text.split("\n\n"):
        data_lines = [
            line[len("data: ") :] for line in frame.splitlines() if line.startswith("data: ")
        ]
        if not data_lines:
            continue
        payload = json.loads("".join(data_lines))
        assert isinstance(payload, list), f"throttled frame is not a JSON array: {payload!r}"
        for elem in payload:
            assert isinstance(elem, dict), (
                f"throttled frame element is {type(elem).__name__}, not an object — "  # pyright: ignore[reportUnknownArgumentType]
                f"the server double-encoded the batch: {elem!r}"
            )
        out.append(payload)  # pyright: ignore[reportUnknownMemberType]
    return out


def test_throttled_batches_multiple_events(
    db_conn: psycopg.Connection,
    redis_client: sync_redis.Redis,
) -> None:
    """Every event from every agent flows through the broadcast stream.

    The throttle flushes on a fixed cadence, so three near-simultaneous
    publishes may batch into one frame or straddle a flush boundary into
    several — both are correct. The guarantee under test is *delivery of all
    three* (no agent_id / role filtering), so collect until all three events
    arrive rather than asserting a single-frame batch (which a timing window
    cannot promise under load — the old cause of this test's flake).
    """
    tid_a = create_agent(db_conn)
    tid_b = create_agent(db_conn)
    payloads = [
        ChatDelta(agent_id=tid_a, item_id="5.0", content="from A").model_dump_json(),
        CodeDelta(agent_id=tid_b, item_id="6.0", content="from B").model_dump_json(),
        ChatDelta(agent_id=tid_a, item_id="5.0", content="from A again").model_dump_json(),
    ]
    frames = asyncio.run(
        _collect_throttled_frames(
            redis_client, payloads, n_data_frames=1, throttle_rate=100.0, min_events=3
        )
    )
    decoded = _decode_throttled_frames(frames)
    assert len(decoded) >= 1  # pyright: ignore[reportUnknownArgumentType]
    # All 3 events should be present, however many frames they landed in
    all_events = [e for batch in decoded for e in batch]
    assert len(all_events) == 3  # pyright: ignore[reportUnknownArgumentType]
    roles = [e["role"] for e in all_events]
    assert "chat_delta" in roles
    assert "code_delta" in roles
    # Both agents' events are present (no agent_id filtering)
    agent_ids = {e["agent_id"] for e in all_events}
    assert tid_a in agent_ids
    assert tid_b in agent_ids


def test_throttled_no_agent_filter(
    db_conn: psycopg.Connection,
    redis_client: sync_redis.Redis,
) -> None:
    """Throttled stream does NOT filter by agent_id — events from all agents flow through."""
    tid_a = create_agent(db_conn)
    tid_b = create_agent(db_conn)
    payloads = [
        ChatDelta(agent_id=tid_a, item_id="5.0", content="A").model_dump_json(),
        ChatDelta(agent_id=tid_b, item_id="5.0", content="B").model_dump_json(),
    ]
    # min_events, not a frame count: the assertion below is about both events
    # arriving, and a time-windowed throttle can split two near-simultaneous
    # publishes across frames — stopping at the first frame then sees only agent A.
    frames = asyncio.run(
        _collect_throttled_frames(
            redis_client, payloads, n_data_frames=1, throttle_rate=1000.0, min_events=2
        )
    )
    decoded = _decode_throttled_frames(frames)
    all_events = [e for batch in decoded for e in batch]
    agent_ids = {e["agent_id"] for e in all_events}
    assert tid_a in agent_ids
    assert tid_b in agent_ids


def test_throttled_agent_filter(
    db_conn: psycopg.Connection,
    redis_client: sync_redis.Redis,
) -> None:
    """A filter keeps the selected agent plus system-level agent_id=0 events."""
    tid_a = create_agent(db_conn)
    tid_b = create_agent(db_conn)
    payloads = [
        ChatDelta(agent_id=tid_a, item_id="5.0", content="A").model_dump_json(),
        ChatDelta(agent_id=tid_b, item_id="5.0", content="B").model_dump_json(),
        LabelUpdated(agent_id=0, label="system").model_dump_json(),
    ]
    frames = asyncio.run(
        _collect_throttled_frames(
            redis_client,
            payloads,
            n_data_frames=1,
            throttle_rate=1000.0,
            min_events=2,
            agent_filter={tid_a},
        )
    )
    decoded = _decode_throttled_frames(frames)
    all_events = [e for batch in decoded for e in batch]
    assert [e["agent_id"] for e in all_events] == [tid_a, 0]
    assert all_events[0]["content"] == "A"
    assert all_events[1]["label"] == "system"


def test_throttled_no_role_filter(
    db_conn: psycopg.Connection,
    redis_client: sync_redis.Redis,
) -> None:
    """Throttled stream does NOT filter by role — both GLOBAL_ROLES and SYSTEM_ROLES events pass."""
    tid = create_agent(db_conn)
    payloads = [
        ChatDelta(agent_id=tid, item_id="5.0", content="delta").model_dump_json(),
        LabelUpdated(agent_id=tid, label="test").model_dump_json(),
    ]
    # Same reason as the agent-filter test above: both roles have to arrive, so
    # wait on the event count rather than on the first frame.
    frames = asyncio.run(
        _collect_throttled_frames(
            redis_client, payloads, n_data_frames=1, throttle_rate=1000.0, min_events=2
        )
    )
    decoded = _decode_throttled_frames(frames)
    all_events = [e for batch in decoded for e in batch]
    roles = {e["role"] for e in all_events}
    assert "chat_delta" in roles
    assert "label_updated" in roles


def test_throttled_wire_format_is_json_array(
    db_conn: psycopg.Connection,
    redis_client: sync_redis.Redis,
) -> None:
    """The data frame is a JSON array, not a single object."""
    tid = create_agent(db_conn)
    payloads = [
        ChatDelta(agent_id=tid, item_id="5.0", content="hello").model_dump_json(),
    ]
    frames = asyncio.run(
        _collect_throttled_frames(redis_client, payloads, n_data_frames=1, throttle_rate=1000.0)
    )
    # Extract the raw data payload
    text = b"".join(frames).decode()
    for frame in text.split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("data: "):
                payload = json.loads(line[len("data: ") :])
                assert isinstance(payload, list), f"Expected JSON array, got {type(payload)}"
                return
    pytest.fail("No data frame found")


def test_throttled_endpoint_response_headers(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/api/system/all endpoint response headers."""

    async def fake_stream(*_args: object, **_kwargs: object) -> AsyncIterator[bytes]:
        yield b": stream open\n\n"

    from gateway.routers import system as system_router

    monkeypatch.setattr(system_router, "throttled_event_stream", fake_stream)

    with TestClient(app) as client, client.stream("GET", "/api/system/all") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.headers["x-accel-buffering"] == "no"


def test_throttled_agent_filter_endpoint_query(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The query is parsed once and forwarded; malformed ids fail with 422."""
    tid = create_agent(db_conn)
    captured_kwargs: dict[str, object] = {}

    async def fake_stream(*_args: object, **kwargs: object) -> AsyncIterator[bytes]:
        captured_kwargs.update(kwargs)
        yield b": stream open\n\n"

    from gateway.routers import system as system_router

    monkeypatch.setattr(system_router, "throttled_event_stream", fake_stream)

    with TestClient(app) as client:
        with client.stream("GET", f"/api/system/all?agents={tid}") as resp:
            assert resp.status_code == 200
        assert captured_kwargs["agent_filter"] == {tid}

        invalid = client.get("/api/system/all?agents=abc")
        assert invalid.status_code == 422


def test_throttled_drops_invalid_payload(
    db_conn: psycopg.Connection,
    redis_client: sync_redis.Redis,
) -> None:
    """Invalid payloads are silently dropped, valid ones still batched."""
    tid = create_agent(db_conn)
    payloads = [
        json.dumps({"role": "made_up_role", "agent_id": tid}),
        ChatDelta(agent_id=tid, item_id="5.0", content="valid").model_dump_json(),
    ]
    frames = asyncio.run(
        _collect_throttled_frames(redis_client, payloads, n_data_frames=1, throttle_rate=1000.0)
    )
    decoded = _decode_throttled_frames(frames)
    all_events = [e for batch in decoded for e in batch]
    # Only the valid event makes it through
    assert len(all_events) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert all_events[0]["role"] == "chat_delta"
    assert all_events[0]["content"] == "valid"


def test_busy_channel_still_emits_keepalive_comments(
    db_conn: psycopg.Connection, redis_client: sync_redis.Redis
) -> None:
    """A channel flooded with other agents' events must not go silent.

    Regression (2026-08-03): event_stream only emitted ``: hb`` on the
    msg-None path, so a continuously busy ``ava:events`` channel (in a live
    cluster, a message every ~0.1s) made the stream yield nothing at all —
    read-timeout clients (httpx, curl) disconnected every 30s and the
    im_bridge subscription reconnected in a loop.
    """
    tid = create_agent(db_conn)  # this subscriber's agent
    other = create_agent(db_conn)  # whose events flood the channel

    req = _FakeRequest()
    gen = event_stream(  # type: ignore[arg-type]
        settings.data_plane.redis_url,
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
            settings.data_plane.redis_url,
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
