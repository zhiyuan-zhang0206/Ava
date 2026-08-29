from __future__ import annotations

import json
from pathlib import Path

from services.pitr import restore_proof
from services.pitr.restore_postgres import _write_sandbox_config


def test_sandbox_config_ignores_restored_config_and_disables_host_side_effects(
    tmp_path: Path,
) -> None:
    pgdata = tmp_path / "sandbox" / "data"
    socket_dir = tmp_path / "socket"
    pgdata.mkdir(parents=True)
    socket_dir.mkdir()
    (pgdata / "postgresql.conf").write_text(
        "include='host.conf'\nshared_preload_libraries='host_library'\n"
    )

    config = _write_sandbox_config(pgdata, socket_dir, 55432, tmp_path)

    value = config.read_text()
    assert "include" not in value
    assert "host.conf" not in value
    assert "host_library" not in value
    for setting in (
        "archive_mode = 'off'",
        "ssl = 'off'",
        "logging_collector = 'off'",
        "shared_preload_libraries = ''",
        "session_preload_libraries = ''",
        "local_preload_libraries = ''",
        "primary_conninfo = ''",
    ):
        assert setting in value


def test_reconcile_removes_stale_postmaster_evidence_only_after_owner_is_dead(
    tmp_path: Path, monkeypatch
) -> None:
    restore_root = tmp_path / "restore"
    owners = tmp_path / "restore-owners"
    partial = restore_root / ".run.partial"
    pgdata = partial / "sandbox" / "data"
    pgdata.mkdir(parents=True)
    owners.mkdir()
    (pgdata / "postmaster.pid").write_text("999999\n")
    owner = owners / "run.owner.json"
    owner.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "state": "postgres_running",
                "partial": str(partial),
                "pid": 888888,
                "created_at": 1.0,
                "pgid": 888888,
                "deadline": 1.0,
                "sandbox_pid": 999999,
                "sandbox_created_at": 1.0,
                "sandbox_pgid": 888888,
                "sandbox_pgdata": str(pgdata),
            }
        )
    )
    monkeypatch.setattr(restore_proof, "_matching_process", lambda _pid, _created: None)
    monkeypatch.setattr(restore_proof, "_group_members", lambda _pgid: [])

    restore_proof.reconcile_restore_runtime(tmp_path)

    assert not partial.exists()
    assert not owner.exists()
