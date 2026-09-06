"""Tests for shared/builtin_schedules.py — the built-in schedules manifest
and its idempotent create-if-missing provisioning.

The provision path is also exercised at the gateway-boot level by
test_schedules_api.py's TestClient(app) lifespan; these tests pin the
manifest parsing + DB behavior directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
import pytest

from shared.builtin_schedules import (
    ManifestError,
    load_manifest,
    provision_builtin_schedules,
)


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


class TestLoadManifest:
    def test_loads_repo_manifest(self) -> None:
        """The repo manifest parses: product schedules enabled / operator schedules disabled."""
        scheds = load_manifest()
        by_name = {s.name: s for s in scheds}
        assert set(by_name) == {
            "c9-daily-report",
            "adversarial-eval-weekly",
            "self-evolution-weekly",
            "self-evolution-daily",
            "memory-arbiter",
            "model-update-tracker",
            "trace-ship-tempo",
        }
        assert all(
            s.klass == "product" and s.default_enabled
            for s in scheds
            if s.name != "trace-ship-tempo"
        )
        assert by_name["trace-ship-tempo"].klass == "operator"
        assert by_name["trace-ship-tempo"].default_enabled is False

    def test_unknown_class_fails_fast(self, tmp_path: Path) -> None:
        path = _manifest(
            tmp_path,
            [
                {
                    "name": "x",
                    "class": "mystery",
                    "default_enabled": True,
                    "script": "x.py",
                    "command": "python x.py",
                }
            ],
        )
        with pytest.raises(ManifestError, match="unknown class"):
            load_manifest(path)

    def test_missing_script_file_fails(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        path.write_text(
            json.dumps(
                {
                    "builtin_schedules": [
                        {
                            "name": "x",
                            "class": "product",
                            "default_enabled": True,
                            "script": "nope.py",
                            "command": "python nope.py",
                        }
                    ]
                }
            )
        )
        with pytest.raises(ManifestError, match="does not exist"):
            load_manifest(path)


class TestProvision:
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
        assert created == ["prod-a", "op-b"]
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
        assert provision_builtin_schedules(db_conn, path=path) == ["prod-a"]
        db_conn.commit()

        # Operator disabled an existing built-in and edited its script —
        # provision must leave both alone.
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE schedules SET enabled = false, script = 'print(2)\n' WHERE name = 'prod-a'"
            )
        db_conn.commit()

        assert provision_builtin_schedules(db_conn, path=path) == []
        db_conn.commit()
        assert _row(db_conn, "prod-a") == ("prod-a", False, "print(2)\n", "python a.py")

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
        assert provision_builtin_schedules(db_conn, path=path) == ["prod-a"]
        db_conn.commit()
