# pyright: reportUnknownMemberType = warning
# pyright: reportUnknownArgumentType = warning
# pyright: reportUnknownLambdaType = warning
# pyright: reportUnknownVariableType = warning
"""Upper-level grouping run by the consumer: due checks, sealing, the durable cursor, failures."""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from psycopg_pool import AsyncConnectionPool

from base.agents.history.hierarchy import group_consumer as gc
from base.agents.history.hierarchy.generate import GenerateError
from base.agents.history.hierarchy.group import Group, GroupCall, OpenNode, parse_groups
from base.agents.history.hierarchy.group_store import (
    GroupOrderError,
    claim_check,
    load_last_checked,
    load_open_nodes,
    write_groups,
)
from base.config import settings
from base.host.env.agent_slices import ModelOverrides

AGENT = 7


def _first(row: tuple | None) -> int:
    assert row is not None
    return int(row[0])


async def _add_leaf(pool: AsyncConnectionPool, i: int, depth: int = 1, agent: int = AGENT) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO understanding_nodes (agent_id, depth, span_start, span_end, start_ts, end_ts,"
            " segment_key, text, text_hash, input_hash, children_count, model, engine_version,"
            " prompt_version, schema_version)"
            " VALUES (%s, %s, %s, %s, %s, %s, 'k', %s, 'h', 'i', 0, 'm', 'chunk-0.2', 'p', 1) RETURNING id",
            (
                agent,
                depth,
                i * 10,
                i * 10 + 9,
                datetime(2026, 10, 5, tzinfo=UTC) + timedelta(hours=i),
                datetime(2026, 10, 5, tzinfo=UTC) + timedelta(hours=i, minutes=30),
                f"leaf {i}",
            ),
        )
        row = await cur.fetchone()
    return _first(row)


async def _fill(pool: AsyncConnectionPool, count: int, start: int = 0) -> list[int]:
    return [await _add_leaf(pool, i) for i in range(start, start + count)]


