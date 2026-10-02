"""`ava.self.set_label` records its audit fact in the transaction that sets the label.

The label is an agent-facing tool call, so a failed audit write rolls the label back and
fails the call cleanly. The plugin is loaded through the real PluginContext path, as in
test_ava_fleet_plugin.py.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

import psycopg
import pytest

import ava
import ava.agent_identity
from agent.state import clear_plugin_registrations
from base.packages.plugins.context import PluginContext


@pytest.fixture
def _load_activity_plugin() -> Iterator[None]:
    clear_plugin_registrations()
    ava.clear_registered_namespaces()
    for name in list(sys.modules):
        if name.startswith("ava_builtins.plugins.ava_fleet"):
            del sys.modules[name]
    with PluginContext("ava_fleet"):
        from ava_builtins.plugins.ava_fleet import (
            plugin as plugin,  # registers self.set_label
        )
    yield
    clear_plugin_registrations()
    ava.clear_registered_namespaces()


@pytest.fixture
def agent_id(db_conn: psycopg.Connection) -> Iterator[int]:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        aid = int(row[0])
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running')", (aid,)
        )
    db_conn.commit()
    original = ava.agent_identity._agent_id
    ava.agent_identity._agent_id = aid
    try:
        yield aid
    finally:
        ava.agent_identity._agent_id = original


def _label_audit(db_conn: psycopg.Connection, agent_id: int) -> list[object]:
    rows = db_conn.execute(
        "SELECT attributes FROM audit_events WHERE agent_id=%s AND event_name='label_change' "
        "ORDER BY id",
        (agent_id,),
    ).fetchall()
    db_conn.commit()
    return [row[0] for row in rows]


def test_set_label_records_its_audit_fact_with_the_label(
    _load_activity_plugin: None, db_conn: psycopg.Connection, agent_id: int
) -> None:
    ava.self.set_label("auth-refactor lead")  # type: ignore[attr-defined]

    assert _label_audit(db_conn, agent_id) == [{"new_label": "auth-refactor lead"}]


def test_set_label_whose_audit_fact_cannot_be_recorded_does_not_set_it(
    _load_activity_plugin: None,
    db_conn: psycopg.Connection,
    agent_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(_conn: object, _event: object) -> None:
        raise RuntimeError("audit write failed")

    monkeypatch.setattr("base.telemetry.audit_events.record_audit", refuse)

    with pytest.raises(RuntimeError, match="audit write failed"):
        ava.self.set_label("never set")  # type: ignore[attr-defined]

    row = db_conn.execute("SELECT label FROM agents WHERE id=%s", (agent_id,)).fetchone()
    db_conn.commit()
    assert row == (None,)
