"""Provisioning of the built-in schedules: the gateway side of `builtin_schedules`.

The provision path is also exercised at the gateway-boot level by
test_schedules_api.py's TestClient(app) lifespan; these tests pin the DB behavior
directly. The manifest parsing lives in base/daemon/schedules/tests/test_builtin_schedules.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.daemon.schedules.builtin_schedules import (
    ProvisionResult,
    load_manifest,
    provision_builtin_schedules,
)

_ENTRY: dict[str, object] = {
    "name": "prod-a",
    "class": "product",
    "default_enabled": True,
    "script": "a.py",
    "command": "python a.py",
    "description": "A",
}


def _manifest(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    """Write a fixture manifest with one tiny script per entry."""
    for e in entries:
        script = e["script"]
        assert isinstance(script, str)
        (tmp_path / script).write_text("print('ok')\n")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"version": 1, "builtin_schedules": entries})
    )
    return tmp_path / "manifest.json"


def _names(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM schedules")
        return {r[0] for r in cur.fetchall()}


def _row(conn: psycopg.Connection, name: str) -> Any:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, enabled, script, command FROM schedules WHERE name = %s",
            (name,),
        )
        return cur.fetchone()


def _versions(conn: psycopg.Connection, name: str) -> list[tuple[str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT v.note FROM schedule_versions v JOIN schedules s ON s.id = v.schedule_id "
            "WHERE s.name = %s ORDER BY v.id",
            (name,),
        )
        return cur.fetchall()


class TestProvision:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("automatic", [False, True])
    async def test_service_start_respects_seeding_without_disabling_explicit_provision(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, automatic: bool
    ) -> None:
        from base.config import settings
        from services.schedule_manager import daemon

        monkeypatch.setattr(settings.gateway, "provision_builtin_schedules", automatic)
        assert _names(db_conn) == set()
        expected = {item.name for item in load_manifest()}
        with ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=1) as pool:
            await daemon.provision_builtins(pool)
            assert _names(db_conn) == (expected if automatic else set())
            # Manual provisioning remains a deliberate action, even on an unseeded home.
            provision_builtin_schedules(db_conn)
            db_conn.commit()
            before = {name: _row(db_conn, name) for name in expected}
            await daemon.provision_builtins(pool)
            assert {name: _row(db_conn, name) for name in expected} == before

    def test_creates_missing_with_manifest_defaults(
        self, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        path = _manifest(
            tmp_path,
            [
                {
                    "name": "prod-a",
                    "class": "product",
                    "default_enabled": True,
                    "script": "a.py",
                    "command": "python a.py",
                    "description": "A",
                },
                {
                    "name": "op-b",
                    "class": "operator",
                    "default_enabled": False,
                    "script": "b.py",
                    "command": "python b.py",
                    "description": "B",
                },
            ],
        )
        created = provision_builtin_schedules(db_conn, path=path)
        db_conn.commit()
        assert created.created == ["prod-a", "op-b"] and created.resynced == []
        assert _row(db_conn, "prod-a")[1] is True
        assert _row(db_conn, "op-b")[1] is False
        assert _row(db_conn, "op-b")[2] == "print('ok')\n"

    def test_idempotent_never_touches_existing(
        self, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        path = _manifest(
            tmp_path,
            [
                {
                    "name": "prod-a",
                    "class": "product",
                    "default_enabled": True,
                    "script": "a.py",
                    "command": "python a.py",
                    "description": "A",
                },
            ],
        )
        assert provision_builtin_schedules(db_conn, path=path).created == ["prod-a"]
        db_conn.commit()
        assert provision_builtin_schedules(db_conn, path=path) == ProvisionResult()
        db_conn.commit()
        assert _row(db_conn, "prod-a") == ("prod-a", True, "print('ok')\n", "python a.py")
        assert _versions(db_conn, "prod-a") == [("initial",)]

    def test_a_drifted_builtin_follows_the_template_and_keeps_its_operator_state(
        self, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        """2026-10-03: the DB copy of a built-in is a creation-time snapshot, and a library
        signature change left thirteen of them crash-looping. Provision now rewrites a differing
        script/command to the repo template, keeps `enabled` and the description, snapshots a
        version row, and queues the sync request that relaunches a live session."""
        path = _manifest(tmp_path, [_ENTRY])
        provision_builtin_schedules(db_conn, path=path)
        db_conn.commit()
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE schedules SET enabled = false, description = 'tuned', "
                "script = 'catch_up([])\n', command = 'python old.py' WHERE name = 'prod-a'"
            )
        db_conn.commit()

        result = provision_builtin_schedules(db_conn, path=path)
        db_conn.commit()

        assert result == ProvisionResult(created=[], resynced=["prod-a"])
        assert _row(db_conn, "prod-a") == ("prod-a", False, "print('ok')\n", "python a.py")
        with db_conn.cursor() as cur:
            cur.execute("SELECT description FROM schedules WHERE name = 'prod-a'")
            assert cur.fetchone() == ("tuned",)
            cur.execute(
                "SELECT count(*) FROM schedule_sync_requests q JOIN schedules s "
                "ON s.id = q.schedule_id WHERE s.name = 'prod-a'"
            )
            assert cur.fetchone() == (1,)
        versions = _versions(db_conn, "prod-a")
        assert versions[0] == ("initial",) and versions[1][0].startswith("builtin-resync ")

        # Converged: the next provision changes nothing and queues nothing new.
        assert provision_builtin_schedules(db_conn, path=path) == ProvisionResult()
        db_conn.commit()
        assert len(_versions(db_conn, "prod-a")) == 2

    def test_a_schedule_outside_the_manifest_is_never_touched(
        self, db_conn: psycopg.Connection, tmp_path: Path
    ) -> None:
        """An agent-created schedule keeps its DB script — provision neither reads nor rewrites it."""
        path = _manifest(tmp_path, [_ENTRY])
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO schedules (name, description, script, command, enabled) "
                "VALUES ('agent-made', 'x', 'print(1)\n', 'python x.py', true)"
            )
        db_conn.commit()
        assert provision_builtin_schedules(db_conn, path=path).created == ["prod-a"]
        db_conn.commit()
        assert _row(db_conn, "agent-made") == ("agent-made", True, "print(1)\n", "python x.py")

    def test_manifest_driven_restore(self, db_conn: psycopg.Connection, tmp_path: Path) -> None:
        """Deleting a built-in makes the next provision recreate it."""
        path = _manifest(
            tmp_path,
            [
                {
                    "name": "prod-a",
                    "class": "product",
                    "default_enabled": True,
                    "script": "a.py",
                    "command": "python a.py",
                    "description": "A",
                },
            ],
        )
        provision_builtin_schedules(db_conn, path=path)
        db_conn.commit()
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM schedules WHERE name = 'prod-a'")
        db_conn.commit()
        assert provision_builtin_schedules(db_conn, path=path).created == ["prod-a"]
        db_conn.commit()