@pytest.fixture(autouse=True)
def _small_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cadence is configurable (default 60); these tests drive it with five."""
    monkeypatch.setattr(settings.agent, "understanding_group_check_open", 5)
    monkeypatch.setattr(settings.agent, "understanding_group_check_decay", 1)
    monkeypatch.setattr(gc, "MIN_CHECK_OPEN", 1)  # no floor, no decay: five at every level


@pytest.fixture
def _seams(monkeypatch: pytest.MonkeyPatch) -> dict:
    seen: dict = {"asked": [], "emitted": [], "reply": lambda _nodes: []}
    monkeypatch.setattr(
        gc, "_group_model", lambda *_a: ("deepseek-flash", ModelOverrides.from_pins(None))
    )

    def generate(
        _models: object,
        model: str,
        _o: object,
        _level: int,
        nodes: list[OpenNode],
        calls: list,
        _agent_id: int,
    ) -> list[Group]:
        seen["asked"].append([n.id for n in nodes])
        calls.append(GroupCall(0, model, "prompt", AIMessage(content="r"), 5.0, None, None))
        return seen["reply"](nodes)

    monkeypatch.setattr(gc, "_generate", generate)
    monkeypatch.setattr(gc.telemetry, "emit", lambda _k, name, **_kw: seen["emitted"].append(name))
    return seen


async def _parents(pool: AsyncConnectionPool) -> list[tuple]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT depth, span_start, span_end, children_count, text FROM understanding_nodes"
            " WHERE depth > 1 ORDER BY depth, span_start"
        )
        return await cur.fetchall()


def test_the_threshold_falls_per_level_down_to_a_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gc, "MIN_CHECK_OPEN", 6)
    monkeypatch.setattr(settings.agent, "understanding_group_check_open", 60)
    monkeypatch.setattr(settings.agent, "understanding_group_check_decay", 3)
    assert [gc.check_threshold(level) for level in (1, 2, 3, 4, 5, 9)] == [60, 20, 7, 6, 6, 6]
    monkeypatch.setattr(settings.agent, "understanding_group_check_open", 90)
    monkeypatch.setattr(settings.agent, "understanding_group_check_decay", 3)
    assert [gc.check_threshold(level) for level in (1, 2, 3, 4)] == [90, 30, 10, 6]
    monkeypatch.setattr(settings.agent, "understanding_group_check_decay", 1)
    assert [gc.check_threshold(level) for level in (1, 5)] == [90, 90]


def test_the_decay_defaults_to_three() -> None:
    assert type(settings.agent).model_fields["understanding_group_check_decay"].default == 3


def test_the_check_cadence_defaults_to_sixty_open_nodes() -> None:
    field = type(settings.agent).model_fields["understanding_group_check_open"]
    assert field.default == 60


async def test_the_cadence_setting_decides_when_a_level_is_due(
    aops_pool: AsyncConnectionPool, _seams: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.agent, "understanding_group_check_open", 8)
    await _fill(aops_pool, 7)
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT)
    assert _seams["asked"] == []
    await _add_leaf(aops_pool, 7)
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT)
    assert len(_seams["asked"]) == 1


async def test_nothing_is_asked_below_five_open_nodes(
    aops_pool: AsyncConnectionPool, _seams: dict
) -> None:
    await _fill(aops_pool, 4)
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT)
    assert _seams["asked"] == []


async def test_a_check_closes_groups_links_children_and_resets_the_baseline(
    aops_pool: AsyncConnectionPool, _seams: dict
) -> None:
    ids = await _fill(aops_pool, 6)
    _seams["reply"] = lambda nodes: [Group(nodes[0].id, nodes[2].id, "first three")]
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT)
    assert _seams["asked"] == [ids]
    assert await _parents(aops_pool) == [(2, 0, 29, 3, "first three")]
    assert [n.id for n in await load_open_nodes(aops_pool, AGENT, 1)] == ids[3:]
    assert await load_last_checked(aops_pool, AGENT, 1) == 3  # what stays open
    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM understanding_group_calls WHERE agent_id = %s", (AGENT,)
        )
        assert _first(await cur.fetchone()) == 1


async def test_a_declined_check_is_not_repeated_at_the_same_count(
    aops_pool: AsyncConnectionPool, _seams: dict
) -> None:
    await _fill(aops_pool, 5)
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT)
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT)
    assert len(_seams["asked"]) == 1 and await load_last_checked(aops_pool, AGENT, 1) == 5
    await _fill(aops_pool, 4, start=5)  # 9 open: still short of 10
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT)
    assert len(_seams["asked"]) == 1
    await _fill(aops_pool, 1, start=9)  # 10 open
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT)
    assert len(_seams["asked"]) == 2


async def test_closing_a_level_can_make_the_next_level_due(
    aops_pool: AsyncConnectionPool, _seams: dict
) -> None:
    for i in range(5):  # five level-2 nodes already waiting
        await _add_leaf(aops_pool, 100 + i, depth=2)
    await _fill(aops_pool, 5)
    _seams["reply"] = lambda nodes: (
        [Group(nodes[0].id, nodes[2].id, "g")] if nodes[0].text.startswith("leaf") else []
    )
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT)
    assert (
        len(_seams["asked"]) == 2
    )  # level 1 closed one group; level 2 (now 6 open) was asked next
    assert await load_last_checked(aops_pool, AGENT, 2) == 6


async def test_a_failed_check_emits_an_event_records_calls_and_waits_for_five_more(
    aops_pool: AsyncConnectionPool, _seams: dict
) -> None:
    await _fill(aops_pool, 5)

    def boom(_nodes: list[OpenNode]) -> list[Group]:
        raise GenerateError("refused after 2 corrections")

    _seams["reply"] = boom
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT)
    assert _seams["emitted"] == ["understanding_group_failed"]
    assert await load_last_checked(aops_pool, AGENT, 1) == 5
    assert await _parents(aops_pool) == []
    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM understanding_group_calls")
        assert _first(await cur.fetchone()) == 1  # the calls are recorded though the check failed
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT)
    assert len(_seams["asked"]) == 1  # same count, not asked again


async def test_a_live_lease_blocks_a_second_checker(
    aops_pool: AsyncConnectionPool, _seams: dict
) -> None:
    await _fill(aops_pool, 5)
    assert await claim_check(aops_pool, AGENT, 1)
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT)
    assert _seams["asked"] == []
    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE understanding_group_state SET claimed_at = now() - interval '1 hour'"
        )
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT)
    assert len(_seams["asked"]) == 1


async def test_a_crash_inside_a_check_frees_the_lease(
    aops_pool: AsyncConnectionPool, _seams: dict
) -> None:
    await _fill(aops_pool, 5)

    def crash(_nodes: list[OpenNode]) -> list[Group]:
        raise RuntimeError("bug")

    _seams["reply"] = crash
    await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT)  # never raises
    assert await claim_check(aops_pool, AGENT, 1)  # the lease was released


def test_the_configured_group_model_wins_over_the_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.agent, "understanding_group_model", "other-model")
    model, _overrides = gc._group_model(MagicMock(), 1)
    assert model == "other-model"


def test_without_a_configured_model_the_agents_own_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.agent, "understanding_group_model", "")
    monkeypatch.setattr(
        gc, "agent_model_target", lambda *_a, **_k: ("agent-model", ModelOverrides.from_pins(None))
    )
    assert gc._group_model(MagicMock(), 1)[0] == "agent-model"


def test_standalone_groupings_hand_the_reasoning_setting_to_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = MagicMock()
    captured: list[tuple] = []
    monkeypatch.setattr(gc, "generate_groups", lambda llm, *_a, **_k: captured.append((llm,)) or [])
    none = ModelOverrides.from_pins(None)
    for value in ("", "off", "high"):
        monkeypatch.setattr(settings.agent, "understanding_group_reasoning", value)
        gc._generate(models, "m", none, 1, [], [], 7)
        assert models.get.call_args.args == ("m", none, value)


# -- Concurrency: leases serialize one (agent, level); different agents run together -----------


def _slow_generate(seen: dict, pause: float = 0.3):
    def generate(
        _models: object,
        model: str,
        _o: object,
        _level: int,
        nodes: list[OpenNode],
        calls: list,
        _agent_id: int,
    ) -> list[Group]:
        seen["asked"].append((nodes[0].id, nodes[-1].id))
        time.sleep(pause)
        return []

    return generate


async def test_two_checkers_of_one_level_make_one_call(
    aops_pool: AsyncConnectionPool, _seams: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _fill(aops_pool, 5)
    monkeypatch.setattr(gc, "_generate", _slow_generate(_seams))
    await asyncio.gather(
        *(gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT) for _ in range(3))
    )
    assert len(_seams["asked"]) == 1  # the others found the lease held and moved on
    assert await claim_check(aops_pool, AGENT, 1)  # and it was released


async def test_different_agents_are_checked_at_the_same_time(
    aops_pool: AsyncConnectionPool, _seams: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    for agent in (7, 8):
        for i in range(5):
            await _add_leaf(aops_pool, i, agent=agent)
    both = threading.Barrier(2, timeout=10)  # passes only if the two calls overlap

    def generate(
        _models: object,
        model: str,
        _o: object,
        _level: int,
        nodes: list[OpenNode],
        calls: list,
        _agent_id: int,
    ) -> list[Group]:
        both.wait()
        return []

    monkeypatch.setattr(gc, "_generate", generate)
    await asyncio.gather(
        *(gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), a) for a in (7, 8))
    )
    for agent in (7, 8):
        assert await load_last_checked(aops_pool, agent, 1) == 5


async def test_the_baseline_counts_leaves_that_landed_while_the_call_ran(
    aops_pool: AsyncConnectionPool,
) -> None:
    ids = await _fill(aops_pool, 5)
    nodes = await load_open_nodes(aops_pool, AGENT, 1)
    assert await claim_check(aops_pool, AGENT, 1)
    await _add_leaf(aops_pool, 5)  # the same agent's next job landed a leaf meanwhile
    remaining = await write_groups(
        aops_pool, AGENT, 1, nodes, [Group(ids[0], ids[2], "g")], model="m", check_key="ck"
    )
    assert remaining == 3  # two of the snapshot plus the newcomer
    assert await load_last_checked(aops_pool, AGENT, 1) == 3
    assert await claim_check(aops_pool, AGENT, 1)  # released with the write


def test_an_open_set_past_three_checks_must_close_a_group(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []
    monkeypatch.setattr(gc, "generate_groups", lambda *_a, **k: seen.append(k["must_close"]) or [])
    none = ModelOverrides.from_pins(None)
    nodes = [
        OpenNode(i, 0, 1, datetime(2026, 10, 5, tzinfo=UTC), datetime(2026, 10, 5, tzinfo=UTC), "t")
        for i in range(15)
    ]
    monkeypatch.setattr(settings.agent, "understanding_group_check_open", 5)
    gc._generate(MagicMock(), "m", none, 1, nodes[:14], [], 7)
    gc._generate(MagicMock(), "m", none, 1, nodes, [], 7)
    monkeypatch.setattr(gc, "MIN_CHECK_OPEN", 6)
    monkeypatch.setattr(settings.agent, "understanding_group_check_open", 60)
    monkeypatch.setattr(settings.agent, "understanding_group_check_decay", 3)
    gc._generate(MagicMock(), "m", none, 2, nodes[:59], [], 7)  # level 2: 3 x 20 = 60
    gc._generate(MagicMock(), "m", none, 2, nodes * 4, [], 7)
    assert seen == [False, True, False, True]  # 14 < 3 x 5, 15 = 3 x 5; per level: 59 < 60 = 3 x 20


async def _tree(pool: AsyncConnectionPool) -> tuple[list[tuple], list[tuple]]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT id, depth, span_start, span_end, parent_id FROM understanding_nodes"
            " WHERE agent_id = %s ORDER BY depth, span_start",
            (AGENT,),
        )
        rows = await cur.fetchall()
    return rows, [r for r in rows if r[1] > 1]


async def test_single_groups_in_the_middle_close_and_no_level_ever_overlaps(
    aops_pool: AsyncConnectionPool, _seams: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review repro: open nodes 1..6, a reply of `1..1`, `2..4`, `5..5`. Closing only the
    middle group left nodes 1, 5, 6 open, and the next check grouped 1..5 into a parent that
    contained the already closed 2..4. Now the head single closes, only the trailing single stays
    open, and over many checks every level stays disjoint with each parent spanning exactly its
    children."""
    plans = [[(0, 0), (1, 3), (4, 4)], [(0, 1), (2, 2), (3, 5), (6, 6)], [(0, 0), (1, 1), (2, 4)]]
    state = {"n": 0}

    def generate(
        _models: object,
        model: str,
        _o: object,
        _level: int,
        nodes: list[OpenNode],
        calls: list,
        _agent_id: int,
    ) -> list[Group]:
        plan = [(a, b) for a, b in plans[state["n"] % len(plans)] if b < len(nodes) - 1]
        state["n"] += 1
        text = "".join(
            f'<group first="{nodes[a].id}" last="{nodes[b].id}">g{nodes[a].id}</group>'
            for a, b in plan
        )
        calls.append(GroupCall(0, model, "prompt", AIMessage(content=text), 5.0, None, None))
        return parse_groups(text, nodes, must_close=False)

    monkeypatch.setattr(gc, "_generate", generate)
    for i in range(60):
        await _add_leaf(aops_pool, i)
        await gc.run_group_checks(aops_pool, MagicMock(), MagicMock(), AGENT)
    rows, parents = await _tree(aops_pool)
    assert parents, "the plans must have closed some groups"
    by_parent: dict[int, list[tuple]] = {}
    for row in rows:
        if row[4] is not None:
            by_parent.setdefault(row[4], []).append(row)
    for depth in {r[1] for r in rows}:
        level = [r for r in rows if r[1] == depth]
        for left, right in pairwise(level):
            assert left[3] < right[2], f"level {depth}: {left} overlaps {right}"
    for parent in parents:
        kids = by_parent[parent[0]]
        assert (parent[2], parent[3]) == (min(k[2] for k in kids), max(k[3] for k in kids))


