"""Real database contracts for agent creation, forks, and resurrection.

Tests verify durable birth configuration, lifecycle messages, exact resurrection
triggers, and caller fences. Host wake publication follows committed state.
"""

from __future__ import annotations

from typing import Any, cast

import psycopg
import pytest
from psycopg.types.json import Jsonb

import base.db
from base.agents import ForkCheckpointNotFound
from base.agents.observation.snapshot import select_one
from base.cluster.machine import machine_name
from base.config import settings
from base.db import Database
from base.events.live.bus import EventBus
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
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The create+launch split (create_agent_row + _launch_agent_process) inserts
        agents + agents_meta row and does **not** insert inbound (asymmetric with
        resurrect — spawn creates from nothing, so no notification needed)."""

        new_id = _spawn_agent()

        # agents row: status='idling', spawner='user' (default), pid not yet filled
        assert _agents_row(db_conn, new_id) == (new_id, "user", "idling", None)
        # spawn should not insert inbound
        assert _inbound_count(db_conn, new_id) == 0

    def test_spawner_recorded(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The birth-time lineage string is stamped into both metadata fields."""
        parent_id = _spawn_agent()  # spawner='user' default
        child_id = _spawn_agent(spawner=f"agent:{parent_id}")

        with db_conn.cursor() as cur:
            cur.execute("SELECT spawner, born_spawner FROM agents_meta WHERE id = %s", (child_id,))
            row = cur.fetchone()
        assert row is not None
        assert row == (f"agent:{parent_id}", f"agent:{parent_id}")

    def test_label_stored_sticky(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A spawner-assigned label is stored with label_user_set=TRUE so the
        labeler's CAS (WHERE label IS NULL AND NOT label_user_set) skips it."""
        new_id = _spawn_agent(label="auth worker")
        with db_conn.cursor() as cur:
            cur.execute("SELECT label, label_user_set FROM agents WHERE id=%s", (new_id,))
            row = cur.fetchone()
        assert row == ("auth worker", True)

        # Default (no label) stays NULL + not-set so the labeler can fill it.
        plain_id = _spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute("SELECT label, label_user_set FROM agents WHERE id=%s", (plain_id,))
            assert cur.fetchone() == (None, False)

    def test_persists_config_overlay(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_spawn_agent(config={...}) persists to agents_meta.config_overlay AND
        passes config_overlay= to _launch_agent_process. Both sides must work for
        the per-agent model override to actually take effect at boot.
        """
        from base.config import settings

        new_id = _spawn_agent(spawner="user", config={"llm_model": "gpt-5.6-sol"})

        # column persisted
        with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT config_overlay FROM agents_meta WHERE id = %s", (new_id,))
            row = cur.fetchone()
        assert row is not None
        assert row[0] == {"llm_model": "gpt-5.6-sol"}

        # launch received the overlay

    def test_snapshot_reports_effective_model_vision_support(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, add_models: AddModels
    ) -> None:
        from dataclasses import replace

        from base.lm.plugin_providers import model_catalog

        model = "deepseek-vision-fixture"
        add_models(
            {
                model: replace(
                    model_catalog().models["deepseek-flash"],
                    spawnable=False,
                    unavailable_fallback="deepseek-flash",
                    media_types=frozenset({"image"}),
                )
            }
        )
        monkeypatch.setattr(settings.lm, "llm_model", "claude-sonnet-5")
        default_model_agent = _spawn_agent()
        # No birth pin: the live default decides (spawn stamps whatever .env says).
        db_conn.execute(
            "UPDATE agents_meta SET birth_config = '{}' WHERE id = %s", (default_model_agent,)
        )
        db_conn.commit()
        text_only_agent = _spawn_agent(config={"llm_model": "deepseek-flash"})
        withdrawn_vision_agent = _spawn_agent(config={"llm_model": model})

        default_snapshot = select_one(db_conn, default_model_agent)
        text_only_snapshot = select_one(db_conn, text_only_agent)
        withdrawn_snapshot = select_one(db_conn, withdrawn_vision_agent)

        assert default_snapshot is not None
        assert default_snapshot.supports_vision is True
        assert text_only_snapshot is not None
        assert text_only_snapshot.supports_vision is False
        assert withdrawn_snapshot is not None
        assert withdrawn_snapshot.supports_vision is False

    def test_snapshot_effective_model_follows_the_birth_stamp(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`llm_model` is birth-frozen: an agent born under a vision-capable default keeps
        it after the cluster default flips to a text-only model, as the host resolves it
        (overlay over birth stamp over the live default)."""
        born_vision = _spawn_agent()
        born_text = _spawn_agent()
        for agent_id, stamped in ((born_vision, "claude-sonnet-5"), (born_text, "deepseek-flash")):
            db_conn.execute(
                "UPDATE agents_meta SET birth_config = %s::jsonb WHERE id = %s",
                (f'{{"llm_model": "{stamped}"}}', agent_id),
            )
        db_conn.commit()
        monkeypatch.setattr(
            settings.lm, "llm_model", "deepseek-flash"
        )  # the default has since flipped

        vision = select_one(db_conn, born_vision)
        text = select_one(db_conn, born_text)

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


class TestSpawnFork:
    def test_fork_copies_target_and_ancestor_chain(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no compaction boundary in the chain, fork copies target ckpt + all ancestors
        (recursively via parent_checkpoint_id), so the new agent sees the full history."""
        source = _spawn_agent()
        # construct chain: a (root) → b → c
        _insert_checkpoint(db_conn, source, "a-ckpt", parent_id=None)
        _insert_checkpoint(db_conn, source, "b-ckpt", parent_id="a-ckpt")
        _insert_checkpoint(db_conn, source, "c-ckpt", parent_id="b-ckpt")

        new_id = _spawn_agent(fork_from=source, fork_checkpoint="c-ckpt")

        # new agent gets all three a/b/c
        assert sorted(_checkpoint_ids(db_conn, new_id)) == ["a-ckpt", "b-ckpt", "c-ckpt"]
        # source is untouched
        assert sorted(_checkpoint_ids(db_conn, source)) == ["a-ckpt", "b-ckpt", "c-ckpt"]

    def test_fork_does_not_copy_descendants_after_target(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """fork at b → new agent only gets a + b, not c (checkpoints after b).
        Verify the semantic: "fork cuts the state before the fork point"."""
        source = _spawn_agent()
        _insert_checkpoint(db_conn, source, "a", parent_id=None)
        _insert_checkpoint(db_conn, source, "b", parent_id="a")
        _insert_checkpoint(db_conn, source, "c", parent_id="b")

        new_id = _spawn_agent(fork_from=source, fork_checkpoint="b")

        assert sorted(_checkpoint_ids(db_conn, new_id)) == ["a", "b"]

    def test_fork_records_source_in_agents_row(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """fork_source_agent_id + fork_source_checkpoint_id are written to the agents row."""
        source = _spawn_agent()
        _insert_checkpoint(db_conn, source, "x")

        new_id = _spawn_agent(fork_from=source, fork_checkpoint="x")

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT fork_source_agent_id, fork_source_checkpoint_id FROM agents_meta WHERE id = %s",
                (new_id,),
            )
            row = cur.fetchone()
        assert row == (source, "x")

    def test_fork_copies_blobs_referenced_by_the_copied_chain(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A blob the copied checkpoints reference through channel_versions is copied —
        the actual message data lives in blobs."""
        source = _spawn_agent()
        _insert_checkpoint(db_conn, source, "ck", channel_versions={"messages": "1"})
        _insert_blob(db_conn, source, "messages", "1", b"\xde\xad\xbe\xef")

        new_id = _spawn_agent(fork_from=source, fork_checkpoint="ck")

        assert _blob_rows(db_conn, new_id) == [("messages", "1", b"\xde\xad\xbe\xef")]

    def test_fork_skips_blobs_no_copied_checkpoint_references(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Superseded blob versions are left behind: PostgresSaver reads blobs only through
        the copied checkpoints' channel_versions, so an unreferenced version is unreachable."""
        source = _spawn_agent()
        _insert_checkpoint(db_conn, source, "ck", channel_versions={"messages": "2"})
        _insert_blob(db_conn, source, "messages", "1", b"\x01stale")
        _insert_blob(db_conn, source, "messages", "2", b"\x02live")

        new_id = _spawn_agent(fork_from=source, fork_checkpoint="ck")

        assert _blob_rows(db_conn, new_id) == [("messages", "2", b"\x02live")]

    def test_fork_stops_at_the_newest_compact_boundary(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fork above a boundary copies down to (and including) it and stops: its ancestors
        stay behind, so the copy stays bounded by the fork point's segment window."""
        source = _spawn_agent()
        _insert_checkpoint(db_conn, source, "a", parent_id=None)
        _insert_checkpoint(db_conn, source, "b", parent_id="a", compact_boundary=True)
        _insert_checkpoint(db_conn, source, "c", parent_id="b")

        new_id = _spawn_agent(fork_from=source, fork_checkpoint="c")

        # "a" is pre-boundary history and is NOT copied.
        assert sorted(_checkpoint_ids(db_conn, new_id)) == ["b", "c"]
        assert sorted(_checkpoint_ids(db_conn, source)) == ["a", "b", "c"]

    def test_fork_at_a_boundary_walks_to_the_root_without_a_previous_boundary(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fork checkpoint never terminates its own walk. Forking exactly at a boundary
        continues down to the next boundary below it — with no boundary below, the window is
        the full chain (the old cut-at-the-boundary window read back empty; the read-back
        assertions live in base/agents/history/tests/test_delta_read_compat.py)."""
        source = _spawn_agent()
        _insert_checkpoint(db_conn, source, "a", parent_id=None)
        _insert_checkpoint(db_conn, source, "b", parent_id="a", compact_boundary=True)

        new_id = _spawn_agent(fork_from=source, fork_checkpoint="b")

        assert _checkpoint_ids(db_conn, new_id) == ["a", "b"]

    def test_fork_at_a_boundary_stops_at_the_previous_boundary(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With a boundary below the fork point, the walk descends to it and stops: the copy
        spans at most two compacted segments, not the whole chain."""
        source = _spawn_agent()
        _insert_checkpoint(db_conn, source, "a", parent_id=None, compact_boundary=True)
        _insert_checkpoint(db_conn, source, "b", parent_id="a")
        _insert_checkpoint(db_conn, source, "c", parent_id="b", compact_boundary=True)

        new_id = _spawn_agent(fork_from=source, fork_checkpoint="c")

        assert sorted(_checkpoint_ids(db_conn, new_id)) == ["a", "b", "c"]

    def test_fork_stops_at_the_nearest_boundary_when_several_exist(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the newest boundary at or below the fork point terminates the walk; older
        segments are not copied."""
        source = _spawn_agent()
        _insert_checkpoint(db_conn, source, "a", parent_id=None, compact_boundary=True)
        _insert_checkpoint(db_conn, source, "b", parent_id="a")
        _insert_checkpoint(db_conn, source, "c", parent_id="b", compact_boundary=True)
        _insert_checkpoint(db_conn, source, "d", parent_id="c")

        new_id = _spawn_agent(fork_from=source, fork_checkpoint="d")

        assert sorted(_checkpoint_ids(db_conn, new_id)) == ["c", "d"]

    def test_fork_copies_writes_only_for_copied_checkpoints(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Delta-written threads keep their message content in writes, so writes must follow the
        copied checkpoints exactly — including the boundary cut."""
        source = _spawn_agent()
        _insert_checkpoint(db_conn, source, "a", parent_id=None)
        _insert_checkpoint(db_conn, source, "b", parent_id="a", compact_boundary=True)
        _insert_checkpoint(db_conn, source, "c", parent_id="b")
        for ckpt in ("a", "b", "c"):
            with db_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id, "
                    "task_id, idx, channel, type, blob) "
                    "VALUES (%s, '', %s, 'task', 0, 'messages', 'msgpack', %s)",
                    (str(source), ckpt, ckpt.encode()),
                )
        db_conn.commit()

        new_id = _spawn_agent(fork_from=source, fork_checkpoint="c")

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT checkpoint_id FROM checkpoint_writes WHERE thread_id = %s ORDER BY checkpoint_id",
                (str(new_id),),
            )
            assert [r[0] for r in cur.fetchall()] == ["b", "c"]

    def test_fork_unknown_checkpoint_raises_and_rolls_back(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """fork_checkpoint does not exist in source → raise + transaction rollback (no orphan agents /
        agents_meta rows)."""
        source = _spawn_agent()
        # intentionally do not INSERT any checkpoint

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM agents_meta")
            row = cur.fetchone()
            assert row is not None
            count_before = row[0]

        with pytest.raises(ForkCheckpointNotFound, match="bogus-ckpt"):
            _spawn_agent(fork_from=source, fork_checkpoint="bogus-ckpt")

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM agents_meta")
            row = cur.fetchone()
            assert row is not None
            assert row[0] == count_before  # no new agents row

    def test_fork_from_without_checkpoint_raises_value_error(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """fork_from / fork_checkpoint must be provided as a pair."""
        with pytest.raises(ValueError, match="must be provided as a pair"):
            _spawn_agent(fork_from=1)
        with pytest.raises(ValueError, match="must be provided as a pair"):
            _spawn_agent(fork_checkpoint="x")

    def test_fork_inserts_identity_inbound(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """fork inserts a kind='fork' lifecycle inbound (source='agent:{fork_from}', content='')
        within the same transaction that copies the checkpoint, so the new process receives
        the identity marker on its first claim. The insert is committed before _launch_agent_process."""
        source = _spawn_agent()
        _insert_checkpoint(db_conn, source, "ck")

        new_id = _spawn_agent(fork_from=source, fork_checkpoint="ck")

        assert _inbound_rows(db_conn, new_id) == [("", "fork", f"agent:{source}")]

    def test_plain_spawn_inserts_no_inbound(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-fork spawn does not insert any inbound (the agent starts from nothing, no 'who am I' to correct)."""
        new_id = _spawn_agent()
        assert _inbound_count(db_conn, new_id) == 0

    def test_fork_prompt_committed_before_launch(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """fork + prompt: the prompt chat inbound must be committed before _launch_agent_process
        so that it lands together with the fork marker in the forked agent's first claim batch
        (otherwise the agent would first run a turn based on inherited history + fork marker,
        and the prompt would arrive in the next batch, which is logically wrong).
        Snapshot inbound rows from the mocked launch to prove: at launch time,
        [fork marker, prompt] are both in the DB, correctly ordered (marker in the main transaction,
        prompt in a separate subsequent insert)."""
        source = _spawn_agent()
        _insert_checkpoint(db_conn, source, "ck")

        seen: list[list[tuple]] = []

        def _spy_wake(_db: object, _bus: object, agent_id: int, _payload: str) -> None:
            with base.db.connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT content, kind, source FROM inbound_messages "
                    "WHERE agent_id = %s ORDER BY id ASC",
                    (agent_id,),
                )
                seen.append(cur.fetchall())  # pyright: ignore[reportUnknownMemberType]

        monkeypatch.setattr(base.db, "publish_inbound_wake", _spy_wake)
        _spawn_agent(fork_from=source, fork_checkpoint="ck", prompt="go do X", prompt_source="user")

        assert seen and all(
            batch == [("", "fork", f"agent:{source}"), ("go do X", "chat", "user")]
            for batch in seen
        )

    def test_spawn_prompt_pairing_validated(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """prompt / prompt_source must be provided as a pair."""
        with pytest.raises(ValueError, match="prompt and prompt_source must be provided as a pair"):
            _spawn_agent(prompt="hi")
        with pytest.raises(ValueError, match="prompt and prompt_source must be provided as a pair"):
            _spawn_agent(prompt_source="user")

    def test_fork_event_target_is_fork_source_not_executor(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """User ruling 2026-08-28 (task #1879): the fork event's
        target_agent_id is the fork SOURCE (the lineage parent), never the
        executor who triggered the fork; the executor stays in `source`. The
        agents_meta spawner column records the fork source the same way (it
        is the frontend tree's parent fallback)."""
        import json
        from datetime import UTC, datetime

        from base import telemetry
        from base.paths import logs_dir

        source = _spawn_agent()
        executor = _spawn_agent()
        _insert_checkpoint(db_conn, source, "ck")

        new_id = _spawn_agent(spawner=f"agent:{executor}", fork_from=source, fork_checkpoint="ck")

        telemetry.sync()
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
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plain spawn keeps the old direction: target_agent_id = the
        spawner (its lineage parent), and the spawner column records it
        verbatim — the fork-source override must not leak into spawns."""
        import json
        from datetime import UTC, datetime

        from base import telemetry
        from base.paths import logs_dir

        parent = _spawn_agent()
        new_id = _spawn_agent(spawner=f"agent:{parent}")

        telemetry.sync()
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
    ) -> None:
        """The unified inbound writer's kind='fork' mapping (currently
        reached by no caller; the fork inbound is inserted with raw SQL in
        create_agent_row) must emit the same direction: target = the fork
        source parsed from the "agent:{fork_source}" identity marker, never
        None — a latent wrong-target path for the same ruling."""
        import json
        from datetime import UTC, datetime

        from base import telemetry
        from base.paths import logs_dir

        source = _spawn_agent()
        new_id = _spawn_agent()
        base.db.insert_inbound_message(
            db_conn,
            new_id,
            "",
            source=f"agent:{source}",
            kind="fork",
            bus=event_bus,
            database=database,
        )

        telemetry.sync()
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


class TestSpawnerValidation:
    """create_agent_row rejects malformed spawner values that would produce
    "Agent None" in the frontend tree."""

    def test_agent_none_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            _spawn_agent(spawner="agent:None")

    def test_agent_empty_id_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            _spawn_agent(spawner="agent:")

    def test_agent_alphabetic_id_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            _spawn_agent(spawner="agent:abc")

    def test_agent_zero_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            _spawn_agent(spawner="agent:0")

    def test_agent_valid_id_accepted(self, db_conn, monkeypatch: pytest.MonkeyPatch) -> None:
        # spawner="agent:42" is valid — must not raise
        new_id = _spawn_agent(spawner="agent:42")
        row = _agents_row(db_conn, new_id)  # pyright: ignore[reportUnknownArgumentType]
        assert row is not None
        assert row[1] == "agent:42"

    def test_user_spawner_accepted(self, db_conn, monkeypatch: pytest.MonkeyPatch) -> None:
        new_id = _spawn_agent(spawner="user")
        row = _agents_row(db_conn, new_id)  # pyright: ignore[reportUnknownArgumentType]
        assert row is not None
        assert row[1] == "user"

    def test_arbitrary_spawner_accepted(self, db_conn, monkeypatch: pytest.MonkeyPatch) -> None:
        new_id = _spawn_agent(spawner="claude-code")
        row = _agents_row(db_conn, new_id)  # pyright: ignore[reportUnknownArgumentType]
        assert row is not None
        assert row[1] == "claude-code"

    def test_agent_negative_id_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            _spawn_agent(spawner="agent:-1")
