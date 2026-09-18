# pyright: reportUnknownArgumentType=warning
"""Update straggler reap — drain truncation, honest receipts, and settlement.

Task #4016: an update-family drain releases an un-landed cohort member as
`reaped` once its restart command has been issued for W seconds
(`AVA_UPDATE_STRAGGLER_REAP_SECONDS`): the row is CAS-marked 'restarting' (the
durable truncation signal read by `agent.db.has_pending_interrupt`), its
in-flight turn interrupted, and it is released with the honest `reaped`
outcome — never a flush/apply receipt. The mark is settled at the successor
boundary (`shared.straggler_reap`): the row returns to runnable and the
never-applied command is closed with an honest `lifecycle_result`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest

from ops import agent_pause, cluster_pause
from shared import maintenance, maintenance_cohort, pause_owner
from shared.db import create_agent, insert_inbound_message
from shared.machine import machine_name
from shared.maintenance_state import MaintenanceHold
from shared.straggler_reap import (
    REAP_LIFECYCLE_OUTCOME,
    REAP_LIFECYCLE_REASON,
    settle_stranded_reaps,
)

_MAINTENANCE_PAYLOAD: dict[str, object] = {
    "maintenance": {"holder": "ops:test:1", "acquired_at": "2026-09-19T00:00:00+00:00"}
}


@pytest.fixture(autouse=True)
def private_journal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A private pause journal — the drain never touches the live host."""
    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause.lock")


def _fake_host(monkeypatch: pytest.MonkeyPatch, owner: UUID, active: set[int]) -> None:
    """A live host bound to `owner`; its active set is what the drain waits on."""
    from ops.agent_pause_probe import HostIdentity

    monkeypatch.setattr(agent_pause, "host_running", lambda: True)
    monkeypatch.setattr(
        agent_pause, "host_identity", lambda: HostIdentity(owner, frozenset(active))
    )


def _running_agent(db_conn: psycopg.Connection) -> tuple[int, UUID, UUID]:
    """One native hosted cohort member, mid-turn, with a fresh lease."""
    agent = create_agent(db_conn)
    owner, generation = uuid4(), uuid4()
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_kind,runtime_owner,"
        "runtime_generation,lease_expires_at) VALUES(%s,'running',%s,'hosted',%s,%s,"
        "clock_timestamp()+interval '1 minute')",
        (agent, machine_name(), owner, generation),
    )
    db_conn.commit()
    return agent, owner, generation