async def test_an_old_workers_node_is_never_an_open_node_of_the_tree(
    aops_pool: AsyncConnectionPool,
) -> None:
    ids = await _fill(aops_pool, 3)
    async with aops_pool.connection() as conn:
        await conn.execute(
            "UPDATE understanding_nodes SET engine_version = '0.3' WHERE id = %s", (ids[0],)
        )
    assert [n.id for n in await load_open_nodes(aops_pool, AGENT, 1)] == ids[1:]


async def test_a_group_may_not_close_across_an_earlier_open_node(
    aops_pool: AsyncConnectionPool,
) -> None:
    """A leaf of an earlier span landed after the snapshot: closing the later group would seal it
    inside the gap, so the write is refused and nothing changes."""
    ids = await _fill(aops_pool, 5, start=5)
    nodes = await load_open_nodes(aops_pool, AGENT, 1)
    late = await _add_leaf(aops_pool, 0)
    assert await claim_check(aops_pool, AGENT, 1)
    with pytest.raises(GroupOrderError, match=f"open node {late}"):
        await write_groups(
            aops_pool, AGENT, 1, nodes, [Group(ids[0], ids[2], "g")], model="m", check_key="ck"
        )
    assert await _parents(aops_pool) == []
    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM understanding_nodes WHERE parent_id IS NOT NULL")
        assert _first(await cur.fetchone()) == 0
