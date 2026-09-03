"""Actual birth boundaries preserve pending work during publication maintenance."""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
import pytest

from agent import _starting
from shared.db import create_agent
from shared.runtime_admission import PublicationAdmissionDeferredError, legacy_boot_terminal_allowed
from tests.shared.test_managed_writer_publication import pending, seed_current
from tests.shared.test_managed_writer_publication import publication_db as publication_db


@pytest.mark.usefixtures("publication_db")
def test_process_pending_does_not_claim_or_terminate(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = seed_current(db_conn)
    pending(db_conn, current)
    target = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling','runner')", (target,)
    )
    row = db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,kind,content,source) VALUES(%s,'chat','retained','user') RETURNING id",
        (target,),
    ).fetchone()
    assert row is not None
    inbound = row[0]
    db_conn.commit()

    @contextmanager
    def transaction() -> Iterator[psycopg.Connection]:
        with db_conn.transaction():
            yield db_conn

    monkeypatch.setattr(_starting, "write_transaction", transaction)
    monkeypatch.setattr(_starting, "machine_name", lambda: "runner")
    with pytest.raises(PublicationAdmissionDeferredError):
        _starting.claim_agent_row(target)
    # Even schema/launch failure cleanup must not erase the deferred row.
    _starting._mark_preclaim_terminated(target)
    assert db_conn.execute(
        "SELECT status,pid,runtime_generation,runtime_owner FROM agents_meta WHERE id=%s", (target,)
    ).fetchone() == ("idling", None, None, None)
    assert db_conn.execute(
        "SELECT status,content FROM inbound_messages WHERE id=%s", (inbound,)
    ).fetchone() == ("pending", "retained")


@pytest.mark.usefixtures("publication_db")
def test_generic_terminal_fallback_never_becomes_enabled_after_rollout(
    db_conn: psycopg.Connection,
) -> None:
    with db_conn.transaction():
        assert legacy_boot_terminal_allowed(db_conn)
    seed_current(db_conn)
    with db_conn.transaction():
        assert not legacy_boot_terminal_allowed(db_conn)
    db_conn.execute(
        "UPDATE deployment_state SET managed_writer_evidence=%s::jsonb", ('{"version":99}',)
    )
    with pytest.raises(ValueError), db_conn.transaction():
        legacy_boot_terminal_allowed(db_conn)
