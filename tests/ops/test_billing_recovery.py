"""Billing batch recovery (task #3919) — whitelist, gates, idempotency.

The explicit post-outage entry: enumerate the billing-class halt victims, check
the provider balance, resurrect each through the versioned billing-guarded
action (`resurrect-billing-v1`). Pinned here: the whitelist is exact (closed /
non-billing / user- and integrity-terminated / alive rows are never picked up),
the halted-but-alive survey is exact and report-only (never dispatched),
dry-run writes nothing, the balance gate fails closed, the per-agent CAS makes
a repeat an audited no-op, the run-level lock refuses a concurrent run, and the
row-locked guard refuses a closed row or a non-billing halt while the explicit
single resurrect keeps its reopen contract.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ops import agent_wake, billing_recovery
from ops.agent_wake import resurrect_agent
from ops.billing_recovery import enumerate_candidates, enumerate_halted_alive, run_billing_recovery
from ops.rpc_schemas import BillingBalanceReport
from shared.agents import ResurrectRefused
from shared.db import create_agent
from shared.recovery_breaker import PERMANENT_REJECT_REASON_BILLING
from shared.telemetry import Event


@pytest.fixture()
def pool() -> Iterator[ConnectionPool]:
    import shared.db

    p = shared.db.pool(max_size=4)
    yield p
    p.close()


def _agent(conn: psycopg.Connection) -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id,status,machine) VALUES (%s,'idling','claim-test') "
        "ON CONFLICT (id) DO UPDATE SET status='idling',machine='claim-test'",
        (agent_id,),
    )
    conn.commit()
    return agent_id


def _halt(
    conn: psycopg.Connection,
    agent_id: int,
    *,
    streak: int = 2,
    reason: str | None = PERMANENT_REJECT_REASON_BILLING,
    source: str | None = "reaper",
    status: str = "terminated",
    closed: bool = False,
) -> None:
    """Seed a row's death shape (the incident signature by default)."""
    conn.execute(
        "UPDATE agents_meta SET status=%s, termination_source=%s, "
        "permanent_reject_streak=%s, last_permanent_reject_reason=%s, "
        "closed_at=CASE WHEN %s THEN now() ELSE NULL END WHERE id=%s",
        (status, source, streak, reason, closed, agent_id),
    )
    conn.commit()


