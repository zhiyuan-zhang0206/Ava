"""Spawn and fork are recorded in `audit_events` by the birth transaction.

Who spawned whom cannot be derived from any later state, so the audit row commits
with the agent row or neither does.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base import telemetry
from base.agents.inbox.insert import insert_inbound_message
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.lm.plugin_providers import build_model_catalog
from ops.agents import birth_transaction
from ops.agents.spawn import create_agent_row
from ops.agents.tests.test_agents_internals import _insert_checkpoint, _spawn_agent
from tests.fixtures.unit.identity import machine_identity as machine_identity


def _audit(
    db: psycopg.Connection, agent_id: int, name: str
) -> list[tuple[str, int | None, dict[str, Any]]]:
    rows = db.execute(
        "SELECT source, target_agent_id, attributes FROM audit_events "
        "WHERE agent_id=%s AND event_name=%s ORDER BY id",
        (agent_id, name),
    ).fetchall()
    db.commit()
    return [(r[0], r[1], r[2]) for r in rows]


def _first_checkpoint(db: psycopg.Connection, agent_id: int) -> str:
    db.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, "
        "checkpoint, metadata) VALUES (%s, '', 'ckpt-1', 'x', '{}'::jsonb, '{}'::jsonb)",
        (str(agent_id),),
    )
    db.commit()
    return "ckpt-1"


def test_a_spawn_is_recorded_with_its_machine(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    machine = machine_identity.name()

    agent_id, _, _, _ = create_agent_row(
        database,
        event_bus,
        spawner="user",
        machine=machine,
        catalog=build_model_catalog(),
        authority=config_authority,
    )

    [(source, target, attributes)] = _audit(db_conn, agent_id, "spawn")
    assert (source, target) == ("user", None)
    assert attributes["machine"] == machine


def test_a_fork_is_recorded_against_its_source_agent(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    parent, _, _, _ = create_agent_row(
        database,
        event_bus,
        spawner="user",
        machine=machine_identity.name(),
        catalog=build_model_catalog(),
        authority=config_authority,
    )
    checkpoint = _first_checkpoint(db_conn, parent)

    child, _, _, _ = create_agent_row(
        database,
        event_bus,
        spawner="agent:4242",
        fork_from=parent,
        fork_checkpoint=checkpoint,
        machine=machine_identity.name(),
        catalog=build_model_catalog(),
        authority=config_authority,
    )

    [(source, target, attributes)] = _audit(db_conn, child, "fork")
    assert (source, target) == ("agent:4242", parent)
    assert attributes["fork_checkpoint"] == checkpoint
    assert _audit(db_conn, child, "spawn") == []


def test_an_agent_sourced_first_prompt_is_recorded_as_a_message(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    agent_id, _, inbound_id, _ = create_agent_row(
        database,
        event_bus,
        spawner="user",
        machine=machine_identity.name(),
        prompt="hello",
        prompt_source="agent:4242",
        catalog=build_model_catalog(),
        authority=config_authority,
    )

    [(source, target, attributes)] = _audit(db_conn, agent_id, "send_message")
    assert (source, target) == ("agent:4242", 4242)
    assert attributes["inbound_id"] == inbound_id


def test_a_spawn_whose_audit_fact_cannot_be_recorded_creates_no_agent(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    before = db_conn.execute("SELECT count(*) FROM agents").fetchone()
    db_conn.commit()

    def refuse(_conn: psycopg.Connection, _event: object) -> None:
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(birth_transaction, "record_audit", refuse)

    with pytest.raises(RuntimeError, match="audit write failed"):
        create_agent_row(
            database,
            event_bus,
            spawner="user",
            machine=machine_identity.name(),
            catalog=build_model_catalog(),
            authority=config_authority,
        )

    after = db_conn.execute("SELECT count(*) FROM agents").fetchone()
    db_conn.commit()
    assert after == before


@pytest.fixture
def fork_events(
    database_gate: ProcessDbGate, machine_identity: MachineIdentity, unit_home: Path
) -> Generator[telemetry.EventPipeline]:
    """Retain the actual writer only for fork event delivery contracts."""
    pipeline = telemetry.build_pipeline(
        database=lambda: Database.from_settings(gate=database_gate),
        machine=machine_identity,
        home=unit_home,
    )
    try:
        telemetry.init_telemetry(
            process="fork-tests", pipeline=pipeline, machine_reader=machine_identity.name
        )
        yield pipeline
    finally:
        assert pipeline.stop(timeout=2).status is telemetry.DrainStatus.COMPLETED


class TestSpawnFork:
    def test_fork_event_target_is_fork_source_not_executor(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        fork_events: telemetry.EventPipeline,
        machine_identity: MachineIdentity,
    ) -> None:
        """User ruling 2026-08-28 (task #1879): the fork event's
        target_agent_id is the fork SOURCE (the lineage parent), never the
        executor who triggered the fork; the executor stays in `source`. The
        agents_meta spawner column records the fork source the same way (it
        is the frontend tree's parent fallback)."""
        import json
        from datetime import UTC, datetime

        from base.paths import logs_dir

        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        executor = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        _insert_checkpoint(db_conn, source, "ck")

        new_id = _spawn_agent(
            spawner=f"agent:{executor}",
            fork_from=source,
            fork_checkpoint="ck",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

        assert fork_events.sync(timeout=2).status is telemetry.DrainStatus.COMPLETED
        day = datetime.now(UTC).strftime("%Y%m%d")
        path = logs_dir() / f"events-{day}.jsonl"
        fork_rows: list[dict[str, Any]] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("event_name") != "fork" or obj.get("agent_id") != new_id:
                    continue
                fork_rows.append(obj)
        assert len(fork_rows) == 1
        assert fork_rows[0]["source"] == f"agent:{executor}"
        assert fork_rows[0]["target_agent_id"] == source
        assert fork_rows[0]["attributes"]["fork_from"] == source

        with db_conn.cursor() as cur:
            cur.execute("SELECT spawner, born_spawner FROM agents_meta WHERE id = %s", (new_id,))
            row = cur.fetchone()
        assert row is not None
        assert row == (f"agent:{source}", f"agent:{source}")

    def test_spawn_event_target_is_spawner(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        fork_events: telemetry.EventPipeline,
        machine_identity: MachineIdentity,
    ) -> None:
        """A plain spawn keeps the old direction: target_agent_id = the
        spawner (its lineage parent), and the spawner column records it
        verbatim — the fork-source override must not leak into spawns."""
        import json
        from datetime import UTC, datetime

        from base.paths import logs_dir

        parent = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        new_id = _spawn_agent(
            spawner=f"agent:{parent}",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

        assert fork_events.sync(timeout=2).status is telemetry.DrainStatus.COMPLETED
        day = datetime.now(UTC).strftime("%Y%m%d")
        path = logs_dir() / f"events-{day}.jsonl"
        spawn_rows: list[dict[str, Any]] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("event_name") != "spawn" or obj.get("agent_id") != new_id:
                    continue
                spawn_rows.append(obj)
        assert len(spawn_rows) == 1
        assert spawn_rows[0]["source"] == f"agent:{parent}"
        assert spawn_rows[0]["target_agent_id"] == parent

        with db_conn.cursor() as cur:
            cur.execute("SELECT spawner, born_spawner FROM agents_meta WHERE id = %s", (new_id,))
            row = cur.fetchone()
        assert row is not None
        assert row == (f"agent:{parent}", f"agent:{parent}")

    def test_fork_inbound_via_unified_path_emits_fork_source_target(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        fork_events: telemetry.EventPipeline,
        machine_identity: MachineIdentity,
    ) -> None:
        """The unified inbound writer's kind='fork' mapping (currently
        reached by no caller; the fork inbound is inserted with raw SQL in
        create_agent_row) must emit the same direction: target = the fork
        source parsed from the "agent:{fork_source}" identity marker, never
        None — a latent wrong-target path for the same ruling."""
        import json
        from datetime import UTC, datetime

        from base.paths import logs_dir

        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        new_id = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        insert_inbound_message(
            db_conn,
            new_id,
            "",
            source=f"agent:{source}",
            kind="fork",
            bus=event_bus,
            database=database,
        )

        assert fork_events.sync(timeout=2).status is telemetry.DrainStatus.COMPLETED
        day = datetime.now(UTC).strftime("%Y%m%d")
        path = logs_dir() / f"events-{day}.jsonl"
        fork_rows: list[dict[str, Any]] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("event_name") != "fork" or obj.get("agent_id") != new_id:
                    continue
                fork_rows.append(obj)
        # The event carries the inbound id in its payload — the raw-SQL fork
        # path emits no such event, so exactly this one row is expected.
        assert len(fork_rows) == 1
        assert fork_rows[0]["target_agent_id"] == source
        assert fork_rows[0]["source"] == f"agent:{source}"
