# pyright: reportUnknownMemberType = warning
# pyright: reportUnknownArgumentType = warning
# pyright: reportUnknownLambdaType = warning
# pyright: reportUnknownVariableType = warning
"""The understanding consumer loop: claim, locate, describe, store, settle."""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import MagicMock

import psycopg
import pytest
from langchain_core.messages import AIMessage, SystemMessage
from psycopg_pool import AsyncConnectionPool

from base.agents.history.checkpoint import CheckpointReadError
from base.agents.history.hierarchy import chunk_consumer as loop
from base.agents.history.hierarchy.chunk_generate import ChunkResult
from base.agents.history.hierarchy.chunks import (
    Chunk,
    ChunkCall,
    ChunkJob,
    LocatedChunk,
    enqueue_chunk,
)
from base.agents.history.hierarchy.generate import GenerateError
from base.agents.history.hierarchy.leaf_groups import UnitGroup
from base.agents.history.hierarchy.tests.consumer_helpers import read_inputs
from base.agents.history.hierarchy.tests.consumer_history import (
    inbound_history,
    job_spans,
    notes_history,
)
from base.agents.history.hierarchy.units import divide_units
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.config import settings
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog


def _result(
    located: LocatedChunk, *groups: UnitGroup, summary: str = "what happened"
) -> ChunkResult:
    """A model's answer over `located`'s units: the given groups, else one group of everything."""
    inputs = read_inputs()
    units = divide_units(
        list(located.messages),
        timeline_inputs=TimelineReadInputs(inputs.clock_factory, inputs.timestamps_enabled),
    )
    return ChunkResult(units, list(groups) or [UnitGroup(0, len(units) - 1, summary)])


@pytest.fixture(autouse=True)
def _seams(monkeypatch: pytest.MonkeyPatch) -> dict:
    seen: dict = {"described": []}
    monkeypatch.setattr(
        loop,
        "agent_model_target",
        lambda *_a, **_k: ("deepseek-flash", ModelOverrides.from_pins(None)),
    )
    monkeypatch.setattr(
        loop,
        "_load_segments",
        lambda _db, _agent_id, _boundary: (seen.get("history", inbound_history()), None),
    )

    def describe(
        _models: object,
        _model: str,
        _overrides: object,
        located: LocatedChunk,
        _tools: object,
        _calls: list,
        _agent_id: int,
        **kw: object,
    ) -> ChunkResult:
        seen["described"].append(located)
        seen["kw"] = kw
        return seen["answer"](located) if "answer" in seen else _result(located)

    monkeypatch.setattr(loop, "_describe", describe)
    return seen


async def _status(pool: AsyncConnectionPool) -> list[tuple]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT status, attempts, error FROM understanding_chunk_jobs ORDER BY id"
        )
        return await cur.fetchall()


async def _nodes(pool: AsyncConnectionPool) -> list[tuple]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT agent_id, depth, span_start, span_end, text FROM understanding_nodes"
        )
        return await cur.fetchall()


async def test_round_describes_a_due_chunk_and_stores_its_node(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, _seams: dict
) -> None:
    # Request list [head, m0, m1, m2]; chunk [1, 3) = m0, m1.
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await _run_rounds(aops_pool, 1, model_catalog=model_catalog)
    assert await _nodes(aops_pool) == [(5, 1, 0, 1, "what happened")]
    assert await _status(aops_pool) == [("done", 1, None)]
    assert [m.id for m in _seams["described"][0].messages] == ["m0", "m1"]
    await _run_rounds(
        aops_pool, 1, model_catalog=model_catalog
    )  # queue drained: nothing more to claim
    assert await _status(aops_pool) == [("done", 1, None)]


async def test_chunk_not_yet_checkpointed_goes_back_to_the_queue(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
) -> None:
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 9), end_msg_id="m8")
    await _run_rounds(aops_pool, 1, model_catalog=model_catalog)
    [(status, attempts, error)] = await _status(aops_pool)
    assert (status, attempts) == ("pending", 0) and "shorter" in error  # waiting spends no attempt
    assert await _nodes(aops_pool) == []