def _row(conn: psycopg.Connection, agent_id: int) -> tuple[Any, ...]:
    row = conn.execute(
        "SELECT status, closed_at, permanent_reject_streak, last_permanent_reject_reason "
        "FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone()
    assert row is not None
    return row


def _resurrect_inbounds(conn: psycopg.Connection, agent_id: int) -> int:
    row = conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='resurrect'",
        (agent_id,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _capture_resurrect_events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Observe the prepared audit fact while retaining the real event shape."""
    events: list[dict[str, Any]] = []
    prepare = agent_wake.prepare_event_log

    def _record_event(
        *,
        event_type: str,
        agent_id: int | None,
        source: str,
        target_agent_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Event:
        events.append(
            {
                "event_type": event_type,
                "agent_id": agent_id,
                "source": source,
                "target_agent_id": target_agent_id,
                "payload": payload or {},
            }
        )
        return prepare(
            event_type=event_type,
            agent_id=agent_id,
            source=source,
            target_agent_id=target_agent_id,
            payload=payload,
        )

    monkeypatch.setattr(agent_wake, "prepare_event_log", _record_event)
    return events


def _balance(ok: bool, detail: str = "probe detail") -> BillingBalanceReport:
    return BillingBalanceReport(ok=ok, detail=detail, threshold=1.0, total=9.5, currency="CNY")


def _capture_events(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict[str, Any]], list[tuple[tuple[Any, ...], dict[str, Any]]]]:
    audits: list[dict[str, Any]] = []
    telemetry: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _record_audit(**kw: Any) -> None:
        audits.append(kw)

    def _record_telemetry(*a: Any, **kw: Any) -> None:
        telemetry.append((a, kw))

    monkeypatch.setattr("shared.audit_events.insert_event_log", _record_audit)
    monkeypatch.setattr("shared.telemetry.emit", _record_telemetry)
    return audits, telemetry


def _dispatch_through_the_op(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    dispatched: list[int] = []

    async def _dispatch(**kwargs: Any) -> dict[str, Any]:
        agent_id = int(str(kwargs["payload"]["path"]).split("/")[3])
        dispatched.append(agent_id)
        response = await billing_recovery.resurrect_billing_agent_op(agent_id)
        return response.model_dump()

    monkeypatch.setattr(billing_recovery._cluster_rpc, "dispatch_to_machine", _dispatch)
    return dispatched


def test_whitelist_is_exact(db_conn: psycopg.Connection) -> None:
    keep = _agent(db_conn)
    _halt(db_conn, keep)
    excluded: list[int] = []
    kws: list[dict[str, Any]] = [
        {"closed": True},
        {"streak": 1},
        {"reason": "auth"},
        {"reason": None},
        {"source": "user"},
        {"source": "integrity"},
        {"status": "idling"},
    ]
    for kw in kws:
        aid = _agent(db_conn)
        _halt(db_conn, aid, **kw)
        excluded.append(aid)

    candidates = enumerate_candidates(db_conn)
    ids = [c.agent_id for c in candidates]
    assert keep in ids
    assert not any(aid in ids for aid in excluded)
    kept = next(c for c in candidates if c.agent_id == keep)
    assert kept.streak == 2
    assert kept.termination_source == "reaper"


def test_halted_alive_survey_is_exact(db_conn: psycopg.Connection) -> None:
    parked = _agent(db_conn)
    _halt(db_conn, parked, status="idling")
    running = _agent(db_conn)
    _halt(db_conn, running, status="running")
    terminated = _agent(db_conn)
    _halt(db_conn, terminated)
    excluded: list[int] = []
    kws: list[dict[str, Any]] = [
        {"status": "idling", "closed": True},
        {"status": "idling", "streak": 1},
        {"status": "idling", "reason": "auth"},
        {"status": "idling", "reason": None},
        {"status": "restarting"},
    ]
    for kw in kws:
        aid = _agent(db_conn)
        _halt(db_conn, aid, **kw)
        excluded.append(aid)

    survey = enumerate_halted_alive(db_conn)
    ids = [a.agent_id for a in survey]
    assert parked in ids and running in ids
    assert terminated not in ids
    assert not any(aid in ids for aid in excluded)
    assert next(a for a in survey if a.agent_id == parked).streak == 2


async def test_dry_run_previews_without_writing(
    db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _agent(db_conn)
    _halt(db_conn, aid)
    parked = _agent(db_conn)
    _halt(db_conn, parked, status="idling")
    audits, _ = _capture_events(monkeypatch)
    monkeypatch.setattr(billing_recovery, "fetch_provider_balance", lambda: _balance(True))

    resp = await run_billing_recovery(execute=False, pool=pool)

    assert resp.mode == "dry_run" and resp.outcome == "preview"
    assert aid in [o.agent_id for o in resp.agents]
    assert parked not in [o.agent_id for o in resp.agents]
    assert aid not in [a.agent_id for a in resp.halted_alive]
    assert parked in [a.agent_id for a in resp.halted_alive]
    assert _row(db_conn, aid)[0] == "terminated"
    assert _row(db_conn, parked)[0] == "idling"
    assert _resurrect_inbounds(db_conn, aid) == 0
    assert audits == []


async def test_execute_refused_when_balance_gate_fails(
    db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _agent(db_conn)
    _halt(db_conn, aid)
    monkeypatch.setattr(
        billing_recovery, "fetch_provider_balance", lambda: _balance(False, "still exhausted")
    )

    async def _must_not_dispatch(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError("dispatch reached while the balance gate refused")

    monkeypatch.setattr(billing_recovery._cluster_rpc, "dispatch_to_machine", _must_not_dispatch)

    resp = await run_billing_recovery(execute=True, pool=pool)

    assert resp.outcome == "refused"
    assert resp.refusal_reason is not None and "balance gate" in resp.refusal_reason
    assert _row(db_conn, aid)[0] == "terminated"


async def test_execute_resurrects_the_cohort(
    db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _agent(db_conn)
    _halt(db_conn, aid)
    parked = _agent(db_conn)
    _halt(db_conn, parked, status="idling")
    monkeypatch.setattr(billing_recovery, "fetch_provider_balance", lambda: _balance(True))
    dispatched = _dispatch_through_the_op(monkeypatch)
    audits, telemetry = _capture_events(monkeypatch)

    resp = await run_billing_recovery(execute=True, pool=pool)

    assert resp.outcome == "executed"
    assert next(o for o in resp.agents if o.agent_id == aid).status == "resurrected"
    assert aid in dispatched
    assert parked not in dispatched
    assert parked in [a.agent_id for a in resp.halted_alive]
    assert _row(db_conn, aid)[0] == "idling"
    assert _row(db_conn, parked)[0] == "idling"
    assert _resurrect_inbounds(db_conn, aid) == 1
    assert any(call["event_type"] == "billing_resurrect" for call in audits)
    assert (
        aid
        in next(c for c in audits if c["event_type"] == "billing_resurrect")["payload"][
            "resurrected"
        ]
    )
    assert any(a and a[1] == "billing_resurrect_run" for a, _ in telemetry)


async def test_second_execute_is_an_audited_noop(
    db_conn: psycopg.Connection, pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _agent(db_conn)
    _halt(db_conn, aid)
    monkeypatch.setattr(billing_recovery, "fetch_provider_balance", lambda: _balance(True))
    _dispatch_through_the_op(monkeypatch)
    _capture_events(monkeypatch)

    first = await run_billing_recovery(execute=True, pool=pool)
    second = await run_billing_recovery(execute=True, pool=pool)

    assert first.outcome == "executed" and second.outcome == "executed"
    assert [o for o in second.agents if o.agent_id == aid] == []
    assert _resurrect_inbounds(db_conn, aid) == 1


async def test_concurrent_run_is_refused_by_the_single_flight_lock(
    pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(billing_recovery, "fetch_provider_balance", lambda: _balance(True))

    def _refuse_lock(_pool: ConnectionPool) -> tuple[bool, None]:
        return False, None

    monkeypatch.setattr(billing_recovery, "_try_run_lock", _refuse_lock)

    async def _must_not_dispatch(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError("dispatch reached while another run held the lock")

    monkeypatch.setattr(billing_recovery._cluster_rpc, "dispatch_to_machine", _must_not_dispatch)

    resp = await run_billing_recovery(execute=True, pool=pool)

    assert resp.outcome == "refused"
    assert resp.refusal_reason is not None and "in progress" in resp.refusal_reason


def test_run_lock_admits_one_holder(pool: ConnectionPool) -> None:
    held, conn = billing_recovery._try_run_lock(pool)
    assert held and conn is not None
    try:
        held_again, conn_again = billing_recovery._try_run_lock(pool)
        assert not held_again and conn_again is None
    finally:
        billing_recovery._release_run_lock(pool, conn)
    held_after, conn_after = billing_recovery._try_run_lock(pool)
    assert held_after and conn_after is not None
    billing_recovery._release_run_lock(pool, conn_after)


def test_runner_refuses_closed_and_keeps_the_marker(db_conn: psycopg.Connection) -> None:
    aid = _agent(db_conn)
    _halt(db_conn, aid, closed=True)

    with pytest.raises(ResurrectRefused) as excinfo:
        resurrect_agent(aid, resurrected_by="user", billing_recovery=True)

    assert excinfo.value.reason == "closed"
    row = _row(db_conn, aid)
    assert row[0] == "terminated" and row[1] is not None


def test_runner_refuses_a_non_billing_halt(db_conn: psycopg.Connection) -> None:
    aid = _agent(db_conn)
    _halt(db_conn, aid, reason="auth")

    with pytest.raises(ResurrectRefused) as excinfo:
        resurrect_agent(aid, resurrected_by="user", billing_recovery=True)

    assert excinfo.value.reason == "not_billing_halted"
    assert _row(db_conn, aid)[0] == "terminated"


def test_runner_resurrects_a_billing_halt_and_marks_the_via_payload(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _agent(db_conn)
    _halt(db_conn, aid)
    events = _capture_resurrect_events(monkeypatch)

    resurrect_agent(aid, resurrected_by="user", billing_recovery=True)

    assert _row(db_conn, aid)[0] == "idling"
    assert _resurrect_inbounds(db_conn, aid) == 1
    resurrect_events = [e for e in events if e["event_type"] == "resurrect"]
    assert resurrect_events and resurrect_events[-1]["payload"] == {"via": "billing_recovery"}


def test_explicit_resurrect_still_reopens_a_closed_agent(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _agent(db_conn)
    _halt(db_conn, aid, closed=True)
    events = _capture_resurrect_events(monkeypatch)

    resurrect_agent(aid, resurrected_by="user")

    row = _row(db_conn, aid)
    assert row[0] == "idling" and row[1] is None
    resurrect_events = [e for e in events if e["event_type"] == "resurrect"]
    assert resurrect_events and resurrect_events[-1]["payload"] == {"reopened": True}


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


def _configure_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: Any = None,
    status_code: int = 200,
    raise_error: Exception | None = None,
    key: str | None = "secret",
    file_key: str | None = None,
    floor: float = 1.0,
    capture: dict[str, Any] | None = None,
) -> None:
    from pydantic import SecretStr

    from shared import http_dial, runtime_config
    from shared.config import settings

    monkeypatch.setattr(
        settings.lm, "deepseek_api_key", None if key is None else SecretStr(key), raising=False
    )
    monkeypatch.setattr(settings.daemon, "billing_recovery_min_balance", floor, raising=False)

    # Hermetic .env-file stub: the probe's gateway fallback reads the unit .env
    # through read_env_aliases; never let a real $AVA_HOME/.env leak into a test.
    def _stub_read_env_aliases() -> dict[str, str]:
        return {} if file_key is None else {"DEEPSEEK_API_KEY": file_key}

    monkeypatch.setattr(runtime_config, "read_env_aliases", _stub_read_env_aliases)

    def _get(url: str, **kwargs: Any) -> _FakeResponse:
        if capture is not None:
            capture.update(kwargs)
        if raise_error is not None:
            raise raise_error
        return _FakeResponse(status_code, payload)

    monkeypatch.setattr(http_dial, "get", _get)


def test_balance_probe_passes_above_the_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_probe(
        monkeypatch,
        payload={
            "is_available": True,
            "balance_infos": [
                {"currency": "USD", "total_balance": "-0.25"},
                {"currency": "CNY", "total_balance": "952.83"},
            ],
        },
    )

    report = billing_recovery.fetch_provider_balance()

    assert report.ok is True
    assert report.total == 952.83 and report.currency == "CNY"


def test_balance_probe_fails_closed_below_the_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_probe(
        monkeypatch,
        payload={
            "is_available": True,
            "balance_infos": [{"currency": "CNY", "total_balance": "0.5"}],
        },
        floor=1.0,
    )

    assert billing_recovery.fetch_provider_balance().ok is False


def test_balance_probe_fails_closed_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_probe(
        monkeypatch,
        payload={
            "is_available": False,
            "balance_infos": [{"currency": "CNY", "total_balance": "5.0"}],
        },
    )

    assert billing_recovery.fetch_provider_balance().ok is False


def test_balance_probe_fails_closed_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_probe(monkeypatch, raise_error=httpx.ConnectError("no route to provider"))

    report = billing_recovery.fetch_provider_balance()

    assert report.ok is False and "ConnectError" in report.detail


def test_balance_probe_fails_closed_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_probe(monkeypatch, key=None)

    report = billing_recovery.fetch_provider_balance()

    assert report.ok is False and "not configured" in report.detail


def test_balance_probe_falls_back_to_the_env_file_when_gateway_pops_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gateway profile: Settings carries no key; the unit .env file supplies it."""
    captured: dict[str, Any] = {}
    _configure_probe(
        monkeypatch,
        payload={
            "is_available": True,
            "balance_infos": [{"currency": "CNY", "total_balance": "660.27"}],
        },
        key=None,
        file_key="sk-from-env-file",
        capture=captured,
    )

    report = billing_recovery.fetch_provider_balance()

    assert report.ok is True and report.total == 660.27
    assert captured["headers"]["Authorization"] == "Bearer sk-from-env-file"


def test_balance_probe_fails_closed_when_neither_settings_nor_env_file_has_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_probe(monkeypatch, key=None, file_key=None)

    report = billing_recovery.fetch_provider_balance()

    assert report.ok is False and "not configured" in report.detail
