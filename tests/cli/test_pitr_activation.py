from __future__ import annotations

from pathlib import Path

from cli.commands import _pitr_activation as activation
from services.pitr.activation_state import ActivationRecord, load_record, write_record


def test_activate_persists_snapshot_before_wal_pending(monkeypatch, tmp_path: Path) -> None:
    snapshot = tmp_path / "backups" / "verified.dump.enc"
    monkeypatch.setattr(activation, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(
        activation,
        "_shadow_readiness",
        lambda: {
            "archive_mode": "off",
            "archive_command": "",
            "archive_timeout": "0",
            "wal_compression": "off",
            "system_identifier": "42",
        },
    )
    monkeypatch.setattr("cli.commands._update_git.snapshot_pre_activation_data", lambda: snapshot)

    assert activation.cmd_pitr_activate(origin="agent:405") == 0
    record = load_record(tmp_path)
    assert record is not None
    assert record.phase == "wal_config_pending"
    assert record.pre_activation_snapshot == str(snapshot)
    assert record.pre_activation_pg_settings == {
        "archive_mode": "off",
        "archive_command": "",
        "archive_timeout": "0",
        "wal_compression": "off",
        "system_identifier": "42",
    }


def test_activate_resume_never_repeats_snapshot(monkeypatch, tmp_path: Path) -> None:
    write_record(
        tmp_path,
        ActivationRecord.start(operation_id="op-1", origin="cli").advance(
            "wal_config_pending",
            pre_activation_snapshot="/verified.dump.enc",
            pre_activation_pg_settings={"archive_mode": "off"},
        ),
    )
    monkeypatch.setattr(activation, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(
        "cli.commands._update_git.snapshot_pre_activation_data",
        lambda: (_ for _ in ()).throw(AssertionError("snapshot repeated")),
    )

    assert activation.cmd_pitr_activate(origin="cli") == 0


def test_rollback_preserves_snapshot_and_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    record = ActivationRecord.start(operation_id="op-1", origin="cli").advance(
        "wal_config_pending", pre_activation_snapshot="/verified.dump.enc"
    )
    write_record(tmp_path, record)
    monkeypatch.setattr(activation, "ava_home", lambda: tmp_path)

    assert activation.cmd_pitr_rollback() == 0
    assert activation.cmd_pitr_rollback() == 0
    rolled_back = load_record(tmp_path)
    assert rolled_back is not None
    assert rolled_back.phase == "rolled_back"
    assert rolled_back.pre_activation_snapshot == "/verified.dump.enc"