async def test_drifted_indices_fail_the_job_with_an_event(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitted: list[str] = []
    monkeypatch.setattr(loop.telemetry, "emit", lambda _kind, name, **_kw: emitted.append(name))
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="not-m1")
    await _run_rounds(aops_pool, 1, model_catalog=model_catalog)
    [(status, _, error)] = await _status(aops_pool)
    assert status == "failed" and "not-m1" in error
    assert "understanding_chunk_failed" in emitted


async def test_a_database_blink_puts_the_job_back_without_spending_an_attempt(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart of the database during a roll must not turn a paid-for job into a permanent
    failure: it goes back to the queue and the generation budget stays whole."""

    def blink(*_a: object) -> object:
        raise psycopg.OperationalError("the database closed the connection")

    monkeypatch.setattr(loop, "_load_segments", blink)
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await _run_rounds(aops_pool, 1, model_catalog=model_catalog)
    [(status, attempts, error)] = await _status(aops_pool)
    assert (status, attempts) == ("pending", 0) and "closed the connection" in error


async def test_a_released_job_waits_its_spacing_from_the_release_not_from_the_claim(
    aops_pool: AsyncConnectionPool,
) -> None:
    from base.agents.history.hierarchy.chunks import claim_job, release_job

    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    job = await claim_job(aops_pool)
    assert job is not None
    async with aops_pool.connection() as conn:  # a slow attempt: claimed long ago
        await conn.execute(
            "UPDATE understanding_chunk_jobs SET claimed_at = now() - interval '10 minutes'"
        )
    await release_job(aops_pool, job.id, error="generation failed")
    assert await claim_job(aops_pool) is None  # just released: not due for the spacing


async def test_a_chunk_that_overlaps_existing_nodes_describes_only_what_is_left(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, _seams: dict
) -> None:
    """A manual close and the producers' next size cut (or a replay after a restart) can cover
    the same stretch: the later job is shortened to the undescribed part, and skipped when
    nothing is left."""
    async with aops_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO understanding_nodes (agent_id, depth, span_start, span_end, segment_key,"
            " text, text_hash, input_hash, children_count, model, engine_version, prompt_version,"
            " schema_version) VALUES (5, 1, 0, 0, 'k', 'old', 'h', 'i', 0, 'm', 'chunk-0.2', 'p', 1)"
        )
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await _run_rounds(aops_pool, 1, model_catalog=model_catalog)
    assert [m.id for m in _seams["described"][0].messages] == ["m1"]  # m0 was already covered
    assert await _nodes(aops_pool) == [(5, 1, 0, 0, "old"), (5, 1, 1, 1, "what happened")]
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 2), end_msg_id="m0")
    await _run_rounds(aops_pool, 1, model_catalog=model_catalog)
    status = (await _status(aops_pool))[-1]
    assert status[0] == "skipped" and "already described" in status[2]


async def test_existing_nodes_in_the_middle_leave_a_gap_that_is_reported_not_dropped(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    _seams: dict,
) -> None:
    """A node covering the middle of a chunk splits it into two undescribed runs: the first is
    described, the other is named in an event (the old prefix-only trim lost it silently)."""
    events: list[tuple[str, dict[str, str] | None]] = []
    monkeypatch.setattr(
        loop.telemetry, "emit", lambda _k, name, attributes=None: events.append((name, attributes))
    )
    async with aops_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO understanding_nodes (agent_id, depth, span_start, span_end, segment_key,"
            " text, text_hash, input_hash, children_count, model, engine_version, prompt_version,"
            " schema_version) VALUES (5, 1, 1, 1, 'k', 'mid', 'h', 'i', 0, 'm', 'chunk-0.2', 'p', 1)"
        )
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 5), end_msg_id="m3")
    await _run_rounds(aops_pool, 1, model_catalog=model_catalog)
    assert [m.id for m in _seams["described"][0].messages] == ["m0"]
    assert {r[2:4] for r in await _nodes(aops_pool)} == {(0, 0), (1, 1)}
    [(name, attrs)] = events
    assert attrs is not None
    assert name == "understanding_chunk_gap" and attrs["gaps"] == "2-3"


async def test_an_old_workers_node_in_a_mixed_version_window_is_not_part_of_the_tree(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, _seams: dict
) -> None:
    """A node with a bare-number engine version (the retired worker's) neither covers a chunk nor
    shows up in the tree's reads."""
    async with aops_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO understanding_nodes (agent_id, depth, span_start, span_end, segment_key,"
            " text, text_hash, input_hash, children_count, model, engine_version, prompt_version,"
            " schema_version) VALUES (5, 1, 0, 3, 'k', 'old', 'h', 'i', 0, 'm', '0.3', '0.3', 1)"
        )
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 5), end_msg_id="m3")
    await _run_rounds(aops_pool, 1, model_catalog=model_catalog)
    assert [m.id for m in _seams["described"][0].messages] == ["m0", "m1", "m2", "m3"]


