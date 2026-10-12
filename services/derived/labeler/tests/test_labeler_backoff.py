"""Label dispatch skips prior attempts without an automatic retry ladder."""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg
import pytest

from base.config import settings
from base.config.service_read import ConfigAuthority
from base.daemon.health import Liveness
from base.db import create_agent, pool
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from services.derived.labeler import daemon
from services.derived.labeler.tests.slices import labeler_db


def _seed_chat(db: psycopg.Connection, tid: int) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, %s, 'chat', 'user')",
            (tid, "do the thing"),
        )


@pytest.mark.asyncio
async def test_dispatch_loop_uses_labeler_model_not_main_model(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    """The root builds the slice from settings.lm.labeler_model (its own knob), not
    settings.lm.llm_model (the main reasoning model). Pin the two to different
    values and assert the model the poll loop's slice carries is the labeler
    one — guards against a future edit reverting the daemon to llm_model."""
    monkeypatch.setattr(settings.lm, "llm_model", "deepseek-v4-pro")
    monkeypatch.setattr(settings.lm, "labeler_model", "deepseek-v4-flash")
    config = daemon.labeler_config()
    monkeypatch.setattr(daemon, "_POLL_INTERVAL_S", 0.0)

    a = create_agent(db_conn)
    _seed_chat(db_conn, a)
    db_conn.commit()

    captured: list[str] = []

    async def _capture(
        _tid: int,
        _prompt: str,
        cfg: Any,
        _db: object,
        _bus: EventBus,
        *,
        catalog: ModelCatalog,
        llm_override: str,
        overrides: ModelOverrides,
    ) -> None:
        assert catalog is model_catalog
        captured.append(cfg.labeler_model)
        # Break the otherwise-infinite poll loop after the first dispatch.
        raise asyncio.CancelledError

    monkeypatch.setattr(daemon, "generate_label_async", _capture)

    p = pool(gate=database_gate)
    try:
        with pytest.raises(asyncio.CancelledError):
            await daemon._dispatch_loop(
                p,
                labeler_db(database_gate=database_gate),
                event_bus,
                Liveness(daemon._LIVENESS_TIMEOUT_S),
                config,
                catalog=model_catalog,
                llm_override=config_authority.runtime.lm.llm_override,
                overrides=ModelOverrides.from_pins({}),
            )
    finally:
        p.close()

    assert captured == ["deepseek-v4-flash"]


def test_select_unlabeled_excludes_attempted_ids(db_conn: psycopg.Connection) -> None:
    """The SQL-level exclusion (`AND NOT (t.id = ANY(%s::bigint[]))`) works for
    both an empty attempted list and a non-empty one — empty selects all, a
    populated list drops exactly those ids from the LIMIT window."""
    a = create_agent(db_conn)
    b = create_agent(db_conn)
    _seed_chat(db_conn, a)
    _seed_chat(db_conn, b)

    with db_conn.cursor() as cur:
        # Empty attempted -> the array param is `'{}'::bigint[]`, excludes nothing.
        all_ids = {tid for tid, _prompt in daemon._select_unlabeled(cur, [])}
        assert {a, b} <= all_ids

        # a is attempted -> excluded; b still selectable.
        remaining = {tid for tid, _prompt in daemon._select_unlabeled(cur, [a])}
        assert a not in remaining
        assert b in remaining


def _set_label(db: psycopg.Connection, tid: int, label: str) -> None:
    with db.cursor() as cur:
        cur.execute("UPDATE agents SET label = %s WHERE id = %s", (label, tid))


def _seed_task_note(db: psycopg.Connection, tid: int, content: str) -> None:
    """Seed the task-assignment inbound shape: a system_note carrying
    note_tag='task' (how create_and_assign briefs reach a new worker since
    the 2026-08-27 task-notification change)."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, %s, 'system_note', 'agent:405', %s::jsonb)",
            (tid, content, '{"note_tag": "task"}'),
        )


def test_select_unlabeled_treats_empty_label_as_unset(db_conn: psycopg.Connection) -> None:
    """A stray label='' row is the same "unset" state as NULL: it must be
    selected for re-labeling. Regression: the poll's `label IS NULL`
    predicate skipped '' rows forever, wedging them out of auto-labeling
    (the labeler's CAS matches the same predicate, so a selected '' row is
    overwritten, never skipped)."""
    a = create_agent(db_conn)
    _set_label(db_conn, a, "")
    _seed_chat(db_conn, a)
    b = create_agent(db_conn)
    _set_label(db_conn, b, "")  # '' but no request-bearing inbound -> EXISTS excludes
    c = create_agent(db_conn)
    _seed_chat(db_conn, c)
    _set_label(db_conn, c, "already labeled")

    with db_conn.cursor() as cur:
        picked = {tid for tid, _prompt in daemon._select_unlabeled(cur, [])}
    assert a in picked
    assert b not in picked
    assert c not in picked


def test_select_unlabeled_prompts_from_task_system_note(db_conn: psycopg.Connection) -> None:
    """Task-assignment briefs arrive as system_note inbounds (note_tag='task'),
    not chat: a delegated worker whose only inbound is its assignment must
    still be selected, with the note content as the label prompt. Regression
    since the 2026-08-27 task-notification change left such workers without a
    labelable prompt. Non-task system notes stay excluded."""
    a = create_agent(db_conn)
    _seed_task_note(db_conn, a, 'Task #2669 "ava.ui docs gap" is now assigned to you.')
    b = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, %s, 'system_note', 'system', %s::jsonb)",
            (b, "impersonation rolled back", '{"impersonation_id": "x"}'),
        )
    db_conn.commit()

    with db_conn.cursor() as cur:
        rows = dict(daemon._select_unlabeled(cur, []))
    assert rows[a] == 'Task #2669 "ava.ui docs gap" is now assigned to you.'
    assert b not in rows


async def test_failed_label_is_not_retried_but_new_agents_are_processed(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    failed = create_agent(db_conn)
    _seed_chat(db_conn, failed)
    db_conn.commit()
    calls: list[int] = []
    fresh: list[int] = []
    ticks = 0

    async def poll(_delay: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks == 3:
            fresh.append(create_agent(db_conn))
            _seed_chat(db_conn, fresh[0])
            db_conn.commit()
        if ticks == 5:
            raise asyncio.CancelledError

    async def failed_generation(tid: int, *_args: Any, **_kwargs: Any) -> bool:
        calls.append(tid)
        return False

    monkeypatch.setattr(daemon.asyncio, "sleep", poll)
    monkeypatch.setattr(daemon, "generate_label_async", failed_generation)
    p = pool(gate=database_gate)
    try:
        with pytest.raises(asyncio.CancelledError):
            await daemon._dispatch_loop(
                p,
                labeler_db(database_gate=database_gate),
                event_bus,
                Liveness(120),
                daemon.labeler_config(),
                catalog=model_catalog,
                llm_override=config_authority.runtime.lm.llm_override,
                overrides=ModelOverrides.from_pins({}),
            )
    finally:
        p.close()
    assert calls == [failed, fresh[0]]
    assert db_conn.execute("SELECT label FROM agents WHERE id = %s", (failed,)).fetchone() == (
        None,
    )
