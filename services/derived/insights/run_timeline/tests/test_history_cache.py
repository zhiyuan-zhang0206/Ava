from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi import HTTPException
from psycopg import Connection

from services.derived.insights.run_timeline.projection.reader import TimelineReader
from services.derived.insights.run_timeline.projection.source import Source, capture_source
from services.derived.insights.run_timeline.tests.test_projection import AGENT, World, messages
from services.derived.insights.run_timeline.tests.test_projection import world as world


def test_concurrent_cold_readers_share_one_tail(world: World) -> None:
    head = world.put(messages(0), sealed=False)
    with ThreadPoolExecutor(max_workers=6) as workers:
        cuts = list(workers.map(world.reader.get, [AGENT] * 6))
    assert all(c is cuts[0] for c in cuts) and world.loads == [head]


def test_unchanged_head_keeps_a_snapshot_after_the_short_ttl(world: World) -> None:
    head = world.put(messages(0), sealed=False)
    first = world.reader.get(AGENT)
    world.clock = 6
    assert world.reader.get(AGENT) is first and world.loads == [head]


def test_new_head_rebuilds_only_the_tail(world: World) -> None:
    world.put(messages(0), sealed=True)
    head = world.put(messages(1), sealed=False)
    world.prepare()
    first = world.reader.get(AGENT)
    latest = world.put(messages(2), sealed=False)
    world.clock = 6
    assert world.reader.get(AGENT).id != first.id
    assert world.loads == [head, latest]


def test_expired_unknown_and_wrong_agent_snapshots_never_read_bodies(world: World) -> None:
    head = world.put(messages(0), sealed=False)
    first = world.reader.get(AGENT)
    for agent, snapshot in ((AGENT, "unknown"), (AGENT + 1, first.id)):
        with pytest.raises(HTTPException) as caught:
            world.reader.get(agent, snapshot)
        assert caught.value.status_code == 409
    world.clock = 601
    with pytest.raises(HTTPException) as caught:
        world.reader.get(AGENT, first.id)
    assert caught.value.status_code == 409 and world.loads == [head]
    assert world.reader.get(AGENT).id != first.id
    assert world.loads == [head, head]


def test_oldest_snapshot_is_evicted_after_sixteen_agents(world: World) -> None:
    first = world.reader.get(1)
    for agent in range(2, 18):
        world.reader.get(agent)
    with pytest.raises(HTTPException) as caught:
        world.reader.get(1, first.id)
    assert caught.value.status_code == 409


def test_recent_head_check_cannot_revive_an_absolutely_expired_snapshot(world: World) -> None:
    head = world.put(messages(0), sealed=False)
    first = world.reader.get(AGENT)
    world.clock = 599
    assert world.reader.get(AGENT) is first
    world.clock = 601
    with pytest.raises(HTTPException) as caught:
        world.reader.get(AGENT, first.id)
    assert caught.value.status_code == 409
    refreshed = world.reader.get(AGENT)
    assert refreshed.id != first.id
    assert world.reader.get(AGENT, refreshed.id) is refreshed
    assert world.loads == [head, head]


def test_head_check_cannot_restore_bookkeeping_for_an_evicted_snapshot(
    world: World,
) -> None:
    def evict_during_capture(conn: Connection[Any], agent_id: int) -> Source:
        source = capture_source(conn, agent_id)
        if agent_id == 1 and world.clock > 0:
            for other_agent in [*range(2, 17), 18]:
                reader.get(other_agent)
        return source

    reader = TimelineReader(
        world.db,
        world.pool,
        inputs=world.inputs,
        jobs=world.jobs,
        read_segment=world.read_segment,
        read_source=evict_during_capture,
        timer=lambda: world.clock,
    )
    first = reader.get(1)
    world.clock = 6
    assert reader.get(1) is first
    assert len(reader._entries) == 16
    assert reader._checked.keys() == reader._entries.keys()
    with pytest.raises(HTTPException) as caught:
        reader.get(1, first.id)
    assert caught.value.status_code == 409