async def test_a_cancelled_job_is_put_back_without_spending_an_attempt(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host stops while a job runs (a rollout): the job goes back to the queue at once instead
    of making the next host wait out the 60-minute lease, and the give-up clock is untouched."""
    started = asyncio.Event()

    async def hang(*_a: object, **_k: object) -> loop.Outcome:
        started.set()
        await asyncio.sleep(3600)
        return loop.Outcome("done")

    monkeypatch.setattr(loop, "_run_job", hang)
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    consumer = loop._Consumer(
        aops_pool, MagicMock(), [], catalog=model_catalog, llm_override="", inputs=read_inputs()
    )
    task = asyncio.create_task(consumer.run_until_idle())
    await asyncio.wait_for(started.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [(status, attempts, error)] = await _status(aops_pool)
    assert (status, attempts, error) == ("pending", 0, "host stopping")


async def test_the_give_up_clock_starts_at_the_first_wait_and_a_queued_job_has_none(
    aops_pool: AsyncConnectionPool,
) -> None:
    from base.agents.history.hierarchy.chunks import claim_job, release_job

    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    async with aops_pool.connection() as conn:  # queued for days while the feature was off
        await conn.execute(
            "UPDATE understanding_chunk_jobs SET created_at = now() - interval '3 days'"
        )
    job = await claim_job(aops_pool)
    assert job is not None and job.age_seconds == 0.0
    await release_job(aops_pool, job.id, error="wait", count_attempt=False, waiting=True)
    async with aops_pool.connection() as conn:
        await conn.execute(
            "UPDATE understanding_chunk_jobs SET claimed_at = now() - interval '1 hour',"
            " waiting_since = now() - interval '2 hours'"
        )
    again = await claim_job(aops_pool)
    assert again is not None and 7000 < again.age_seconds < 7400
    await release_job(aops_pool, again.id, error="generation", count_attempt=True, waiting=False)
    async with aops_pool.connection() as conn:
        await conn.execute(
            "UPDATE understanding_chunk_jobs SET claimed_at = now() - interval '1 h'"
        )
    last = await claim_job(aops_pool)
    assert last is not None and last.age_seconds == 0.0  # a generation retry ends the wait


async def test_a_job_that_never_becomes_describable_is_given_up_on_by_age(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 9), end_msg_id="m8")
    async with aops_pool.connection() as conn:
        await conn.execute(
            "UPDATE understanding_chunk_jobs SET waiting_since = now() - interval '7 hours'"
        )
    monkeypatch.setattr(loop.telemetry, "emit", lambda *_a, **_k: None)
    await _run_rounds(aops_pool, 1, model_catalog=model_catalog)
    [(status, _, error)] = await _status(aops_pool)
    assert status == "failed" and "gave up" in error


async def test_a_closing_chunk_past_its_snapshot_describes_what_exists_and_reports_the_rest(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    _seams: dict,
) -> None:
    """The boundary snapshot of segment 0 holds fewer messages than the closing chunk's end (the
    checkpoint of the segment's last super-step was still in flight at compaction). The part the
    snapshot holds is described and written; the missing request indices are in the event."""
    events: list[tuple[str, dict[str, str] | None]] = []
    monkeypatch.setattr(
        loop.telemetry, "emit", lambda _k, name, attributes=None: events.append((name, attributes))
    )
    monkeypatch.setattr(loop, "_load_segments", lambda *_a: (inbound_history(), 0))
    await enqueue_chunk(
        aops_pool,
        5,
        compact_version=0,
        chunk=Chunk(1, 9),
        end_msg_id="m8",
        boundary_checkpoint_id="cp-1",
    )
    await _run_rounds(aops_pool, 1, model_catalog=model_catalog)
    [(status, _, error)] = await _status(aops_pool)
    assert (status, error) == ("done", None)
    assert [m.id for m in _seams["described"][0].messages] == ["m0", "m1", "m2", "m3"]
    assert len(await _nodes(aops_pool)) == 1
    [(name, attrs)] = events
    assert attrs is not None and name == "understanding_chunk_gap"
    assert "request 5-9 (not in the snapshot)" in attrs["gaps"]


async def test_generation_error_retries_then_fails_at_the_cap(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> str:
        raise GenerateError("provider down")

    monkeypatch.setattr(loop, "_describe", boom)
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    status = ""
    for attempt in range(1, loop.GENERATION_MAX_ATTEMPTS + 1):
        await _run_rounds(aops_pool, 1, model_catalog=model_catalog)
        [(status, attempts, _)] = await _status(aops_pool)
        assert attempts == attempt
        if status == "pending":
            async with (
                aops_pool.connection() as conn,
                conn.cursor() as cur,
            ):  # skip the retry spacing
                await cur.execute(
                    "UPDATE understanding_chunk_jobs SET claimed_at = now() - interval '1 hour'"
                )
    assert status == "failed"


async def test_gemini_chunk_is_described_with_its_original_prefix(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    _seams: dict,
) -> None:
    monkeypatch.setattr(
        loop,
        "agent_model_target",
        lambda *_a, **_kw: ("gemini-3.8-flash", ModelOverrides.from_pins(None)),
    )
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await _run_rounds(aops_pool, 1, model_catalog=model_catalog)
    assert await _status(aops_pool) == [("done", 1, None)]
    assert await _nodes(aops_pool) == [(5, 1, 0, 1, "what happened")]
    located = _seams["described"][0]
    assert located.prefix[0] == SystemMessage(content="head")
    assert [msg.id for msg in located.prefix[1:]] == ["m0", "m1"]


async def test_checkpoint_read_failure_is_retried(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_a: object) -> None:
        raise CheckpointReadError("db blip")

    monkeypatch.setattr(loop, "_load_segments", fail)
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await _run_rounds(aops_pool, 1, model_catalog=model_catalog)
    assert (await _status(aops_pool))[0][0] == "pending"


async def test_loop_is_idle_when_the_feature_is_off(
    model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.agent, "understanding_enabled", False)
    await loop.understanding_loop_forever(
        MagicMock(), MagicMock(), [], catalog=model_catalog, llm_override="", inputs=read_inputs()
    )  # returns at once


def test_generation_models_are_reused_and_never_closed(
    model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[MagicMock] = []

    def build(
        model: str,
        params: object,
        overrides: object,
        *,
        catalog: ModelCatalog,
        llm_override: str,
        thinking_off: bool,
    ) -> MagicMock:
        assert catalog is model_catalog
        assert llm_override == ""
        built.append(llm := MagicMock(name=model))
        llm.thinking_off, llm.params = thinking_off, params
        return llm

    monkeypatch.setattr(loop, "build_generation_llm", build)
    models = loop.ModelCache(catalog=model_catalog, llm_override="")
    none = ModelOverrides.from_pins(None)
    first = models.get("m", none)
    assert models.get("m", none) is first
    assert models.get("other", none) is not first
    assert len(built) == 2
    first.close.assert_not_called()
    # the grouping reasoning knob: empty leaves the model's tier alone, `off` disables
    # thinking, anything else is the effort asked; each is its own cached model.
    off, high = models.get("m", none, "off"), models.get("m", none, "high")
    assert off is not first and high is not first and off is not high
    assert first.thinking_off is False and first.params is None
    assert off.thinking_off is True and off.params is None
    assert high.thinking_off is False and high.params.reasoning_effort == "high"


def _call(round_: int, response: AIMessage | None, error: str | None = None) -> ChunkCall:
    return ChunkCall(
        round=round_,
        model="deepseek-flash",
        instruction="describe it",
        prefix_len=3,
        start_offset=1,
        response=response,
        duration_ms=12.5,
        error=error,
    )


async def _calls(pool: AsyncConnectionPool) -> list[tuple]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT job_id, agent_id, attempt, round, model, instruction, prefix_len,"
            " start_offset, content, tool_calls, usage_metadata, response_metadata,"
            " duration_ms, error FROM understanding_chunk_calls ORDER BY id"
        )
        return await cur.fetchall()


async def test_raw_calls_are_persisted_for_a_done_job(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    _seams: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply = AIMessage(
        content=[{"type": "thinking", "thinking": "hm"}, {"type": "text", "text": "what"}],
        usage_metadata={"input_tokens": 9, "output_tokens": 2, "total_tokens": 11},
        response_metadata={"stop_reason": "end_turn"},
    )

    def describe(*args: object, **_k: object) -> ChunkResult:
        args[5].append(_call(0, reply))  # type: ignore[attr-defined]
        return _result(args[3])  # type: ignore[arg-type]

    monkeypatch.setattr(loop, "_describe", describe)
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await _run_rounds(aops_pool, 1, model_catalog=model_catalog)
    [row] = await _calls(aops_pool)
    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT id FROM understanding_chunk_jobs")
        [(job_id,)] = await cur.fetchall()
    assert row[:8] == (job_id, 5, 1, 0, "deepseek-flash", "describe it", 3, 1)
    assert row[8] == [{"type": "thinking", "thinking": "hm"}, {"type": "text", "text": "what"}]
    assert row[10]["input_tokens"] == 9 and row[11] == {"stop_reason": "end_turn"}
    assert row[12:] == (12.5, None)


async def test_a_failed_call_is_recorded_and_the_job_still_retries(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **_k: object) -> str:
        args[5].append(_call(0, None, "provider down"))  # type: ignore[attr-defined]
        raise GenerateError("provider down")

    monkeypatch.setattr(loop, "_describe", boom)
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await _run_rounds(aops_pool, 1, model_catalog=model_catalog)
    [row] = await _calls(aops_pool)
    assert row[8] is None and row[13] == "provider down"
    assert (await _status(aops_pool))[0][0] == "pending"


async def test_a_record_write_failure_emits_an_event_and_spares_the_job(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    _seams: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[str] = []
    monkeypatch.setattr(loop.telemetry, "emit", lambda _kind, name, **_kw: emitted.append(name))
    from base.agents.history.hierarchy import chunks

    monkeypatch.setattr(chunks, "_INSERT_CALL_SQL", "INSERT INTO no_such_table VALUES (1)")

    def describe(*args: object, **_k: object) -> ChunkResult:
        args[5].append(_call(0, AIMessage(content="x")))  # type: ignore[attr-defined]
        return _result(args[3])  # type: ignore[arg-type]

    monkeypatch.setattr(loop, "_describe", describe)
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await _run_rounds(aops_pool, 1, model_catalog=model_catalog)
    assert "understanding_call_record_failed" in emitted
    assert await _status(aops_pool) == [("done", 1, None)]
    assert await _nodes(aops_pool) == [(5, 1, 0, 1, "what happened")]


async def _run_rounds(
    pool: AsyncConnectionPool, count: int, *, model_catalog: ModelCatalog
) -> None:
    """`count` times: claim at most one job and finish it (the loop's claim, one step at a time)."""
    for _ in range(count):
        async with asyncio.TaskGroup() as tg:
            await loop._Consumer(
                pool, MagicMock(), [], catalog=model_catalog, llm_override="", inputs=read_inputs()
            ).claim(tg)


async def test_each_group_is_a_node_covering_the_chunk_to_its_end(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, _seams: dict
) -> None:
    # Request [head, m0..m3]; chunk [1, 5): four units in three groups.
    _seams["answer"] = lambda located: _result(
        located, UnitGroup(0, 0, "first"), UnitGroup(1, 2, "second"), UnitGroup(3, 3, "third")
    )
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 5), end_msg_id="m3")
    await _run_rounds(aops_pool, 1, model_catalog=model_catalog)
    assert await _nodes(aops_pool) == [
        (5, 1, 0, 0, "first"),
        (5, 1, 1, 2, "second"),
        (5, 1, 3, 3, "third"),
    ]
    assert await job_spans(aops_pool) == [("done", 1, 5)]
    assert set(_seams["kw"]) == {"inputs"}  # no open / closing arguments
    assert isinstance(_seams["kw"]["inputs"], loop.UnderstandingReadInputs)


