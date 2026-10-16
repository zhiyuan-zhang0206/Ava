from __future__ import annotations

import psycopg

from tests.fixtures.gateway_config import gateway_test_client, spawn_via_http


def _checkpoint(conn: psycopg.Connection, agent_id: int) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, "
            "checkpoint, metadata) VALUES (%s, '', 'ckpt-1', 'x', '{}'::jsonb, '{}'::jsonb)",
            (str(agent_id),),
        )
    conn.commit()
    return "ckpt-1"


def _row(conn: psycopg.Connection, agent_id: int) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT config_overlay FROM agents_meta WHERE id = %s", (agent_id,))
        r = cur.fetchone()
    assert r is not None
    return r[0] or {}


def _fork_inbound(conn: psycopg.Connection, agent_id: int) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM inbound_messages WHERE agent_id = %s AND kind = 'fork'",
            (agent_id,),
        )
        r = cur.fetchone()
    assert r is not None
    return r[0]


class TestForkConfigStability:
    def test_fork_without_config_inherits_source_overlay(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            src = spawn_via_http(
                client,
                {"spawner": "user", "config": {"llm_model": "deepseek-flash"}},
            ).json()["id"]
            _checkpoint(db_conn, src)
            fork = spawn_via_http(client, {"spawner": "user", "fork_from": src}).json()["id"]
        overlay = _row(db_conn, fork)
        assert overlay == {"llm_model": "deepseek-flash"}
        assert _fork_inbound(db_conn, fork) is None

    def test_fork_with_identical_explicit_config_is_allowed(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            src = spawn_via_http(
                client, {"spawner": "user", "config": {"llm_model": "claude-sonnet-5"}}
            ).json()["id"]
            _checkpoint(db_conn, src)
            r = spawn_via_http(
                client,
                {
                    "spawner": "user",
                    "fork_from": src,
                    "config": {"llm_model": "claude-sonnet-5"},
                },
            )
            assert r.status_code == 201, r.text
            fork = r.json()["id"]
        overlay = _row(db_conn, fork)
        assert overlay == {"llm_model": "claude-sonnet-5"}

    def test_fork_model_change_is_rejected(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            src = spawn_via_http(
                client, {"spawner": "user", "config": {"llm_model": "claude-sonnet-5"}}
            ).json()["id"]
            _checkpoint(db_conn, src)
            r = spawn_via_http(
                client,
                {
                    "spawner": "user",
                    "fork_from": src,
                    "config": {"llm_model": "deepseek-v4-pro"},
                },
            )
        assert r.status_code == 400
        body = r.json()
        assert body["reason"] == "fork_config_change_not_allowed"
        assert "llm_model" in body["detail"]

    def test_fork_skill_removal_is_rejected(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            src = spawn_via_http(
                client,
                {
                    "spawner": "user",
                    "config": {"skills_to_expand_at_start": ["a", "b"]},
                },
            ).json()["id"]
            _checkpoint(db_conn, src)
            r = spawn_via_http(
                client,
                {
                    "spawner": "user",
                    "fork_from": src,
                    "config": {"skills_to_expand_at_start": ["a"]},
                },
            )
        assert r.status_code == 400
        assert r.json()["reason"] == "fork_config_change_not_allowed"

    def test_fork_skill_addition_superset_is_allowed_and_carries_delta(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            src = spawn_via_http(
                client,
                {
                    "spawner": "user",
                    "config": {
                        "skills_to_inject_into_system_prompt": ["a"],
                        "skills_to_expand_at_start": ["e"],
                    },
                },
            ).json()["id"]
            _checkpoint(db_conn, src)
            r = spawn_via_http(
                client,
                {
                    "spawner": "user",
                    "fork_from": src,
                    "config": {
                        "skills_to_inject_into_system_prompt": ["a", "b"],
                        "skills_to_expand_at_start": ["e", "b"],
                    },
                },
            )
            assert r.status_code == 201, r.text
            fork = r.json()["id"]
        overlay = _row(db_conn, fork)
        assert overlay == {
            "skills_to_inject_into_system_prompt": ["a", "b"],
            "skills_to_expand_at_start": ["e", "b"],
        }
        assert _fork_inbound(db_conn, fork) is None

    def test_fork_inject_only_addition_carries_tail_delta(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            src = spawn_via_http(
                client,
                {"spawner": "user", "config": {"skills_to_inject_into_system_prompt": ["a"]}},
            ).json()["id"]
            _checkpoint(db_conn, src)
            r = spawn_via_http(
                client,
                {
                    "spawner": "user",
                    "fork_from": src,
                    "config": {"skills_to_inject_into_system_prompt": ["a", "b"]},
                },
            )
            assert r.status_code == 201, r.text
            fork = r.json()["id"]
        assert _fork_inbound(db_conn, fork) == {"tail_skills": ["b"]}

    def test_fork_source_row_missing_is_404(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            r = spawn_via_http(client, {"spawner": "user", "fork_from": 99999})
        assert r.status_code == 404
        assert r.json()["reason"] == "agent_not_found"
