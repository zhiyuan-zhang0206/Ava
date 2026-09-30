"""Verified release authority failures must precede migration writes."""

import base64
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from base.deploy.release.runtime_release import ReleaseRejectedError, file_sha256
from base.deploy.schema.runtime_migration import (
    ReleaseMigrationContext,
    installed_migration_paths,
)
from base.deploy.state.cluster_lock import DeployLease


def test_installed_readonly_inventory_rejects_unlisted_and_changed_sql(tmp_path: Path) -> None:
    from base.deploy.schema import runtime_migration

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

    def locate(name: object) -> Path:
        return Path(runtime_migration.__file__) if isinstance(name, str) else path

    distribution.locate_file.side_effect = locate
    with patch(
        "base.deploy.schema.runtime_migration.importlib.metadata.distribution",
        return_value=distribution,
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


def test_migration_context_rejects_another_acquisition() -> None:
    acquired = datetime(2026, 9, 3, tzinfo=UTC)
    context = ReleaseMigrationContext(MagicMock(), MagicMock(), "host:pid1", acquired, "a" * 40)
    other = DeployLease(
        holder="host:pid1",
        held_for_s=1,
        expires_in_s=30,
        kind="rollout",
        acquired_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    with (
        patch("base.deploy.state.cluster_lock.read_update_lease", return_value=other),
        pytest.raises(ReleaseRejectedError, match="current rollout"),
    ):
        context.assert_operation(MagicMock())