def _reap_window(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    monkeypatch.setattr("shared.config.settings.gateway.update_straggler_reap_seconds", seconds)


def test_drain_reaps_a_straggler_past_its_window(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The member is released as `reaped` — marked, never flushed or applied."""
    agent, owner, generation = _running_agent(db_conn)
    _fake_host(monkeypatch, owner, {agent})
    _reap_window(monkeypatch, 0.001)
    monkeypatch.setattr("shared.config.settings.gateway.update_quiesce_timeout_seconds", 10.0)

    cluster_pause.pause_local_cluster()

    current = maintenance.snapshot()
    assert current is not None and current.maintenance is not None
    hold = current.maintenance
    assert hold.phase == "drained"
    assert hold.drained == ()
    assert hold.reaped == {agent: "update_straggler_reap"}
    # The mark is the truncation signal; ownership records stay historical.
    assert db_conn.execute(
        "SELECT status,runtime_owner,runtime_generation FROM agents_meta WHERE id=%s", (agent,)
    ).fetchone() == ("restarting", owner, generation)
    command = hold.commands[agent]
    assert db_conn.execute(
        "SELECT kind,status,applied_at FROM inbound_messages WHERE id=%s", (command,)
    ).fetchone() == ("restart", "pending", None)


def test_drain_with_a_zero_window_keeps_the_abort_behavior(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W=0 disables the reap: the drain times out and the hold is retained."""
    agent, owner, generation = _running_agent(db_conn)
    _fake_host(monkeypatch, owner, {agent})
    _reap_window(monkeypatch, 0)
    monkeypatch.setattr("shared.config.settings.gateway.update_quiesce_timeout_seconds", 0.01)

    with pytest.raises(TimeoutError, match="without force") as raised:
        cluster_pause.pause_local_cluster()

    assert "reaped this wave" not in str(raised.value)
    current = maintenance.snapshot()
    assert current is not None and current.maintenance is not None
    assert current.maintenance.reaped == {}
    assert db_conn.execute(
        "SELECT status,runtime_owner,runtime_generation FROM agents_meta WHERE id=%s", (agent,)
    ).fetchone() == ("running", owner, generation)


def test_drain_excludes_takeover_rows_from_the_reap(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An external takeover owns the decision: graceful path, no mark."""
    agent, owner, generation = _running_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agent_impersonations(id,agent_id,session_id,source,machine,status,"
        "ttl_seconds,expires_at) VALUES(%s,%s,1,'cli',%s,'requested',300,"
        "clock_timestamp()+interval '5 minutes')",
        (uuid4(), agent, machine_name()),
    )
    db_conn.commit()
    _fake_host(monkeypatch, owner, {agent})
    _reap_window(monkeypatch, 0.001)
    monkeypatch.setattr("shared.config.settings.gateway.update_quiesce_timeout_seconds", 0.01)

    with pytest.raises(TimeoutError, match="without force"):
        cluster_pause.pause_local_cluster()

    current = maintenance.snapshot()
    assert current is not None and current.maintenance is not None
    assert current.maintenance.reaped == {}
    assert db_conn.execute(
        "SELECT status,runtime_owner,runtime_generation FROM agents_meta WHERE id=%s", (agent,)
    ).fetchone() == ("running", owner, generation)


def test_verify_drained_accepts_the_reap_state_and_rejects_drift(
    db_conn: psycopg.Connection,
) -> None:
    agent, _owner, _generation = _running_agent(db_conn)
    command = insert_inbound_message(
        db_conn, agent, "", "system:maintenance", kind="restart", payload=_MAINTENANCE_PAYLOAD
    )
    db_conn.execute("UPDATE agents_meta SET status='restarting' WHERE id=%s", (agent,))
    db_conn.commit()
    hold = MaintenanceHold("draining", {agent: command}, reaped={agent: "update_straggler_reap"})

    maintenance_cohort.verify_drained(db_conn, hold)

    db_conn.execute("UPDATE agents_meta SET status='idling' WHERE id=%s", (agent,))
    db_conn.commit()
    with pytest.raises(RuntimeError, match="reap state"):
        maintenance_cohort.verify_drained(db_conn, hold)


def test_settle_stranded_reaps_restores_and_closes_honestly(
    db_conn: psycopg.Connection,
) -> None:
    """The successor boundary: row runnable again, command closed as reaped."""
    agent = create_agent(db_conn)
    owner, generation = uuid4(), uuid4()
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_kind,runtime_owner,"
        "runtime_generation,lease_expires_at) VALUES(%s,'restarting',%s,'hosted',%s,%s,"
        "clock_timestamp()+interval '1 minute')",
        (agent, machine_name(), owner, generation),
    )
    command = insert_inbound_message(
        db_conn, agent, "", "system:maintenance", kind="restart", payload=_MAINTENANCE_PAYLOAD
    )
    db_conn.execute("UPDATE agents_meta SET lifecycle_command_id=%s WHERE id=%s", (command, agent))
    db_conn.commit()

    with db_conn.transaction():
        settled = settle_stranded_reaps(db_conn, machine_name())

    assert settled == [agent]
    assert db_conn.execute(
        "SELECT status,runtime_kind,runtime_owner,runtime_generation,lease_expires_at,"
        "lifecycle_command_id FROM agents_meta WHERE id=%s",
        (agent,),
    ).fetchone() == ("idling", None, None, None, None, None)
    row = db_conn.execute(
        "SELECT status,applied_at,observed_at,payload->'lifecycle_result' "
        "FROM inbound_messages WHERE id=%s",
        (command,),
    ).fetchone()
    assert row is not None
    assert row[0] == "done" and row[1] is None and row[2] is None
    assert row[3] == {"outcome": REAP_LIFECYCLE_OUTCOME, "reason": REAP_LIFECYCLE_REASON}
    # Idempotent: a second pass finds nothing settled.
    with db_conn.transaction():
        assert settle_stranded_reaps(db_conn, machine_name()) == []


def test_settle_leaves_rows_that_are_not_reap_marks_alone(
    db_conn: psycopg.Connection,
) -> None:
    idle = create_agent(db_conn)
    stranded_without_command = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling',%s)",
        (idle, machine_name()),
    )
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_kind) "
        "VALUES(%s,'restarting',%s,'hosted')",
        (stranded_without_command, machine_name()),
    )
    db_conn.commit()

    with db_conn.transaction():
        settled = settle_stranded_reaps(db_conn, machine_name())

    assert settled == []
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (stranded_without_command,)
    ).fetchone() == ("restarting",)


def test_unpause_settles_stranded_reaps_and_wakes_them(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resume on a host that stayed up is the other settle boundary."""
    import shared.db as shared_db

    agent = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_kind,runtime_owner,"
        "runtime_generation) VALUES(%s,'restarting',%s,'hosted',%s,%s)",
        (agent, machine_name(), uuid4(), uuid4()),
    )
    insert_inbound_message(
        db_conn, agent, "", "system:maintenance", kind="restart", payload=_MAINTENANCE_PAYLOAD
    )
    db_conn.commit()
    woke: list[int] = []
    monkeypatch.setattr(shared_db, "publish_inbound_wake", lambda aid, _source: woke.append(aid))
    monkeypatch.setattr("shared.host_deploy_state.set_posture", lambda _posture: None)

    cluster_pause.unpause_local_cluster()

    assert woke == [agent]
    assert db_conn.execute("SELECT status FROM agents_meta WHERE id=%s", (agent,)).fetchone() == (
        "idling",
    )


async def test_settle_async_restores_the_same_shape(db_conn: psycopg.Connection, aops_pool) -> None:
    """The boot transport is the same settle on the async pool."""
    from shared.straggler_reap import settle_stranded_reaps_async

    agent = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_kind,runtime_owner,"
        "runtime_generation) VALUES(%s,'restarting',%s,'hosted',%s,%s)",
        (agent, machine_name(), uuid4(), uuid4()),
    )
    command = insert_inbound_message(
        db_conn, agent, "", "system:maintenance", kind="restart", payload=_MAINTENANCE_PAYLOAD
    )
    db_conn.commit()

    settled = await settle_stranded_reaps_async(aops_pool, machine_name())

    assert settled == [agent]
    assert db_conn.execute("SELECT status FROM agents_meta WHERE id=%s", (agent,)).fetchone() == (
        "idling",
    )
    payload = db_conn.execute(
        "SELECT payload->'lifecycle_result' FROM inbound_messages WHERE id=%s", (command,)
    ).fetchone()
    assert payload is not None and payload[0] == {
        "outcome": REAP_LIFECYCLE_OUTCOME,
        "reason": REAP_LIFECYCLE_REASON,
    }


def test_record_reaped_rejects_agents_outside_the_cohort() -> None:
    """The honest receipt is only for this hold's captured cohort."""
    when = datetime(2026, 9, 19, tzinfo=UTC)
    pause_owner.begin_maintenance("ops:test:1", when)
    pause_owner.change_maintenance(
        "ops:test:1", when, MaintenanceHold(), MaintenanceHold("draining", {7: 100})
    )
    with pytest.raises(RuntimeError, match="cohort"):
        maintenance.record_reaped(9, "update_straggler_reap")


def test_reaped_receipts_roundtrip_and_reject_invalid_shapes() -> None:
    hold = MaintenanceHold("draining", {7: 100, 8: 101}, reaped={7: "update_straggler_reap"})
    decoded = MaintenanceHold.decode(hold.encode())
    assert decoded.reaped == {7: "update_straggler_reap"}
    assert decoded.drained == ()

    with pytest.raises(ValueError, match="reaped receipt is outside"):
        MaintenanceHold.decode({**hold.encode(), "reaped": {"9": "update_straggler_reap"}})
    with pytest.raises(ValueError, match="overlap"):
        MaintenanceHold.decode(
            {
                **hold.encode(),
                "drained": [7],
                "reaped": {"7": "update_straggler_reap"},
            }
        )