async def test_each_chunk_starts_at_its_own_start_whatever_the_previous_one_did(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, _seams: dict
) -> None:
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(3, 5), end_msg_id="m3")
    await _run_rounds(aops_pool, 2, model_catalog=model_catalog)
    assert [[m.id for m in loc.messages] for loc in _seams["described"]] == [
        ["m0", "m1"],
        ["m2", "m3"],
    ]
    assert await _nodes(aops_pool) == [(5, 1, 0, 1, "what happened"), (5, 1, 2, 3, "what happened")]


async def test_a_refused_grouping_retries_then_fails_the_job_without_nodes(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(*_a: object, **_k: object) -> str:
        raise GenerateError("grouping reply refused after 2 correction(s): no <groups>")

    monkeypatch.setattr(loop, "_describe", refused)
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    for _ in range(loop.GENERATION_MAX_ATTEMPTS):
        await _run_rounds(aops_pool, 1, model_catalog=model_catalog)
        async with aops_pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE understanding_chunk_jobs SET claimed_at = now() - interval '1 hour'"
            )
    [(status, attempts, error)] = await _status(aops_pool)
    assert (status, attempts) == ("failed", loop.GENERATION_MAX_ATTEMPTS) and "refused" in error
    assert await _nodes(aops_pool) == []


