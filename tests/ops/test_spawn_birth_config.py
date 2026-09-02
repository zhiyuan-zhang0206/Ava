"""Spawn stamping — `ops/agent_spawn.spawn_agent` freezes the frozen set on the row.

The agents_meta INSERT is THE spawn boundary (every spawn in the system funnels
through it), so these tests go through the create+launch split (`create_agent_row`
+ the launch) rather than through `resolve_birth_config` directly: what matters
is that the stamp lands on the row and rides out to the launched child, not just
that the resolver can compute it.

The autouse `_guard_agent_launch` fixture replaces the real launch with a recorder,
so `launched_agents` is what the child would have been handed.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg
import pytest

from ops import agent_launch
from ops.agent_spawn import create_agent_row
from shared.birth_config import set_cluster_default_model
from shared.config import frozen_field_names
from tests.conftest import LaunchCall


def _spawn_agent(
    *,
    spawner: str = "user",
    config: dict[str, object] | None = None,
    **kw: Any,
) -> int:
    """Test setup helper — the #1236 split collapsed for setup: create_agent_row
    (gateway-side, the main data-plane identity) + `_launch_agent_process`
    (runner-side; the autouse guard spy records the launch so tests can assert
    the stamp rides out to the child)."""
    from shared.machine import machine_name

    agent_id, birth_config = create_agent_row(
        spawner=spawner, machine=machine_name(), config=config, **kw
    )
    agent_launch._launch_agent_process(
        agent_id, config_overlay=config, birth_config=birth_config, confirm=False
    )
    return agent_id


@pytest.fixture(autouse=True)
def _unset(cluster_defaults_unset: None) -> None:
    """Every test here starts from "no cluster choice" (see the shared fixture)."""


def _birth_config(conn: psycopg.Connection, agent_id: int) -> dict[str, object] | None:
    with conn.cursor() as cur:
        cur.execute("SELECT birth_config FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _first_checkpoint(conn: psycopg.Connection, agent_id: int) -> str:
    """Give `agent_id` one checkpoint so it can be forked from."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, "
            "checkpoint, metadata) VALUES (%s, '', 'ckpt-1', 'x', '{}'::jsonb, '{}'::jsonb)",
            (str(agent_id),),
        )
    conn.commit()
    return "ckpt-1"


class TestSpawnStamping:
    def test_absent_from_overlay_frozen_fields_are_stamped(
        self, db_conn: psycopg.Connection
    ) -> None:
        agent_id = _spawn_agent(spawner="test")
        assert set(_birth_config(db_conn, agent_id) or {}) == frozen_field_names()

    def test_overlay_present_frozen_field_is_not_stamped(self, db_conn: psycopg.Connection) -> None:
        agent_id = _spawn_agent(spawner="test", config={"llm_model": "claude-sonnet-5"})
        stamped = _birth_config(db_conn, agent_id) or {}
        assert "llm_model" not in stamped
        assert set(stamped) == frozen_field_names() - {"llm_model"}

    def test_the_cluster_default_model_is_what_gets_frozen(
        self, db_conn: psycopg.Connection
    ) -> None:
        with db_conn.cursor() as cur:
            set_cluster_default_model(cur, "claude-sonnet-5", updated_by="test")
        db_conn.commit()
        agent_id = _spawn_agent(spawner="test")
        assert (_birth_config(db_conn, agent_id) or {})["llm_model"] == "claude-sonnet-5"

    def test_a_later_default_flip_does_not_move_an_existing_agent(
        self, db_conn: psycopg.Connection
    ) -> None:
        """The whole point of the feature, end to end."""
        agent_id = _spawn_agent(spawner="test")
        born_with = (_birth_config(db_conn, agent_id) or {})["llm_model"]
        with db_conn.cursor() as cur:
            set_cluster_default_model(cur, "claude-sonnet-5", updated_by="test")
        db_conn.commit()
        assert (_birth_config(db_conn, agent_id) or {})["llm_model"] == born_with
        assert born_with != "claude-sonnet-5"

    def test_the_stamp_rides_out_to_the_launched_child(
        self, db_conn: psycopg.Connection, launched_agents: list[LaunchCall]
    ) -> None:
        agent_id = _spawn_agent(spawner="test")
        call = next(c for c in launched_agents if c.agent_id == agent_id)
        assert call.birth_config == _birth_config(db_conn, agent_id)


class TestReplayOnWake:
    """ "Replayed on every restart / respawn / resurrect" — the wake paths read the
    stamp off the row and hand it to the same launch channel spawn used."""

    def test_resurrect_replays_the_stamp(
        self, db_conn: psycopg.Connection, launched_agents: list[LaunchCall]
    ) -> None:
        from ops.agent_wake import resurrect_agent

        agent_id = _spawn_agent(spawner="test")
        stamp = _birth_config(db_conn, agent_id)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status = 'terminated', termination_source = 'exit' "
                "WHERE id = %s",
                (agent_id,),
            )
        db_conn.commit()
        resurrect_agent(agent_id, resurrected_by="user")
        assert launched_agents[-1].birth_config == stamp


