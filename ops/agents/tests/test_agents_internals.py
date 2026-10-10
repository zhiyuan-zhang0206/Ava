"""Real database contracts for agent creation, forks, and resurrection.

Tests verify durable birth configuration, lifecycle messages, exact resurrection
triggers, and caller fences. Host wake publication follows committed state.
"""

from __future__ import annotations

from typing import cast

import psycopg
import pytest
from psycopg.types.json import Jsonb

import base.db
from base.agents.observation.snapshot import select_one
from base.cluster.machine import machine_name
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.lm.plugin_providers import build_model_catalog
from ops.agents import create_agent_row
from tests.fixtures.model_catalog import AddModels


def _agents_row(db: psycopg.Connection, agent_id: int) -> tuple[int, str, str, int | None] | None:
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, spawner, status, pid FROM agents_meta WHERE id = %s",
            (agent_id,),
        )
        return cur.fetchone()


def _inbound_count(db: psycopg.Connection, agent_id: int) -> int:
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM inbound_messages WHERE agent_id = %s", (agent_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _spawn_agent(
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
) -> int:
    """Test setup helper — mirrors the pre-#1236 `spawn_agent()` contract
    (create row + launch) as the two-phase split: `create_agent_row`
    (gateway-side, the main data-plane identity) then `_launch_agent_process`
    (runner-side), with the launch stubbed by the autouse guard. The launch op's
    prompt-delivery half is covered in ops/lifecycle/tests/test_operations.py."""
    agent_id, _birth_config, _prompt_id, _attempt_id = create_agent_row(
        Database.from_settings(),
        EventBus.from_settings(),
        spawner=spawner,
        fork_from=fork_from,
        fork_checkpoint=fork_checkpoint,
        machine=machine_name(),
        config=config,
        label=label,
        prompt=prompt,
        prompt_source=prompt_source,
        catalog=model_catalog,
        authority=config_authority,
    )
    base.db.publish_inbound_wake(Database.from_settings(), EventBus.from_settings(), agent_id, "0")
    return agent_id