async def test_a_done_job_is_followed_by_the_upper_level_checks(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[int] = []

    async def standalone(
        _pool: object, _db: object, _models: object, agent_id: int, **_kw: object
    ) -> None:
        asked.append(agent_id)

    monkeypatch.setattr(loop, "run_group_checks", standalone)
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await _run_rounds(aops_pool, 1, model_catalog=model_catalog)
    assert asked == [5]


# -- Concurrency: bounded, per agent in order, failures isolated --------------------------------


async def _enqueue_ends(pool: AsyncConnectionPool, agent: int, *ends: str) -> None:
    """Jobs of one agent over the test history (request [head, m0..m3]); `m1` ends chunk (1, 3)."""
    spans = {"m1": Chunk(1, 3), "m2": Chunk(1, 4), "m3": Chunk(3, 5)}
    for end in ends:
        await enqueue_chunk(pool, agent, compact_version=0, chunk=spans[end], end_msg_id=end)


async def test_different_agents_run_together_and_one_agents_jobs_in_order(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, _seams: dict
) -> None:
    log: list[tuple[str, str]] = []
    lock = threading.Lock()
    b_started = threading.Event()

    def answer(located: LocatedChunk) -> ChunkResult:
        tag = {"m1": "a1", "m2": "b", "m3": "a2"}[str(located.messages[-1].id)]
        with lock:
            log.append(("start", tag))
        if tag == "b":
            b_started.set()
        if tag == "a1":
            assert b_started.wait(10), "agent b never ran while agent a's first job was in flight"
        time.sleep(0.05)
        with lock:
            log.append(("end", tag))
        return _result(located)

    _seams["answer"] = answer
    await _enqueue_ends(aops_pool, 1, "m1", "m3")  # agent 1: a1 then a2
    await _enqueue_ends(aops_pool, 2, "m2")  # agent 2: b
    await loop._Consumer(
        aops_pool, MagicMock(), [], catalog=model_catalog, llm_override="", inputs=read_inputs()
    ).run_until_idle()
    assert [s for s, *_ in await _status(aops_pool)] == ["done"] * 3
    assert log.index(("start", "b")) < log.index(("end", "a1"))  # overlapped
    assert log.index(("end", "a1")) < log.index(("start", "a2"))  # the same agent, in order


async def test_every_due_job_runs_at_once_each_on_its_own_thread(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, _seams: dict
) -> None:
    barrier = threading.Barrier(5, timeout=10)

    def answer(located: LocatedChunk) -> ChunkResult:
        barrier.wait()  # passes only when all five jobs are inside their calls together
        return _result(located)

    _seams["answer"] = answer
    for agent in range(1, 6):
        await _enqueue_ends(aops_pool, agent, "m1")
    consumer = loop._Consumer(
        aops_pool, MagicMock(), [], catalog=model_catalog, llm_override="", inputs=read_inputs()
    )
    await consumer.run_until_idle()
    assert [s for s, *_ in await _status(aops_pool)] == ["done"] * 5
    assert consumer.in_flight == 0


async def test_one_jobs_failure_touches_no_other_job(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, _seams: dict
) -> None:
    def answer(located: LocatedChunk) -> ChunkResult:
        if located.messages[-1].id == "m2":
            raise RuntimeError("bug in one job")  # not a GenerateError: the crash path
        return _result(located)

    _seams["answer"] = answer
    for agent, end in ((1, "m1"), (2, "m2"), (3, "m1")):
        await _enqueue_ends(aops_pool, agent, end)
    await loop._Consumer(
        aops_pool, MagicMock(), [], catalog=model_catalog, llm_override="", inputs=read_inputs()
    ).run_until_idle()
    status = [(s, e) for s, _, e in await _status(aops_pool)]
    assert [s for s, _ in status] == ["done", "failed", "done"]
    assert "bug in one job" in str(status[1][1])


async def test_a_job_that_cannot_be_settled_is_left_to_its_lease_and_others_finish(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = loop._settle

    async def settle(pool: AsyncConnectionPool, job: ChunkJob, outcome: loop.Outcome) -> None:
        if job.agent_id == 1:
            raise ConnectionError("database went away")
        await real(pool, job, outcome)

    monkeypatch.setattr(loop, "_settle", settle)
    for agent in (1, 2):
        await _enqueue_ends(aops_pool, agent, "m1")
    consumer = loop._Consumer(
        aops_pool, MagicMock(), [], catalog=model_catalog, llm_override="", inputs=read_inputs()
    )
    await consumer.run_until_idle()  # does not raise
    assert [s for s, *_ in await _status(aops_pool)] == ["running", "done"]
    assert consumer.in_flight == 0


async def test_the_backlog_event_carries_in_flight(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict] = []
    monkeypatch.setattr(
        loop.telemetry, "emit", lambda _k, _name, attributes=None: seen.append(attributes or {})
    )
    await _enqueue_ends(aops_pool, 1, "m1")
    consumer = loop._Consumer(
        aops_pool, MagicMock(), [], catalog=model_catalog, llm_override="", inputs=read_inputs()
    )
    consumer.in_flight = 1
    await consumer.emit_backlog()
    assert seen[-1]["pending"] == 1 and seen[-1]["in_flight"] == 1


async def test_the_forever_loop_works_the_queue_and_stops_cleanly_on_cancel(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.agent, "understanding_enabled", True)
    monkeypatch.setattr(loop, "POLL_SECONDS", 0.05)
    for agent in (1, 2, 3):
        await _enqueue_ends(aops_pool, agent, "m1")
    task = asyncio.create_task(
        loop.understanding_loop_forever(
            aops_pool, MagicMock(), [], catalog=model_catalog, llm_override="", inputs=read_inputs()
        )
    )
    for _ in range(100):
        if [s for s, *_ in await _status(aops_pool)] == ["done"] * 3:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [s for s, *_ in await _status(aops_pool)] == ["done"] * 3


async def test_a_replay_describes_one_agents_segments_together_and_skips_the_upper_checks(
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    _seams: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[int] = []

    async def standalone(*_a: object, **_k: object) -> None:
        asked.append(1)

    monkeypatch.setattr(loop, "run_group_checks", standalone)
    both = threading.Barrier(2, timeout=10)  # passes only when both segments are in flight

    def answer(located: LocatedChunk) -> ChunkResult:
        both.wait()
        return _result(located)

    _seams["answer"] = answer
    for segment in (0, 1):
        await enqueue_chunk(
            aops_pool, 1, compact_version=segment, chunk=Chunk(1, 3), end_msg_id="m1"
        )
    await enqueue_chunk(aops_pool, 2, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await loop.replay_jobs(
        aops_pool, MagicMock(), [], 1, catalog=model_catalog, llm_override="", inputs=read_inputs()
    )
    assert [s for s, *_ in await _status(aops_pool)] == ["done", "done", "pending"]
    assert asked == []


async def test_a_chunk_of_only_framework_notes_is_skipped_without_a_call(
    model_catalog: ModelCatalog, aops_pool: AsyncConnectionPool, _seams: dict
) -> None:
    _seams["history"] = notes_history()
    await enqueue_chunk(aops_pool, 5, compact_version=0, chunk=Chunk(1, 3), end_msg_id="m1")
    await _run_rounds(aops_pool, 1, model_catalog=model_catalog)
    [(status, _, error)] = await _status(aops_pool)
    assert status == "skipped" and "only framework notes" in error
    assert _seams["described"] == [] and await _nodes(aops_pool) == []