class TestSelfRespawnFallback:
    """`agent/db.py:schedule_self_respawn` is the ONE launch path outside
    `ops/agent_launch.py` — an atexit fallback that fires exactly when the
    restarter is paused, i.e. mid-rollout. It must carry the same two maps, or a
    rollout would quietly drop agents back onto cluster defaults."""

    def test_replacement_env_carries_both_maps(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Neither map rides argv (issue #974: either JSON blob may carry a
        provider api_key, and argv is world-readable via `ps`) — both land in
        the replacement's env dict instead."""
        import atexit
        import subprocess
        import time

        from agent.db import schedule_self_respawn
        from shared.env_registry import AGENT_BIRTH_CONFIG_ENV, AGENT_CONFIG_OVERLAY_ENV

        agent_id = _spawn_agent(spawner="test", config={"llm_model": "claude-sonnet-5"})
        stamp = _birth_config(db_conn, agent_id)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'restarting' WHERE id = %s", (agent_id,))
        db_conn.commit()

        registered: list[object] = []
        monkeypatch.setattr(atexit, "register", registered.append)
        schedule_self_respawn(agent_id)
        monkeypatch.setattr(time, "sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]
        launched: list[tuple[list[str], dict[str, str]]] = []
        monkeypatch.setattr(
            subprocess,
            "Popen",
            lambda argv, *, env, **_kw: launched.append((list(argv), dict(env))),  # pyright: ignore[reportUnknownArgumentType]
        )

        registered[0]()  # type: ignore[operator] — the atexit callable

        assert launched, "self-respawn should have launched a replacement"
        argv, env = launched[0]
        assert "--config-overlay" not in argv
        assert "--birth-config" not in argv
        assert json.loads(env[AGENT_CONFIG_OVERLAY_ENV]) == {"llm_model": "claude-sonnet-5"}
        assert json.loads(env[AGENT_BIRTH_CONFIG_ENV]) == stamp

    def test_restarter_never_claims_and_fallback_launches_when_the_row_carries_nothing(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import atexit
        import subprocess
        import time

        from agent.db import schedule_self_respawn
        from shared.env_registry import AGENT_BIRTH_CONFIG_ENV, AGENT_CONFIG_OVERLAY_ENV

        agent_id = _spawn_agent(spawner="test")
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status = 'restarting', config_overlay = NULL, "
                "birth_config = NULL WHERE id = %s",
                (agent_id,),
            )
        db_conn.commit()

        registered: list[object] = []
        monkeypatch.setattr(atexit, "register", registered.append)
        schedule_self_respawn(agent_id)
        monkeypatch.setattr(time, "sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]
        launched: list[tuple[list[str], dict[str, str]]] = []
        monkeypatch.setattr(
            subprocess,
            "Popen",
            lambda argv, *, env, **_kw: launched.append((list(argv), dict(env))),  # pyright: ignore[reportUnknownArgumentType]
        )

        registered[0]()  # type: ignore[operator] — the atexit callable

        assert launched
        argv, env = launched[0]
        assert "--config-overlay" not in argv
        assert "--birth-config" not in argv
        assert AGENT_CONFIG_OVERLAY_ENV not in env
        assert AGENT_BIRTH_CONFIG_ENV not in env
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
            row = cur.fetchone()
        assert row is not None and row[0] == "idling"


class TestForkInheritance:
    def test_a_fork_inherits_the_parents_stamp(self, db_conn: psycopg.Connection) -> None:
        parent = _spawn_agent(spawner="test")
        ckpt = _first_checkpoint(db_conn, parent)
        with db_conn.cursor() as cur:
            set_cluster_default_model(cur, "claude-sonnet-5", updated_by="test")
        db_conn.commit()
        child = _spawn_agent(spawner="test", fork_from=parent, fork_checkpoint=ckpt)
        assert _birth_config(db_conn, child) == _birth_config(db_conn, parent)

    def test_a_fork_of_an_unstamped_agent_is_stamped_fresh(
        self, db_conn: psycopg.Connection
    ) -> None:
        """A pre-column agent (or one the migration backfill skipped) has NULL. Its
        fork must still come out fully stamped rather than inherit the hole."""
        parent = _spawn_agent(spawner="test")
        ckpt = _first_checkpoint(db_conn, parent)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET birth_config = NULL WHERE id = %s", (parent,))
        db_conn.commit()
        child = _spawn_agent(spawner="test", fork_from=parent, fork_checkpoint=ckpt)
        assert set(_birth_config(db_conn, child) or {}) == frozen_field_names()
