"""Reject invalid saved defaults and keep implicit notice reads page-bounded."""

from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from shared import runtime_config
from shared.config import settings
from shared.config.display import DisplaySettings
from tests.gateway.test_notices_endpoint import _seed_agent
from tests.shared.test_display_config import DISPLAY_RANGES


@pytest.mark.parametrize(("name", "minimum", "maximum"), DISPLAY_RANGES)
def test_config_write_rejects_invalid_default_without_changing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, minimum: int, maximum: int
) -> None:
    monkeypatch.setattr(runtime_config, "_ava_home", lambda: tmp_path)
    runtime_config.write_fields({"notices_open_default_limit": 200}, set())
    env_path = tmp_path / ".env"
    before = env_path.read_bytes()

    with TestClient(app) as client:
        for value in (minimum - 1, maximum + 1):
            response = client.put("/api/config", json={name: value})
            assert response.status_code == 400, response.text
            assert env_path.read_bytes() == before


@pytest.mark.parametrize(("name", "minimum", "maximum"), DISPLAY_RANGES)
def test_config_write_accepts_boundary_defaults_without_clamping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, minimum: int, maximum: int
) -> None:
    monkeypatch.setattr(runtime_config, "_ava_home", lambda: tmp_path)
    alias = DisplaySettings.model_fields[name].alias
    assert isinstance(alias, str)
    with TestClient(app) as client:
        for value in (minimum, maximum):
            response = client.put("/api/config", json={name: value})
            assert response.status_code == 200, response.text
            assert runtime_config.read_env_aliases()[alias] == str(value)


@pytest.mark.parametrize(("open_limit", "resolved_limit"), ((1, 1), (500, 100)))
def test_implicit_notice_reads_use_validated_boundary_defaults(
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection,
    open_limit: int,
    resolved_limit: int,
) -> None:
    configured = DisplaySettings.model_validate(
        {"notices_open_default_limit": open_limit, "notices_resolved_default_page": resolved_limit}
    )
    monkeypatch.setattr(settings, "display", configured)
    agent_id = _seed_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            """INSERT INTO agent_notices
                (agent_id, local_id, title, priority, require_response, expire_at)
            SELECT %s, i, 'open', 'P3', false, now() + interval '1 day'
            FROM generate_series(1, 601) i""",
            (agent_id,),
        )
        cur.execute(
            """INSERT INTO agent_notices
                (agent_id, local_id, title, priority, require_response,
                 resolved_at, resolution, expire_at)
            SELECT %s, i + 1000, 'resolved', 'P3', false, now(), 'read', now() + interval '1 day'
            FROM generate_series(1, 101) i""",
            (agent_id,),
        )
    db_conn.commit()

    with TestClient(app) as client:
        for path, expected in (
            ("/api/notices/open", open_limit),
            ("/api/notices/live", open_limit),
            ("/api/notices/resolved", resolved_limit),
        ):
            implicit = client.get(path)
            explicit = client.get(path, params={"limit": expected})
            assert implicit.status_code == explicit.status_code == 200
            assert len(implicit.json()) == expected
            assert implicit.json() == explicit.json()
        feed = client.get("/api/notices")
        assert feed.status_code == 200
        assert len(feed.json()["open"]) == open_limit
        assert len(feed.json()["resolved_page"]) == resolved_limit
