"""Real database contracts for agent forks and their committed wake receipts.

Tests verify durable birth configuration, lifecycle messages, exact resurrection
triggers, and caller fences. Host wake publication follows committed state.
"""

from __future__ import annotations

import psycopg
import pytest

import base.db
from base.agents import ForkCheckpointNotFound
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
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
from tests.fixtures.unit.identity import machine_identity as machine_identity


class TestSpawnFork:
    def test_fork_copies_target_and_ancestor_chain(
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
        """With no compaction boundary in the chain, fork copies target ckpt + all ancestors
        (recursively via parent_checkpoint_id), so the new agent sees the full history."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
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
            local_machine=machine_identity,
        )

        # new agent gets all three a/b/c
        assert sorted(_checkpoint_ids(db_conn, new_id)) == ["a-ckpt", "b-ckpt", "c-ckpt"]
        # source is untouched
        assert sorted(_checkpoint_ids(db_conn, source)) == ["a-ckpt", "b-ckpt", "c-ckpt"]

    def test_fork_does_not_copy_descendants_after_target(
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
        """fork at b → new agent only gets a + b, not c (checkpoints after b).
        Verify the semantic: "fork cuts the state before the fork point"."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
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
            local_machine=machine_identity,
        )

        assert sorted(_checkpoint_ids(db_conn, new_id)) == ["a", "b"]

    def test_fork_records_source_in_agents_row(
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
        """fork_source_agent_id + fork_source_checkpoint_id are written to the agents row."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        _insert_checkpoint(db_conn, source, "x")

        new_id = _spawn_agent(
            fork_from=source,
            fork_checkpoint="x",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
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
        redis_url: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        """A blob the copied checkpoints reference through channel_versions is copied —
        the actual message data lives in blobs."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        _insert_checkpoint(db_conn, source, "ck", channel_versions={"messages": "1"})
        _insert_blob(db_conn, source, "messages", "1", b"\xde\xad\xbe\xef")

        new_id = _spawn_agent(
            fork_from=source,
            fork_checkpoint="ck",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

        assert _blob_rows(db_conn, new_id) == [("messages", "1", b"\xde\xad\xbe\xef")]

    def test_fork_skips_blobs_no_copied_checkpoint_references(
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
        """Superseded blob versions are left behind: PostgresSaver reads blobs only through
        the copied checkpoints' channel_versions, so an unreferenced version is unreachable."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
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
            local_machine=machine_identity,
        )

        assert _blob_rows(db_conn, new_id) == [("messages", "2", b"\x02live")]

    def test_fork_stops_at_the_newest_compact_boundary(
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
        """A fork above a boundary copies down to (and including) it and stops: its ancestors
        stay behind, so the copy stays bounded by the fork point's segment window."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
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
            local_machine=machine_identity,
        )

        # "a" is pre-boundary history and is NOT copied.
        assert sorted(_checkpoint_ids(db_conn, new_id)) == ["b", "c"]
        assert sorted(_checkpoint_ids(db_conn, source)) == ["a", "b", "c"]

    def test_fork_at_a_boundary_walks_to_the_root_without_a_previous_boundary(
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
        """The fork checkpoint never terminates its own walk. Forking exactly at a boundary
        continues down to the next boundary below it — with no boundary below, the window is
        the full chain (the old cut-at-the-boundary window read back empty; the read-back
        assertions live in base/agents/history/tests/test_delta_read_compat.py)."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        _insert_checkpoint(db_conn, source, "a", parent_id=None)
        _insert_checkpoint(db_conn, source, "b", parent_id="a", compact_boundary=True)

        new_id = _spawn_agent(
            fork_from=source,
            fork_checkpoint="b",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

        assert _checkpoint_ids(db_conn, new_id) == ["a", "b"]

    def test_fork_at_a_boundary_stops_at_the_previous_boundary(
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
        """With a boundary below the fork point, the walk descends to it and stops: the copy
        spans at most two compacted segments, not the whole chain."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
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
            local_machine=machine_identity,
        )

        assert sorted(_checkpoint_ids(db_conn, new_id)) == ["a", "b", "c"]

    def test_fork_stops_at_the_nearest_boundary_when_several_exist(
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
        """Only the newest boundary at or below the fork point terminates the walk; older
        segments are not copied."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
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
            local_machine=machine_identity,
        )

        assert sorted(_checkpoint_ids(db_conn, new_id)) == ["c", "d"]

    def test_fork_copies_writes_only_for_copied_checkpoints(
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
        """Delta-written threads keep their message content in writes, so writes must follow the
        copied checkpoints exactly — including the boundary cut."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
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
            local_machine=machine_identity,
        )

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT checkpoint_id FROM checkpoint_writes WHERE thread_id = %s ORDER BY checkpoint_id",
                (str(new_id),),
            )
            assert [r[0] for r in cur.fetchall()] == ["b", "c"]

    def test_fork_unknown_checkpoint_raises_and_rolls_back(
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
        """fork_checkpoint does not exist in source → raise + transaction rollback (no orphan agents /
        agents_meta rows)."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
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
                local_machine=machine_identity,
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
        machine_identity: MachineIdentity,
    ) -> None:
        """fork_from / fork_checkpoint must be provided as a pair."""
        with pytest.raises(ValueError, match="must be provided as a pair"):
            _spawn_agent(
                fork_from=1,
                config_authority=config_authority,
                model_catalog=model_catalog,
                database_gate=database_gate,
                local_machine=machine_identity,
            )
        with pytest.raises(ValueError, match="must be provided as a pair"):
            _spawn_agent(
                fork_checkpoint="x",
                config_authority=config_authority,
                model_catalog=model_catalog,
                database_gate=database_gate,
                local_machine=machine_identity,
            )

    def test_fork_inserts_identity_inbound(
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
        """fork inserts a kind='fork' lifecycle inbound (source='agent:{fork_from}', content='')
        within the same transaction that copies the checkpoint, so the new process receives
        the identity marker on its first claim. The insert is committed before _launch_agent_process."""
        source = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        _insert_checkpoint(db_conn, source, "ck")

        new_id = _spawn_agent(
            fork_from=source,
            fork_checkpoint="ck",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

        assert _inbound_rows(db_conn, new_id) == [("", "fork", f"agent:{source}")]

    def test_plain_spawn_inserts_no_inbound(
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
        """Non-fork spawn does not insert any inbound (the agent starts from nothing, no 'who am I' to correct)."""
        new_id = _spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        assert _inbound_count(db_conn, new_id) == 0

    def test_fork_prompt_committed_before_launch(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        *,
        database_gate: ProcessDbGate,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        machine_identity: MachineIdentity,
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
            local_machine=machine_identity,
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
            local_machine=machine_identity,
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
        machine_identity: MachineIdentity,
    ) -> None:
        """prompt / prompt_source must be provided as a pair."""
        with pytest.raises(ValueError, match="prompt and prompt_source must be provided as a pair"):
            _spawn_agent(
                prompt="hi",
                config_authority=config_authority,
                model_catalog=model_catalog,
                database_gate=database_gate,
                local_machine=machine_identity,
            )
        with pytest.raises(ValueError, match="prompt and prompt_source must be provided as a pair"):
            _spawn_agent(
                prompt_source="user",
                config_authority=config_authority,
                model_catalog=model_catalog,
                database_gate=database_gate,
                local_machine=machine_identity,
            )
