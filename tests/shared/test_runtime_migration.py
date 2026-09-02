"""Verified release authority failures must precede migration writes."""

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from shared.cluster_lock import DeployLease
from shared.config import settings
from shared.migrations import (
    MigrationAuthorityMismatch,
    _assert_migration_authority,
    _ensure_cutover,
)
from shared.runtime_migration import ReleaseMigrationContext, installed_migration_paths
from shared.runtime_release import ReleaseRejectedError, file_sha256, verify_release


def test_installed_readonly_inventory_rejects_unlisted_and_changed_sql(tmp_path: Path) -> None:
    from shared import runtime_migration

    path = tmp_path / "29991231T235959_inventory.sql"
    path.write_text("SELECT 1")
    record = MagicMock()
    record.hash.mode = "sha256"
    record.hash.value = (
        base64.urlsafe_b64encode(bytes.fromhex(file_sha256(path))).decode().rstrip("=")
    )
    distribution = MagicMock()
    distribution.files = [record]
    distribution.read_text.return_value = None
    distribution.locate_file.side_effect = lambda name: (
        Path(runtime_migration.__file__) if isinstance(name, str) else path
    )
    with patch(
        "shared.runtime_migration.importlib.metadata.distribution", return_value=distribution
    ):
        assert installed_migration_paths(tmp_path) == {path}
        extra = tmp_path / "29991231T235958_unlisted.sql"
        extra.write_text("SELECT 2")
        with pytest.raises(ReleaseRejectedError, match="undeclared"):
            installed_migration_paths(tmp_path)
        extra.unlink()
        path.write_text("SELECT 3")
        with pytest.raises(ReleaseRejectedError, match="differs"):
            installed_migration_paths(tmp_path)


def test_release_receipt_rejects_another_acquisition() -> None:
    acquired = datetime(2026, 9, 3, tzinfo=UTC)
    context = ReleaseMigrationContext(MagicMock(), MagicMock(), "host:pid1", acquired, "a" * 40)
    other = DeployLease(
        holder="host:pid1",
        held_for_s=1,
        expires_in_s=30,
        note=None,
        kind="rollout",
        acquired_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    with (
        patch("shared.runtime_migration.read_update_lease", return_value=other),
        pytest.raises(ReleaseRejectedError, match="current rollout"),
    ):
        context.assert_operation(MagicMock())


def test_release_cannot_use_fresh_birth_empty_roster_exception() -> None:
    context = MagicMock(spec=ReleaseMigrationContext)
    context.home = "/unit"
    with (
        patch("shared.migrations._gateway_units", return_value=[]),
        patch("shared.migrations.machine_name", return_value="host"),
        pytest.raises(MigrationAuthorityMismatch),
    ):
        _assert_migration_authority(MagicMock(), context)


def test_legacy_cutover_refuses_wrong_gateway_before_transaction() -> None:
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value.fetchall.return_value = [
        (version,) for version in range(1, 82)
    ]
    with (
        patch("shared.migrations._schema_migrations_shape", return_value="legacy"),
        patch(
            "shared.migrations._assert_migration_authority",
            side_effect=MigrationAuthorityMismatch("wrong gateway"),
        ),
        pytest.raises(MigrationAuthorityMismatch, match="wrong gateway"),
    ):
        _ensure_cutover(connection)
    connection.transaction.assert_not_called()
    executed = connection.cursor.return_value.__enter__.return_value.execute.call_args_list
    assert len(executed) == 1
    assert executed[0].args[0] == "SELECT version FROM schema_migrations"


def test_verified_inventory_applies_without_git_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real PG transaction; synthetic image exercises authority, not launch closure."""
    from shared.migrations import apply_pending_migrations

    home = tmp_path.resolve()
    store = home / "releases"
    digest = "a" * 64
    root = store / digest
    directory = root / "venv/lib/migrations"
    directory.mkdir(parents=True)
    interpreter = root / "venv/python"
    interpreter.write_bytes(b"not executed: filesystem authority fixture")
    migration = directory / "29991231T235959_runtime-authority.sql"
    migration.write_text("CREATE TABLE runtime_migration_probe (id integer)")
    manifest = {
        "version": 1,
        "artifact_digest": digest,
        "platform": "test-platform",
        "schema_digest": "b" * 64,
        "interpreter": "venv/python",
        "cwd": "venv",
        "files": {p.relative_to(root).as_posix(): file_sha256(p) for p in (interpreter, migration)},
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    release = verify_release(
        store,
        digest,
        manifest_digest=file_sha256(manifest_path),
        platform_tag="test-platform",
        schema_digest="b" * 64,
    )
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", directory)
    monkeypatch.setattr("shared.migrations.machine_name", lambda: "runtime-proof")
    with psycopg.connect(settings.data_plane.db_url) as connection:
        try:
            connection.execute("DELETE FROM machine_units")
            connection.execute(
                "INSERT INTO machine_units (machine_name,home,serve_gateway,serve_agent_runner) "
                "VALUES (%s,%s,true,true)",
                ("runtime-proof", str(home)),
            )
            acquired_row = connection.execute(
                "UPDATE deployment_state SET holder='runtime-proof:pid1',acquired_at=now(), "
                "expires_at=now()+interval '5 minutes',kind='rollout',phase='updating', "
                "note=NULL,target_sha=%s WHERE id=1 RETURNING acquired_at",
                ("c" * 40,),
            ).fetchone()
            assert acquired_row is not None
            context = ReleaseMigrationContext(
                release, home, "runtime-proof:pid1", acquired_row[0], "c" * 40
            )
            assert apply_pending_migrations(connection, release=context) == [migration.stem]
            created = connection.execute("SELECT to_regclass('runtime_migration_probe')").fetchone()
            assert created is not None and created[0] is not None
        finally:
            connection.rollback()
        removed = connection.execute("SELECT to_regclass('runtime_migration_probe')").fetchone()
        assert removed is not None and removed[0] is None
