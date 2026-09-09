"""Gateway fork config rules — task #2694 (preset-in-config-overlay, fork
cache-friendliness).

A fork must keep the source agent's effective config (so the inherited context
stays cache-valid); the only sanctioned change is ADDING skills to the two
skill lists. These tests drive POST /api/agents through TestClient with the
in-process spawn fixture (tests/gateway/conftest.py) and read the stored row +
the fork inbound payload through db_conn.
"""

from __future__ import annotations

import psycopg
from fastapi.testclient import TestClient

from gateway.app import app


def _checkpoint(conn: psycopg.Connection, agent_id: int) -> str:
    """Give `agent_id` one checkpoint so it can be forked from."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, "
            "checkpoint, metadata) VALUES (%s, '', 'ckpt-1', 'x', '{}'::jsonb, '{}'::jsonb)",
            (str(agent_id),),
        )
    conn.commit()
    return "ckpt-1"


def _row(conn: psycopg.Connection, agent_id: int) -> tuple[dict, str | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT config_overlay, preset_name FROM agents_meta WHERE id = %s", (agent_id,)
        )
        r = cur.fetchone()
    assert r is not None
    return (r[0] or {}), r[1]


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
    def test_fork_without_config_inherits_source_overlay_and_preset(
        self, db_conn: psycopg.Connection
    ) -> None:
        """The pre-ruling fork dropped the source's overlay (silent re-brain);
        now a bare fork copies overlay + preset_name verbatim — the fork runs
        exactly what the source ran, so the cached prefix survives."""
        with TestClient(app) as client:
            client.post(
                "/api/presets",
                json={
                    "name": "coder",
                    "label": "Coder",
                    "config": {"llm_model": "claude-sonnet-5"},
                },
            )
            src = client.post(
                "/api/agents",
                json={
                    "spawner": "user",
                    "config": {"preset": "coder", "llm_model": "deepseek-v4-pro"},
                },
            ).json()["id"]
            _checkpoint(db_conn, src)
            fork = client.post("/api/agents", json={"spawner": "user", "fork_from": src}).json()[
                "id"
            ]
        overlay, preset_name = _row(db_conn, fork)
        assert preset_name == "coder"
        assert overlay == {"llm_model": "deepseek-v4-pro"}
        assert _fork_inbound(db_conn, fork) is None

    def test_fork_with_identical_explicit_config_is_allowed(
        self, db_conn: psycopg.Connection
    ) -> None:
        with TestClient(app) as client:
            src = client.post(
                "/api/agents",
                json={"spawner": "user", "config": {"llm_model": "claude-sonnet-5"}},
            ).json()["id"]
            _checkpoint(db_conn, src)
            r = client.post(
                "/api/agents",
                json={
                    "spawner": "user",
                    "fork_from": src,
                    "config": {"llm_model": "claude-sonnet-5"},
                },
            )
            assert r.status_code == 201, r.text
            fork = r.json()["id"]
        overlay, preset_name = _row(db_conn, fork)
        assert preset_name is None
        assert overlay == {"llm_model": "claude-sonnet-5"}

    def test_fork_model_change_is_rejected(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            src = client.post(
                "/api/agents",
                json={"spawner": "user", "config": {"llm_model": "claude-sonnet-5"}},
            ).json()["id"]
            _checkpoint(db_conn, src)
            r = client.post(
                "/api/agents",
                json={
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
        with TestClient(app) as client:
            src = client.post(
                "/api/agents",
                json={
                    "spawner": "user",
                    "config": {"skills_to_expand_at_start": ["a", "b"]},
                },
            ).json()["id"]
            _checkpoint(db_conn, src)
            r = client.post(
                "/api/agents",
                json={
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
        with TestClient(app) as client:
            src = client.post(
                "/api/agents",
                json={
                    "spawner": "user",
                    "config": {
                        "skills_to_inject_into_system_prompt": ["a"],
                        "skills_to_expand_at_start": ["e"],
                    },
                },
            ).json()["id"]
            _checkpoint(db_conn, src)
            r = client.post(
                "/api/agents",
                json={
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
        overlay, preset_name = _row(db_conn, fork)
        assert preset_name is None
        assert overlay == {
            "skills_to_inject_into_system_prompt": ["a", "b"],
            "skills_to_expand_at_start": ["e", "b"],
        }
        # "b" is added to BOTH lists: the expand graft already covers it, so
        # the tail payload carries no delta.
        assert _fork_inbound(db_conn, fork) is None

    def test_fork_inject_only_addition_carries_tail_delta(
        self, db_conn: psycopg.Connection
    ) -> None:
        with TestClient(app) as client:
            src = client.post(
                "/api/agents",
                json={"spawner": "user", "config": {"skills_to_inject_into_system_prompt": ["a"]}},
            ).json()["id"]
            _checkpoint(db_conn, src)
            r = client.post(
                "/api/agents",
                json={
                    "spawner": "user",
                    "fork_from": src,
                    "config": {"skills_to_inject_into_system_prompt": ["a", "b"]},
                },
            )
            assert r.status_code == 201, r.text
            fork = r.json()["id"]
        assert _fork_inbound(db_conn, fork) == {"tail_skills": ["b"]}

    def test_fork_preset_adding_a_skill_is_allowed_and_carries_delta(
        self, db_conn: psycopg.Connection
    ) -> None:
        """The user's scenario 2b: fork passes a preset that adds a skill the
        source never had — allowed, and the skill rides the fork inbound
        payload for the tail graft (prefix + cache untouched)."""
        with TestClient(app) as client:
            client.post(
                "/api/presets",
                json={
                    "name": "extra",
                    "label": "Extra",
                    "config": {"skills_to_inject_into_system_prompt": ["b"]},
                },
            )
            # The source pins an explicit (empty) inject list: the cluster default
            # is the ["*"] wildcard, against which a narrow fork list would be a
            # removal and correctly rejected.
            src = client.post(
                "/api/agents",
                json={
                    "spawner": "user",
                    "config": {
                        "llm_model": "claude-sonnet-5",
                        "skills_to_inject_into_system_prompt": [],
                    },
                },
            ).json()["id"]
            _checkpoint(db_conn, src)
            r = client.post(
                "/api/agents",
                json={"spawner": "user", "fork_from": src, "config": {"preset": "extra"}},
            )
            assert r.status_code == 201, r.text
            fork = r.json()["id"]
        overlay, preset_name = _row(db_conn, fork)
        assert preset_name == "extra"
        assert overlay == {
            "llm_model": "claude-sonnet-5",
            "skills_to_inject_into_system_prompt": ["b"],
        }
        assert _fork_inbound(db_conn, fork) == {"tail_skills": ["b"]}

    def test_fork_preset_with_model_change_is_rejected(self, db_conn: psycopg.Connection) -> None:
        """A preset that changes a non-skill field is still a config change —
        the fork rule applies to the RESOLVED overlay."""
        with TestClient(app) as client:
            client.post(
                "/api/presets",
                json={
                    "name": "other",
                    "label": "Other",
                    "config": {"llm_model": "deepseek-v4-pro"},
                },
            )
            src = client.post(
                "/api/agents", json={"spawner": "user", "config": {"llm_model": "claude-sonnet-5"}}
            ).json()["id"]
            _checkpoint(db_conn, src)
            r = client.post(
                "/api/agents",
                json={"spawner": "user", "fork_from": src, "config": {"preset": "other"}},
            )
        assert r.status_code == 400
        assert r.json()["reason"] == "fork_config_change_not_allowed"

    def test_fork_source_row_missing_is_404(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            r = client.post("/api/agents", json={"spawner": "user", "fork_from": 99999})
        assert r.status_code == 404
        assert r.json()["reason"] == "agent_not_found"
