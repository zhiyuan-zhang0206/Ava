"""Trusted provider recovery and propagation to the labeler's service owner."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import httpx2
import openai
import psycopg
import pytest
from langchain_core.exceptions import ModelConnectionError
from langchain_core.messages import AIMessage
from psycopg_pool import PoolTimeout

from base.config.service_read import ConfigAuthority
from base.daemon.health import Liveness
from base.db import Database, create_agent, pool
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import ModelOverrides
from base.lm import usage
from base.lm.catalog import ModelCatalog
from services.derived.labeler import daemon, labeler
from services.derived.labeler.tests.slices import labeler_config, labeler_db


class _LLM:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def ainvoke(self, _messages: object) -> AIMessage:
        if self.error is not None:
            raise self.error
        return AIMessage(content="Fix label generation")


def _install_llm(monkeypatch: pytest.MonkeyPatch, error: Exception | None = None) -> None:
    def build(_model: str, **_kwargs: object) -> _LLM:
        return _LLM(error)

    monkeypatch.setattr(labeler, "build_chat_model", build)


async def _generate(
    tid: int,
    bus: EventBus,
    authority: ConfigAuthority,
    catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> bool | None:
    return await labeler.generate_label_async(
        tid,
        "Fix the failing labeler",
        labeler_config(),
        labeler_db(database_gate=database_gate),
        bus,
        catalog=catalog,
        llm_override=authority.runtime.lm.llm_override,
        overrides=ModelOverrides.from_pins({}),
    )


def _status_error(status: int) -> openai.APIStatusError:
    response = httpx2.Response(status, request=httpx2.Request("POST", "https://audit.invalid"))
    return openai.APIStatusError(f"HTTP {status}", response=response, body=None)


def _label(db: psycopg.Connection, tid: int) -> str | None:
    with db.cursor() as cur:
        cur.execute("SELECT label FROM agents WHERE id = %s", (tid,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


@pytest.mark.parametrize(
    "error",
    [
        openai.APIConnectionError(request=httpx2.Request("POST", "https://audit.invalid")),
        _status_error(429),
        _status_error(503),
        ModelConnectionError("provider connection lost"),
        httpx.ReadTimeout("provider timed out"),
        httpx.ConnectError("provider connection lost"),
    ],
    ids=[
        "sdk-connection",
        "rate-limit",
        "temporary-server",
        "model-connection",
        "http-timeout",
        "http-connection",
    ],
)
async def test_trusted_invocation_failure_returns_retry_result_without_writing(
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    error: Exception,
    *,
    database_gate: ProcessDbGate,
) -> None:
    tid = create_agent(db_conn)
    _install_llm(monkeypatch, error)
    assert (
        await _generate(
            tid, event_bus, config_authority, model_catalog, database_gate=database_gate
        )
        is False
    )
    assert _label(db_conn, tid) is None


class _StatusLookalikeError(RuntimeError):
    status_code = 429


@pytest.mark.parametrize(
    "error",
    [
        TypeError("invalid invocation"),
        RuntimeError("network timed out"),
        _StatusLookalikeError("rate limited"),
        _status_error(401),
        _status_error(400),
    ],
    ids=["type-error", "network-prose", "status-lookalike", "auth", "invalid-request"],
)
async def test_unknown_and_permanent_invocation_failure_preserves_identity(
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    error: Exception,
    *,
    database_gate: ProcessDbGate,
) -> None:
    tid = create_agent(db_conn)
    _install_llm(monkeypatch, error)
    with pytest.raises(type(error)) as caught:
        await _generate(
            tid, event_bus, config_authority, model_catalog, database_gate=database_gate
        )
    assert caught.value is error
    assert _label(db_conn, tid) is None


@pytest.mark.parametrize("stage", ["builder", "usage", "parse", "cas"])
async def test_internal_failure_never_becomes_a_generation_retry(
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    stage: str,
    *,
    database_gate: ProcessDbGate,
) -> None:
    tid = create_agent(db_conn)
    error = psycopg.ProgrammingError("CAS schema drift") if stage == "cas" else TypeError(stage)
    _install_llm(monkeypatch)

    def fail(*_args: object, **_kwargs: object) -> Any:
        raise error

    targets = {
        "builder": (labeler, "build_chat_model"),
        "usage": (usage, "log_usage_from_message"),
        "parse": (labeler, "message_content"),
        "cas": (Database, "write_transaction"),
    }
    owner, name = targets[stage]
    monkeypatch.setattr(owner, name, fail)
    with pytest.raises(type(error)) as caught:
        await _generate(
            tid, event_bus, config_authority, model_catalog, database_gate=database_gate
        )
    assert caught.value is error
    assert _label(db_conn, tid) is None


@pytest.mark.parametrize("stage", ["builder", "usage", "parse"])
async def test_transport_shaped_failure_outside_invocation_is_not_recovered(
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    stage: str,
    *,
    database_gate: ProcessDbGate,
) -> None:
    tid = create_agent(db_conn)
    error = httpx.ReadTimeout(stage)
    _install_llm(monkeypatch)

    def fail(*_args: object, **_kwargs: object) -> Any:
        raise error

    targets = {
        "builder": (labeler, "build_chat_model"),
        "usage": (usage, "log_usage_from_message"),
        "parse": (labeler, "message_content"),
    }
    owner, name = targets[stage]
    monkeypatch.setattr(owner, name, fail)
    with pytest.raises(httpx.ReadTimeout) as caught:
        await _generate(
            tid, event_bus, config_authority, model_catalog, database_gate=database_gate
        )
    assert caught.value is error


async def test_notification_failure_cannot_uncommit_or_reselect_a_written_label(
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    tid = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, 'Fix labeler', 'chat', 'user')",
            (tid,),
        )
    error = TypeError("invalid label event")
    _install_llm(monkeypatch)

    async def fail(_bus: EventBus, _tid: int, _label: str) -> None:
        raise error

    monkeypatch.setattr(labeler, "publish_label_updated", fail)
    with pytest.raises(TypeError) as caught:
        await _generate(
            tid, event_bus, config_authority, model_catalog, database_gate=database_gate
        )
    assert caught.value is error
    assert _label(db_conn, tid) == "Fix label generation"
    with db_conn.cursor() as cur:
        assert tid not in dict(daemon._select_unlabeled(cur, []))


@pytest.mark.parametrize("error", [TypeError("missing catalog"), psycopg.ProgrammingError("CAS")])
async def test_dispatch_failure_stops_the_batch_without_rewriting_inbounds(
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    error: Exception,
    *,
    database_gate: ProcessDbGate,
) -> None:
    first = create_agent(db_conn)
    second = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, 'Fix labeler', 'chat', 'user') RETURNING id",
            (first,),
        )
        row = cur.fetchone()
        assert row is not None
        inbound_id = row[0]
        cur.execute("SELECT to_jsonb(im) FROM inbound_messages im WHERE id = %s", (inbound_id,))
        before = cur.fetchone()
    db_conn.commit()
    monkeypatch.setattr(daemon, "_POLL_INTERVAL_S", 0.0)
    polls = 0
    calls: list[int] = []

    def select(_cur: object, _cooling: list[int]) -> list[tuple[int, str]]:
        nonlocal polls
        polls += 1
        # A restored swallowing loop exits on its second poll rather than hanging.
        if polls > 1:
            raise asyncio.CancelledError
        return [(first, "prompt"), (second, "later prompt")]

    async def fail(tid: int, *_args: object, **_kwargs: object) -> bool:
        calls.append(tid)
        raise error

    monkeypatch.setattr(daemon, "_select_unlabeled", select)
    monkeypatch.setattr(daemon, "generate_label_async", fail)
    p = pool(gate=database_gate)
    try:
        with pytest.raises(type(error)) as caught:
            await daemon._dispatch_loop(
                p,
                labeler_db(database_gate=database_gate),
                event_bus,
                Liveness(120.0),
                labeler_config(),
                catalog=model_catalog,
                llm_override=config_authority.runtime.lm.llm_override,
                overrides=ModelOverrides.from_pins({}),
            )
        assert caught.value is error
    finally:
        p.close()
    assert calls == [first]
    assert polls == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT to_jsonb(im) FROM inbound_messages im WHERE id = %s", (inbound_id,))
        assert cur.fetchone() == before
    assert _label(db_conn, first) is None
    assert _label(db_conn, second) is None


@pytest.mark.parametrize(
    "error",
    [
        TypeError("invalid poll"),
        psycopg.ProgrammingError("SELECT"),
        RuntimeError("DB down"),
        psycopg.OperationalError("unproven database failure"),
        PoolTimeout("unproven pool failure"),
    ],
)
async def test_poll_failure_reaches_service_owner_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    error: Exception,
    *,
    database_gate: ProcessDbGate,
) -> None:
    monkeypatch.setattr(daemon, "_POLL_INTERVAL_S", 0.0)
    polls = 0

    def fail(_cur: object, _cooling: list[int]) -> list[tuple[int, str]]:
        nonlocal polls
        polls += 1
        if polls > 1:
            raise asyncio.CancelledError
        raise error

    monkeypatch.setattr(daemon, "_select_unlabeled", fail)
    p = pool(gate=database_gate)
    try:
        with pytest.raises(type(error)) as caught:
            await daemon._dispatch_loop(
                p,
                labeler_db(database_gate=database_gate),
                event_bus,
                Liveness(120.0),
                labeler_config(),
                catalog=model_catalog,
                llm_override=config_authority.runtime.lm.llm_override,
                overrides=ModelOverrides.from_pins({}),
            )
        assert caught.value is error
    finally:
        p.close()
    assert polls == 1


async def test_invocation_cancellation_propagates(
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    tid = create_agent(db_conn)
    cancellation = asyncio.CancelledError()

    class _CancelledLLM:
        async def ainvoke(self, _messages: object) -> None:
            raise cancellation

    def build(_model: str, **_kwargs: object) -> _CancelledLLM:
        return _CancelledLLM()

    monkeypatch.setattr(labeler, "build_chat_model", build)
    with pytest.raises(asyncio.CancelledError) as caught:
        await _generate(
            tid, event_bus, config_authority, model_catalog, database_gate=database_gate
        )
    assert caught.value is cancellation
    assert _label(db_conn, tid) is None
