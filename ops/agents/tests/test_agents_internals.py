# meiva:ref ops/docs/env-audit.meiva.okf.md#spawn-and-snapshot-contract

from __future__ import annotations

from typing import cast

import psycopg
import pytest
from psycopg.types.json import Jsonb

from base.agents.birth.spawn import create_agent_row
from base.agents.impersonation.wake import publish_inbound_wake
from base.agents.observation.snapshot import select_one
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.packages.plugin_providers import build_model_catalog
from base.packages.tests.model_catalog import AddModels


def _agents_row(db: psycopg.Connection, agent_id: int) -> tuple[int, str, str, int | None] | None:
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, spawner, status, pid FROM agents_meta WHERE id = %s",
            (agent_id,),
        )
        return cur.fetchone()


def inbound_count(db: psycopg.Connection, agent_id: int) -> int:
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM inbound_messages WHERE agent_id = %s", (agent_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def spawn_agent(
    *,
    spawner: str = "user",
    fork_from: int | None = None,
    fork_checkpoint: str | None = None,
    config: dict[str, object] | None = None,
    label: str | None = None,
    prompt: str | None = None,
    prompt_source: str | None = None,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    local_machine: MachineIdentity,
) -> int:
    agent_id, _birth_config, _prompt_id, _attempt_id = create_agent_row(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        spawner=spawner,
        fork_from=fork_from,
        fork_checkpoint=fork_checkpoint,
        machine=local_machine.name(),
        config=config,
        label=label,
        prompt=prompt,
        prompt_source=prompt_source,
        catalog=model_catalog,
        authority=config_authority,
    )
    publish_inbound_wake(
        Database.from_settings(gate=database_gate), EventBus.from_settings(), agent_id, "0"
    )
    return agent_id


def inbound_rows(db: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str | None]]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT content, kind, source FROM inbound_messages "
            "WHERE agent_id = %s ORDER BY id ASC",
            (agent_id,),
        )
        return cur.fetchall()


