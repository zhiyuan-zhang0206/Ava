"""Real database contracts for agent forks and their committed wake receipts.

Tests verify durable birth configuration, lifecycle messages, exact resurrection
triggers, and caller fences. Host wake publication follows committed state.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

import base.db
from base.agents import ForkCheckpointNotFound
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from ops.agents.tests.test_agents_internals import (
    _blob_rows,
    _checkpoint_ids,
    _inbound_count,
    _inbound_rows,
    _insert_blob,
    _insert_checkpoint,
    _spawn_agent,
)


class TestSpawnFork:
    def test_fork_copies_target_and_ancestor_chain(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """With no compaction boundary in the chain, fork copies target ckpt + all ancestors
        (recursively via parent_checkpoint_id), so the new agent sees the full history."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        # construct chain: a (root) → b → c
        _insert_checkpoint(db_conn, source, "a-ckpt", parent_id=None)
        _insert_checkpoint(db_conn, source, "b-ckpt", parent_id="a-ckpt")
        _insert_checkpoint(db_conn, source, "c-ckpt", parent_id="b-ckpt")

        new_id = _spawn_agent(
            fork_from=source,
            fork_checkpoint="c-ckpt",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )

        # new agent gets all three a/b/c
        assert sorted(_checkpoint_ids(db_conn, new_id)) == ["a-ckpt", "b-ckpt", "c-ckpt"]
        # source is untouched
        assert sorted(_checkpoint_ids(db_conn, source)) == ["a-ckpt", "b-ckpt", "c-ckpt"]

    def test_fork_does_not_copy_descendants_after_target(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """fork at b → new agent only gets a + b, not c (checkpoints after b).
        Verify the semantic: "fork cuts the state before the fork point"."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        _insert_checkpoint(db_conn, source, "a", parent_id=None)
        _insert_checkpoint(db_conn, source, "b", parent_id="a")
        _insert_checkpoint(db_conn, source, "c", parent_id="b")

        new_id = _spawn_agent(
            fork_from=source,
            fork_checkpoint="b",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )

        assert sorted(_checkpoint_ids(db_conn, new_id)) == ["a", "b"]

    def test_fork_records_source_in_agents_row(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """fork_source_agent_id + fork_source_checkpoint_id are written to the agents row."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        _insert_checkpoint(db_conn, source, "x")

        new_id = _spawn_agent(
            fork_from=source,
            fork_checkpoint="x",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT fork_source_agent_id, fork_source_checkpoint_id FROM agents_meta WHERE id = %s",
                (new_id,),
            )
            row = cur.fetchone()
        assert row == (source, "x")

    def test_fork_copies_blobs_referenced_by_the_copied_chain(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """A blob the copied checkpoints reference through channel_versions is copied —
        the actual message data lives in blobs."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        _insert_checkpoint(db_conn, source, "ck", channel_versions={"messages": "1"})
        _insert_blob(db_conn, source, "messages", "1", b"\xde\xad\xbe\xef")

        new_id = _spawn_agent(
            fork_from=source,
            fork_checkpoint="ck",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )

        assert _blob_rows(db_conn, new_id) == [("messages", "1", b"\xde\xad\xbe\xef")]

    def test_fork_skips_blobs_no_copied_checkpoint_references(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """Superseded blob versions are left behind: PostgresSaver reads blobs only through
        the copied checkpoints' channel_versions, so an unreferenced version is unreachable."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        _insert_checkpoint(db_conn, source, "ck", channel_versions={"messages": "2"})
        _insert_blob(db_conn, source, "messages", "1", b"\x01stale")
        _insert_blob(db_conn, source, "messages", "2", b"\x02live")

        new_id = _spawn_agent(
            fork_from=source,
            fork_checkpoint="ck",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )

        assert _blob_rows(db_conn, new_id) == [("messages", "2", b"\x02live")]

    def test_fork_stops_at_the_newest_compact_boundary(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """A fork above a boundary copies down to (and including) it and stops: its ancestors
        stay behind, so the copy stays bounded by the fork point's segment window."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        _insert_checkpoint(db_conn, source, "a", parent_id=None)
        _insert_checkpoint(db_conn, source, "b", parent_id="a", compact_boundary=True)
        _insert_checkpoint(db_conn, source, "c", parent_id="b")

        new_id = _spawn_agent(
            fork_from=source,
            fork_checkpoint="c",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )

        # "a" is pre-boundary history and is NOT copied.
        assert sorted(_checkpoint_ids(db_conn, new_id)) == ["b", "c"]
        assert sorted(_checkpoint_ids(db_conn, source)) == ["a", "b", "c"]

    def test_fork_at_a_boundary_walks_to_the_root_without_a_previous_boundary(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """The fork checkpoint never terminates its own walk. Forking exactly at a boundary
        continues down to the next boundary below it — with no boundary below, the window is
        the full chain (the old cut-at-the-boundary window read back empty; the read-back
        assertions live in base/agents/history/tests/test_delta_read_compat.py)."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        _insert_checkpoint(db_conn, source, "a", parent_id=None)
        _insert_checkpoint(db_conn, source, "b", parent_id="a", compact_boundary=True)

        new_id = _spawn_agent(
            fork_from=source,
            fork_checkpoint="b",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )

        assert _checkpoint_ids(db_conn, new_id) == ["a", "b"]

    def test_fork_at_a_boundary_stops_at_the_previous_boundary(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """With a boundary below the fork point, the walk descends to it and stops: the copy
        spans at most two compacted segments, not the whole chain."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        _insert_checkpoint(db_conn, source, "a", parent_id=None, compact_boundary=True)
        _insert_checkpoint(db_conn, source, "b", parent_id="a")
        _insert_checkpoint(db_conn, source, "c", parent_id="b", compact_boundary=True)

        new_id = _spawn_agent(
            fork_from=source,
            fork_checkpoint="c",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )

        assert sorted(_checkpoint_ids(db_conn, new_id)) == ["a", "b", "c"]

    def test_fork_stops_at_the_nearest_boundary_when_several_exist(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """Only the newest boundary at or below the fork point terminates the walk; older
        segments are not copied."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        _insert_checkpoint(db_conn, source, "a", parent_id=None, compact_boundary=True)
        _insert_checkpoint(db_conn, source, "b", parent_id="a")
        _insert_checkpoint(db_conn, source, "c", parent_id="b", compact_boundary=True)
        _insert_checkpoint(db_conn, source, "d", parent_id="c")

        new_id = _spawn_agent(
            fork_from=source,
            fork_checkpoint="d",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )

        assert sorted(_checkpoint_ids(db_conn, new_id)) == ["c", "d"]

    def test_fork_copies_writes_only_for_copied_checkpoints(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """Delta-written threads keep their message content in writes, so writes must follow the
        copied checkpoints exactly — including the boundary cut."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
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

        new_id = _spawn_agent(
            fork_from=source,
            fork_checkpoint="c",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT checkpoint_id FROM checkpoint_writes WHERE thread_id = %s ORDER BY checkpoint_id",
                (str(new_id),),
            )
            assert [r[0] for r in cur.fetchall()] == ["b", "c"]

    def test_fork_unknown_checkpoint_raises_and_rolls_back(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """fork_checkpoint does not exist in source → raise + transaction rollback (no orphan agents /
        agents_meta rows)."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        # intentionally do not INSERT any checkpoint

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM agents_meta")
            row = cur.fetchone()
            assert row is not None
            count_before = row[0]

        with pytest.raises(ForkCheckpointNotFound, match="bogus-ckpt"):
            _spawn_agent(
                fork_from=source,
                fork_checkpoint="bogus-ckpt",
                config_authority=config_authority,
                model_catalog=model_catalog,
                database_gate=database_gate,
            )

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM agents_meta")
            row = cur.fetchone()
            assert row is not None
            assert row[0] == count_before  # no new agents row

    def test_fork_from_without_checkpoint_raises_value_error(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """fork_from / fork_checkpoint must be provided as a pair."""
        with pytest.raises(ValueError, match="must be provided as a pair"):
            _spawn_agent(
                fork_from=1,
                config_authority=config_authority,
                model_catalog=model_catalog,
                database_gate=database_gate,
            )
        with pytest.raises(ValueError, match="must be provided as a pair"):
            _spawn_agent(
                fork_checkpoint="x",
                config_authority=config_authority,
                model_catalog=model_catalog,
                database_gate=database_gate,
            )

    def test_fork_inserts_identity_inbound(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """fork inserts a kind='fork' lifecycle inbound (source='agent:{fork_from}', content='')
        within the same transaction that copies the checkpoint, so the new process receives
        the identity marker on its first claim. The insert is committed before _launch_agent_process."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        _insert_checkpoint(db_conn, source, "ck")

        new_id = _spawn_agent(
            fork_from=source,
            fork_checkpoint="ck",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )

        assert _inbound_rows(db_conn, new_id) == [("", "fork", f"agent:{source}")]

    def test_plain_spawn_inserts_no_inbound(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """Non-fork spawn does not insert any inbound (the agent starts from nothing, no 'who am I' to correct)."""
        new_id = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        assert _inbound_count(db_conn, new_id) == 0

    def test_fork_prompt_committed_before_launch(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        database_gate: ProcessDbGate,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
    ) -> None:
        """fork + prompt: the prompt chat inbound must be committed before _launch_agent_process
        so that it lands together with the fork marker in the forked agent's first claim batch
        (otherwise the agent would first run a turn based on inherited history + fork marker,
        and the prompt would arrive in the next batch, which is logically wrong).
        Snapshot inbound rows from the mocked launch to prove: at launch time,
        [fork marker, prompt] are both in the DB, correctly ordered (marker in the main transaction,
        prompt in a separate subsequent insert)."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        _insert_checkpoint(db_conn, source, "ck")

        seen: list[list[tuple]] = []

        def _spy_wake(_db: object, _bus: object, agent_id: int, _payload: str) -> None:
            with base.db.connect(gate=database_gate) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT content, kind, source FROM inbound_messages "
                    "WHERE agent_id = %s ORDER BY id ASC",
                    (agent_id,),
                )
                seen.append(cur.fetchall())  # pyright: ignore[reportUnknownMemberType]

        monkeypatch.setattr(base.db, "publish_inbound_wake", _spy_wake)
        _spawn_agent(
            fork_from=source,
            fork_checkpoint="ck",
            prompt="go do X",
            prompt_source="user",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )

        assert seen and all(
            batch == [("", "fork", f"agent:{source}"), ("go do X", "chat", "user")]
            for batch in seen
        )

    def test_spawn_prompt_pairing_validated(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """prompt / prompt_source must be provided as a pair."""
        with pytest.raises(ValueError, match="prompt and prompt_source must be provided as a pair"):
            _spawn_agent(
                prompt="hi",
                config_authority=config_authority,
                model_catalog=model_catalog,
                database_gate=database_gate,
            )
        with pytest.raises(ValueError, match="prompt and prompt_source must be provided as a pair"):
            _spawn_agent(
                prompt_source="user",
                config_authority=config_authority,
                model_catalog=model_catalog,
                database_gate=database_gate,
            )

    def test_fork_event_target_is_fork_source_not_executor(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
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

        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        executor = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        _insert_checkpoint(db_conn, source, "ck")

        new_id = _spawn_agent(
            spawner=f"agent:{executor}",
            fork_from=source,
            fork_checkpoint="ck",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
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
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
    ) -> None:
        """A plain spawn keeps the old direction: target_agent_id = the
        spawner (its lineage parent), and the spawner column records it
        verbatim — the fork-source override must not leak into spawns."""
        import json
        from datetime import UTC, datetime

        from base import telemetry
        from base.paths import logs_dir

        parent = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        new_id = _spawn_agent(
            spawner=f"agent:{parent}",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )

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
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
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

        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
        new_id = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
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
