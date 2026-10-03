"""SSE throttled stream (`throttled_event_stream`) and frame-shape regressions: batching, agent/role filters, wire format, keepalive under load, unicode line separators; split from gateway/tests/test_sse.py (task #4922)."""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

import psycopg
import pytest
import redis as sync_redis
from fastapi.testclient import TestClient

from base.config import settings
from base.db import create_agent
from base.events.live.bus import EventBus
from base.events.live.projection import ChatDelta, CodeDelta, LabelUpdated
from gateway.app import app
from gateway.events.sse import (
    _decode_frames_for_test,
    _sse_batch_frame,
    _sse_frame,
    throttled_event_stream,
)
from gateway.tests.test_sse import _FakeRequest
from gateway.tests.test_sse import redis_client as redis_client


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
        EventBus.from_settings(),
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
        # Split/rejoin exactly like the browser: "\n" only. str.splitlines() would also break on U+0085 / U+2028 /
        # U+2029, masking a frame the writer split there instead of failing the parse.
        data_lines = [
            line[len("data: ") :] for line in frame.split("\n") if line.startswith("data: ")
        ]
        if not data_lines:
            continue
        payload = json.loads("\n".join(data_lines))
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
    # min_events, not a frame count: the assertion below is about both events arriving, and a time-windowed throttle can
    # split two near-simultaneous publishes across frames — stopping at the first frame then sees only agent A.
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
        for line in frame.split("\n"):
            if line.startswith("data: "):
                payload = json.loads(line[len("data: ") :])
                assert isinstance(payload, list), f"Expected JSON array, got {type(payload)}"
                return
    pytest.fail("No data frame found")


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

    from gateway.events import system as system_router

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


@pytest.mark.parametrize("ch", ("\u0085", "\u2028", "\u2029"), ids=("U+0085", "U+2028", "U+2029"))
def test_sse_frame_keeps_unicode_line_separators(ch: str) -> None:
    """One frame, one data line; the payload round-trips unchanged."""
    payload = json.dumps({"content": f"a{ch}b"}, ensure_ascii=False)
    frame = _sse_frame(payload)
    assert frame.decode().count("\ndata: ") == 0
    assert _decode_frames_for_test([frame]) == [{"content": f"a{ch}b"}]


@pytest.mark.parametrize("ch", ("\u0085", "\u2028", "\u2029"), ids=("U+0085", "U+2028", "U+2029"))
def test_sse_batch_frame_keeps_unicode_line_separators(ch: str) -> None:
    """A poisoned event must not split, and thereby corrupt, the batch."""
    events = [
        json.dumps({"content": f"a{ch}b"}, ensure_ascii=False),
        json.dumps({"content": "ok"}, ensure_ascii=False),
    ]
    frame = _sse_batch_frame(events)
    assert frame.decode().count("\ndata: ") == 0
    assert _decode_throttled_frames([frame]) == [[{"content": f"a{ch}b"}, {"content": "ok"}]]


def test_decode_frames_for_test_rejects_split_payload() -> None:
    """The test helper models the browser: a frame whose data was split at
    a Unicode line separator (the old bug) must fail the parse here."""
    poisoned = json.dumps({"content": "a\u2028b"}, ensure_ascii=False)
    split_frame = ("data: " + poisoned.replace("\u2028", "\ndata: ") + "\n\n").encode()
    with pytest.raises(json.JSONDecodeError):
        _decode_frames_for_test([split_frame])


def test_throttled_stream_keeps_unicode_line_separators(
    db_conn: psycopg.Connection,
    redis_client: sync_redis.Redis,
) -> None:
    """All three chars in one event survive the throttled wire path."""
    tid = create_agent(db_conn)
    content = "a\u0085b\u2028c\u2029d"
    payloads = [
        ChatDelta(agent_id=tid, item_id="5.0", content=content).model_dump_json(),
    ]
    frames = asyncio.run(
        _collect_throttled_frames(redis_client, payloads, n_data_frames=1, throttle_rate=1000.0)
    )
    decoded = _decode_throttled_frames(frames)
    all_events = [e for batch in decoded for e in batch]
    assert len(all_events) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert all_events[0]["content"] == content