class TestSpawnAgent:
    def test_inserts_thread_agent_and_starts_session(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        new_id = spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

        assert _agents_row(db_conn, new_id) == (new_id, "user", "idling", None)
        assert inbound_count(db_conn, new_id) == 0

    def test_spawner_recorded(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        parent_id = spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        child_id = spawn_agent(
            spawner=f"agent:{parent_id}",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

        with db_conn.cursor() as cur:
            cur.execute("SELECT spawner, born_spawner FROM agents_meta WHERE id = %s", (child_id,))
            row = cur.fetchone()
        assert row is not None
        assert row == (f"agent:{parent_id}", f"agent:{parent_id}")

    def test_label_stored_sticky(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        new_id = spawn_agent(
            label="auth worker",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        with db_conn.cursor() as cur:
            cur.execute("SELECT label, label_user_set FROM agents WHERE id=%s", (new_id,))
            row = cur.fetchone()
        assert row == ("auth worker", True)

        plain_id = spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        with db_conn.cursor() as cur:
            cur.execute("SELECT label, label_user_set FROM agents WHERE id=%s", (plain_id,))
            assert cur.fetchone() == (None, False)

    def test_persists_config_overlay(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        from base.config import settings

        new_id = spawn_agent(
            spawner="user",
            config={"llm_model": "gpt-5.6-sol"},
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

        with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT config_overlay FROM agents_meta WHERE id = %s", (new_id,))
            row = cur.fetchone()
        assert row is not None
        assert row[0] == {"llm_model": "gpt-5.6-sol"}

    def test_snapshot_reports_effective_model_vision_support(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        add_models: AddModels,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        from dataclasses import replace

        model = "glm-vision-fixture"
        model_catalog = add_models(
            model_catalog,
            {
                model: replace(
                    build_model_catalog().models["glm-5.3"],
                    spawnable=False,
                    unavailable_fallback="glm-5.3",
                    media_types=frozenset({"image"}),
                )
            },
        )
        monkeypatch.setattr(settings.lm, "llm_model", "claude-sonnet-5")
        default_model_agent = spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        db_conn.execute(
            "UPDATE agents_meta SET birth_config = '{}' WHERE id = %s", (default_model_agent,)
        )
        db_conn.commit()
        text_only_agent = spawn_agent(
            config={"llm_model": "glm-5.3"},
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        withdrawn_vision_agent = spawn_agent(
            config={"llm_model": model},
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

        default_snapshot = select_one(
            db_conn,
            default_model_agent,
            catalog=model_catalog,
            default_model_reader=lambda: settings.lm.llm_model,
        )
        text_only_snapshot = select_one(
            db_conn,
            text_only_agent,
            catalog=model_catalog,
            default_model_reader=lambda: settings.lm.llm_model,
        )
        withdrawn_snapshot = select_one(
            db_conn,
            withdrawn_vision_agent,
            catalog=model_catalog,
            default_model_reader=lambda: settings.lm.llm_model,
        )

        assert default_snapshot is not None
        assert default_snapshot.supports_vision is True
        assert text_only_snapshot is not None
        assert text_only_snapshot.supports_vision is False
        assert withdrawn_snapshot is not None
        assert withdrawn_snapshot.supports_vision is False

    def test_snapshot_effective_model_follows_the_birth_stamp(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        born_vision = spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        born_text = spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        for agent_id, stamped in ((born_vision, "claude-sonnet-5"), (born_text, "glm-5.3")):
            db_conn.execute(
                "UPDATE agents_meta SET birth_config = %s::jsonb WHERE id = %s",
                (f'{{"llm_model": "{stamped}"}}', agent_id),
            )
        db_conn.commit()
        monkeypatch.setattr(settings.lm, "llm_model", "glm-5.3")

        vision = select_one(
            db_conn,
            born_vision,
            catalog=model_catalog,
            default_model_reader=lambda: settings.lm.llm_model,
        )
        text = select_one(
            db_conn,
            born_text,
            catalog=model_catalog,
            default_model_reader=lambda: settings.lm.llm_model,
        )

        assert vision is not None and vision.supports_vision is True
        assert text is not None and text.supports_vision is False


def insert_checkpoint(
    db: psycopg.Connection,
    agent_id: int,
    ckpt_id: str,
    parent_id: str | None = None,
    *,
    channel_versions: dict[str, str] | None = None,
    compact_boundary: bool = False,
) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_id, parent_checkpoint_id, "
            "checkpoint, metadata) VALUES (%s, %s, %s, %s, %s)",
            (
                str(agent_id),
                ckpt_id,
                parent_id,
                Jsonb({"channel_versions": channel_versions or {}}),
                Jsonb({"compact_boundary": True} if compact_boundary else {}),
            ),
        )
    db.commit()


def checkpoint_ids(db: psycopg.Connection, agent_id: int) -> list[str]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE thread_id = %s ORDER BY checkpoint_id",
            (str(agent_id),),
        )
        return [r[0] for r in cur.fetchall()]


def insert_blob(
    db: psycopg.Connection, agent_id: int, channel: str, version: str, blob: bytes
) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO checkpoint_blobs (thread_id, checkpoint_ns, channel, version, type, blob) "
            "VALUES (%s, '', %s, %s, 'msgpack', %s)",
            (str(agent_id), channel, version, blob),
        )
    db.commit()


def blob_rows(db: psycopg.Connection, agent_id: int) -> list[tuple[str, str, bytes]]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT channel, version, blob FROM checkpoint_blobs WHERE thread_id = %s "
            "ORDER BY channel, version",
            (str(agent_id),),
        )
        return cast("list[tuple[str, str, bytes]]", cur.fetchall())


class TestSpawnerValidation:
    def test_agent_none_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            spawn_agent(
                spawner="agent:None",
                config_authority=config_authority,
                model_catalog=model_catalog,
                database_gate=database_gate,
                local_machine=machine_identity,
            )

    def test_agent_empty_id_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            spawn_agent(
                spawner="agent:",
                config_authority=config_authority,
                model_catalog=model_catalog,
                database_gate=database_gate,
                local_machine=machine_identity,
            )

    def test_agent_alphabetic_id_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            spawn_agent(
                spawner="agent:abc",
                config_authority=config_authority,
                model_catalog=model_catalog,
                database_gate=database_gate,
                local_machine=machine_identity,
            )

    def test_agent_zero_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            spawn_agent(
                spawner="agent:0",
                config_authority=config_authority,
                model_catalog=model_catalog,
                database_gate=database_gate,
                local_machine=machine_identity,
            )

    def test_agent_valid_id_accepted(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        new_id = spawn_agent(
            spawner="agent:42",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        row = _agents_row(db_conn, new_id)
        assert row is not None
        assert row[1] == "agent:42"

    def test_user_spawner_accepted(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        new_id = spawn_agent(
            spawner="user",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        row = _agents_row(db_conn, new_id)
        assert row is not None
        assert row[1] == "user"

    def test_arbitrary_spawner_accepted(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        new_id = spawn_agent(
            spawner="claude-code",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        row = _agents_row(db_conn, new_id)
        assert row is not None
        assert row[1] == "claude-code"

    def test_agent_negative_id_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            spawn_agent(
                spawner="agent:-1",
                config_authority=config_authority,
                model_catalog=model_catalog,
                database_gate=database_gate,
                local_machine=machine_identity,
            )
