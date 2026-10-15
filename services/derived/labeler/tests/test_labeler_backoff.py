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
    monkeypatch.setattr(settings.lm, "llm_model", "deepseek-v4-pro")
    monkeypatch.setattr(settings.lm, "labeler_model", "deepseek-v4-flash")
    config = daemon.labeler_config()
    monkeypatch.setattr(daemon, "POLL_INTERVAL_S", 0.0)

    a = create_agent(db_conn)
    _seed_chat(db_conn, a)
    db_conn.commit()

    captured: list[str] = []

    def _reject_build(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("stubbed generate_label_async must not call build_chat_model")

    async def _capture(
        _tid: int,
        _prompt: str,
        cfg: Any,
        _db: object,
        _bus: EventBus,
        *,
        build_chat_model: object,
        catalog: ModelCatalog,
        llm_override: str,
        overrides: ModelOverrides,
    ) -> None:
        assert build_chat_model is _reject_build
        assert catalog is model_catalog
        captured.append(cfg.labeler_model)
        raise asyncio.CancelledError

    monkeypatch.setattr(daemon, "generate_label_async", _capture)

    p = pool(gate=database_gate)
    try:
        with pytest.raises(asyncio.CancelledError):
            await daemon.dispatch_loop(
                p,
                labeler_db(database_gate=database_gate),
                event_bus,
                Liveness(daemon.LIVENESS_TIMEOUT_S),
                config,
                build_chat_model=_reject_build,
                catalog=model_catalog,
                llm_override=config_authority.runtime.lm.llm_override,
                overrides=ModelOverrides.from_pins({}),
            )
    finally:
        p.close()

    assert captured == ["deepseek-v4-flash"]


def test_select_unlabeled_excludes_attempted_ids(db_conn: psycopg.Connection) -> None:
    a = create_agent(db_conn)
    b = create_agent(db_conn)
    _seed_chat(db_conn, a)
    _seed_chat(db_conn, b)

    with db_conn.cursor() as cur:
        all_ids = {tid for tid, _prompt in daemon.select_unlabeled(cur, [])}
        assert {a, b} <= all_ids

        remaining = {tid for tid, _prompt in daemon.select_unlabeled(cur, [a])}
        assert a not in remaining
        assert b in remaining


def _set_label(db: psycopg.Connection, tid: int, label: str) -> None:
    with db.cursor() as cur:
        cur.execute("UPDATE agents SET label = %s WHERE id = %s", (label, tid))


def _seed_task_note(db: psycopg.Connection, tid: int, content: str) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, %s, 'system_note', 'agent:405', %s::jsonb)",
            (tid, content, '{"note_tag": "task"}'),
        )


def test_select_unlabeled_treats_empty_label_as_unset(db_conn: psycopg.Connection) -> None:
    a = create_agent(db_conn)
    _set_label(db_conn, a, "")
    _seed_chat(db_conn, a)
    b = create_agent(db_conn)
    _set_label(db_conn, b, "")
    c = create_agent(db_conn)
    _seed_chat(db_conn, c)
    _set_label(db_conn, c, "already labeled")

    with db_conn.cursor() as cur:
        picked = {tid for tid, _prompt in daemon.select_unlabeled(cur, [])}
    assert a in picked
    assert b not in picked
    assert c not in picked


def test_select_unlabeled_prompts_from_task_system_note(db_conn: psycopg.Connection) -> None:
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
        rows = dict(daemon.select_unlabeled(cur, []))
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

    def _reject_build(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("stubbed generate_label_async must not call build_chat_model")

    async def poll(_delay: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks == 3:
            fresh.append(create_agent(db_conn))
            _seed_chat(db_conn, fresh[0])
            db_conn.commit()
        if ticks == 5:
            raise asyncio.CancelledError

    async def failed_generation(tid: int, *_args: Any, **kwargs: Any) -> bool:
        assert kwargs["build_chat_model"] is _reject_build
        calls.append(tid)
        return False

    monkeypatch.setattr(daemon.asyncio, "sleep", poll)
    monkeypatch.setattr(daemon, "generate_label_async", failed_generation)
    p = pool(gate=database_gate)
    try:
        with pytest.raises(asyncio.CancelledError):
            await daemon.dispatch_loop(
                p,
                labeler_db(database_gate=database_gate),
                event_bus,
                Liveness(120),
                daemon.labeler_config(),
                build_chat_model=_reject_build,
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


async def test_dispatch_loop_hands_injected_factory_to_real_generate_label(
    db_conn: psycopg.Connection,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    tid = create_agent(db_conn)
    _seed_chat(db_conn, tid)
    db_conn.commit()
    config = daemon.labeler_config()
    overrides = ModelOverrides.from_pins({})
    stop = RuntimeError("stop after factory call")
    built: list[str] = []

    def build(model: str, **kwargs: object) -> object:
        built.append(model)
        assert kwargs["thinking"] == {"type": "disabled"}
        assert kwargs["catalog"] is model_catalog
        assert kwargs["llm_override"] == config_authority.runtime.lm.llm_override
        assert kwargs["overrides"] is overrides
        raise stop

    p = pool(gate=database_gate)
    try:
        with pytest.raises(RuntimeError) as caught:
            await daemon.dispatch_loop(
                p,
                labeler_db(database_gate=database_gate),
                event_bus,
                Liveness(daemon.LIVENESS_TIMEOUT_S),
                config,
                build_chat_model=build,
                catalog=model_catalog,
                llm_override=config_authority.runtime.lm.llm_override,
                overrides=overrides,
            )
        assert caught.value is stop
    finally:
        p.close()

    assert built == [config.labeler_model]