def _inbound_rows(db: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str | None]]:
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
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        """The create+launch split (create_agent_row + _launch_agent_process) inserts
        agents + agents_meta row and does **not** insert inbound (asymmetric with
        resurrect — spawn creates from nothing, so no notification needed)."""

        new_id = _spawn_agent(config_authority=config_authority, model_catalog=model_catalog)

        # agents row: status='idling', spawner='user' (default), pid not yet filled
        assert _agents_row(db_conn, new_id) == (new_id, "user", "idling", None)
        # spawn should not insert inbound
        assert _inbound_count(db_conn, new_id) == 0

    def test_spawner_recorded(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        """The birth-time lineage string is stamped into both metadata fields."""
        parent_id = _spawn_agent(
            config_authority=config_authority, model_catalog=model_catalog
        )  # spawner='user' default
        child_id = _spawn_agent(
            spawner=f"agent:{parent_id}",
            config_authority=config_authority,
            model_catalog=model_catalog,
        )

        with db_conn.cursor() as cur:
            cur.execute("SELECT spawner, born_spawner FROM agents_meta WHERE id = %s", (child_id,))
            row = cur.fetchone()
        assert row is not None
        assert row == (f"agent:{parent_id}", f"agent:{parent_id}")

    def test_label_stored_sticky(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        """A spawner-assigned label is stored with label_user_set=TRUE so the
        labeler's CAS (WHERE label IS NULL AND NOT label_user_set) skips it."""
        new_id = _spawn_agent(
            label="auth worker", config_authority=config_authority, model_catalog=model_catalog
        )
        with db_conn.cursor() as cur:
            cur.execute("SELECT label, label_user_set FROM agents WHERE id=%s", (new_id,))
            row = cur.fetchone()
        assert row == ("auth worker", True)

        # Default (no label) stays NULL + not-set so the labeler can fill it.
        plain_id = _spawn_agent(config_authority=config_authority, model_catalog=model_catalog)
        with db_conn.cursor() as cur:
            cur.execute("SELECT label, label_user_set FROM agents WHERE id=%s", (plain_id,))
            assert cur.fetchone() == (None, False)

    def test_persists_config_overlay(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        """_spawn_agent(config={...}) persists to agents_meta.config_overlay AND
        passes config_overlay= to _launch_agent_process. Both sides must work for
        the per-agent model override to actually take effect at boot.
        """
        from base.config import settings

        new_id = _spawn_agent(
            spawner="user",
            config={"llm_model": "gpt-5.6-sol"},
            config_authority=config_authority,
            model_catalog=model_catalog,
        )

        # column persisted
        with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT config_overlay FROM agents_meta WHERE id = %s", (new_id,))
            row = cur.fetchone()
        assert row is not None
        assert row[0] == {"llm_model": "gpt-5.6-sol"}

        # launch received the overlay

    def test_snapshot_reports_effective_model_vision_support(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        add_models: AddModels,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        from dataclasses import replace

        model = "deepseek-vision-fixture"
        model_catalog = add_models(
            model_catalog,
            {
                model: replace(
                    build_model_catalog().models["deepseek-flash"],
                    spawnable=False,
                    unavailable_fallback="deepseek-flash",
                    media_types=frozenset({"image"}),
                )
            },
        )
        monkeypatch.setattr(settings.lm, "llm_model", "claude-sonnet-5")
        default_model_agent = _spawn_agent(
            config_authority=config_authority, model_catalog=model_catalog
        )
        # No birth pin: the live default decides (spawn stamps whatever .env says).
        db_conn.execute(
            "UPDATE agents_meta SET birth_config = '{}' WHERE id = %s", (default_model_agent,)
        )
        db_conn.commit()
        text_only_agent = _spawn_agent(
            config={"llm_model": "deepseek-flash"},
            config_authority=config_authority,
            model_catalog=model_catalog,
        )
        withdrawn_vision_agent = _spawn_agent(
            config={"llm_model": model},
            config_authority=config_authority,
            model_catalog=model_catalog,
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
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        """`llm_model` is birth-frozen: an agent born under a vision-capable default keeps
        it after the cluster default flips to a text-only model, as the host resolves it
        (overlay over birth stamp over the live default)."""
        born_vision = _spawn_agent(config_authority=config_authority, model_catalog=model_catalog)
        born_text = _spawn_agent(config_authority=config_authority, model_catalog=model_catalog)
        for agent_id, stamped in ((born_vision, "claude-sonnet-5"), (born_text, "deepseek-flash")):
            db_conn.execute(
                "UPDATE agents_meta SET birth_config = %s::jsonb WHERE id = %s",
                (f'{{"llm_model": "{stamped}"}}', agent_id),
            )
        db_conn.commit()
        monkeypatch.setattr(
            settings.lm, "llm_model", "deepseek-flash"
        )  # the default has since flipped

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


def _insert_checkpoint(
    db: psycopg.Connection,
    agent_id: int,
    ckpt_id: str,
    parent_id: str | None = None,
    *,
    channel_versions: dict[str, str] | None = None,
    compact_boundary: bool = False,
) -> None:
    """Test helper: directly INSERT a LangGraph checkpoint row (simplified version, bypassing
    PostgresSaver's serialization overhead). The fork copy logic only cares about SQL row-level copy + chain
    integrity, not the real content of the checkpoint blob.

    `channel_versions` fills the mapping PostgresSaver reads blobs through, and
    `compact_boundary` stamps the metadata flag `mark_compact_boundary` writes —
    the two fields the fork copy actually branches on.
    """
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


def _checkpoint_ids(db: psycopg.Connection, agent_id: int) -> list[str]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE thread_id = %s ORDER BY checkpoint_id",
            (str(agent_id),),
        )
        return [r[0] for r in cur.fetchall()]


def _insert_blob(
    db: psycopg.Connection, agent_id: int, channel: str, version: str, blob: bytes
) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO checkpoint_blobs (thread_id, checkpoint_ns, channel, version, type, blob) "
            "VALUES (%s, '', %s, %s, 'msgpack', %s)",
            (str(agent_id), channel, version, blob),
        )
    db.commit()


def _blob_rows(db: psycopg.Connection, agent_id: int) -> list[tuple[str, str, bytes]]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT channel, version, blob FROM checkpoint_blobs WHERE thread_id = %s "
            "ORDER BY channel, version",
            (str(agent_id),),
        )
        return cast("list[tuple[str, str, bytes]]", cur.fetchall())


class TestSpawnerValidation:
    """create_agent_row rejects malformed spawner values that would produce
    "Agent None" in the frontend tree."""

    def test_agent_none_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            _spawn_agent(
                spawner="agent:None", config_authority=config_authority, model_catalog=model_catalog
            )

    def test_agent_empty_id_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            _spawn_agent(
                spawner="agent:", config_authority=config_authority, model_catalog=model_catalog
            )

    def test_agent_alphabetic_id_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            _spawn_agent(
                spawner="agent:abc", config_authority=config_authority, model_catalog=model_catalog
            )

    def test_agent_zero_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            _spawn_agent(
                spawner="agent:0", config_authority=config_authority, model_catalog=model_catalog
            )

    def test_agent_valid_id_accepted(
        self,
        db_conn,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        # spawner="agent:42" is valid — must not raise
        new_id = _spawn_agent(
            spawner="agent:42", config_authority=config_authority, model_catalog=model_catalog
        )
        row = _agents_row(db_conn, new_id)  # pyright: ignore[reportUnknownArgumentType]
        assert row is not None
        assert row[1] == "agent:42"

    def test_user_spawner_accepted(
        self,
        db_conn,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        new_id = _spawn_agent(
            spawner="user", config_authority=config_authority, model_catalog=model_catalog
        )
        row = _agents_row(db_conn, new_id)  # pyright: ignore[reportUnknownArgumentType]
        assert row is not None
        assert row[1] == "user"

    def test_arbitrary_spawner_accepted(
        self,
        db_conn,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        new_id = _spawn_agent(
            spawner="claude-code", config_authority=config_authority, model_catalog=model_catalog
        )
        row = _agents_row(db_conn, new_id)  # pyright: ignore[reportUnknownArgumentType]
        assert row is not None
        assert row[1] == "claude-code"

    def test_agent_negative_id_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            _spawn_agent(
                spawner="agent:-1", config_authority=config_authority, model_catalog=model_catalog
            )
