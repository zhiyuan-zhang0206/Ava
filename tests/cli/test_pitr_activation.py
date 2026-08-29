# pyright: reportUnknownArgumentType=false

from __future__ import annotations

from pathlib import Path

import pytest

from cli.commands import _pitr_activation as activation
from services.pitr.activation_state import ActivationRecord, load_record, write_record


def test_activate_persists_snapshot_before_wal_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
    monkeypatch.setattr("services.backup.activation_snapshots_since", lambda _started: [])
    monkeypatch.setattr(activation, "_read_pg_state", activation._shadow_readiness)
    monkeypatch.setattr(activation, "_validate_snapshot", lambda _record: None)
    monkeypatch.setattr("shared.cluster_lock.acquire_update_lock", lambda *_a, **_kw: True)
    monkeypatch.setattr("shared.cluster_lock.release_update_lock", lambda *_a, **_kw: None)

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


def test_activate_resume_never_repeats_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_record(
        tmp_path,
        ActivationRecord.start(operation_id="op-1", origin="cli").advance(
            "wal_config_pending",
            pre_activation_snapshot="/verified.dump.enc",
            pre_activation_pg_settings={"archive_mode": "off"},
        ),
    )
    monkeypatch.setattr(activation, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(activation, "_validate_snapshot", lambda _record: None)
    monkeypatch.setattr("shared.cluster_lock.acquire_update_lock", lambda *_a, **_kw: True)
    monkeypatch.setattr("shared.cluster_lock.release_update_lock", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        "cli.commands._update_git.snapshot_pre_activation_data",
        lambda: (_ for _ in ()).throw(AssertionError("snapshot repeated")),
    )

    assert activation.cmd_pitr_activate(origin="cli") == 0


def test_rollback_preserves_snapshot_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = ActivationRecord.start(operation_id="op-1", origin="cli").advance(
        "wal_config_pending",
        pre_activation_snapshot="/verified.dump.enc",
        pre_activation_pg_settings={"archive_mode": "off"},
    )
    write_record(tmp_path, record)
    monkeypatch.setattr(activation, "ava_home", lambda: tmp_path)
    monkeypatch.setattr("shared.cluster_lock.acquire_update_lock", lambda *_a, **_kw: True)
    monkeypatch.setattr("shared.cluster_lock.release_update_lock", lambda *_a, **_kw: None)

    assert activation.cmd_pitr_rollback() == 0
    assert activation.cmd_pitr_rollback() == 0
    rolled_back = load_record(tmp_path)
    assert rolled_back is not None
    assert rolled_back.phase == "rolled_back"
    assert rolled_back.pre_activation_snapshot == "/verified.dump.enc"


def test_concurrent_activation_refuses_without_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(activation, "ava_home", lambda: tmp_path)
    monkeypatch.setattr("shared.cluster_lock.acquire_update_lock", lambda *_a, **_kw: False)
    called = False

    def snapshot() -> Path:
        nonlocal called
        called = True
        return tmp_path / "unexpected"

    monkeypatch.setattr("cli.commands._update_git.snapshot_pre_activation_data", snapshot)
    assert activation.cmd_pitr_activate(origin="cli") == 1
    assert called is False
    record = load_record(tmp_path)
    assert record is not None and record.started_at and record.error == "RuntimeError"


def test_concurrent_rollback_persists_error_and_preserves_phase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = ActivationRecord.start(operation_id="op-1", origin="cli").advance(
        "wal_config_pending",
        pre_activation_snapshot="/verified.dump.enc",
        pre_activation_pg_settings={"archive_mode": "off"},
    )
    write_record(tmp_path, record)
    monkeypatch.setattr(activation, "ava_home", lambda: tmp_path)
    monkeypatch.setattr("shared.cluster_lock.acquire_update_lock", lambda *_a, **_kw: False)

    assert activation.cmd_pitr_rollback() == 1
    refused = load_record(tmp_path)
    assert refused is not None
    assert refused.operation_id == record.operation_id
    assert refused.phase == "wal_config_pending"
    assert refused.started_at == record.started_at
    assert refused.error == "RuntimeError"


def test_shadow_drift_fails_before_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(activation, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(
        activation,
        "_shadow_readiness",
        lambda: (_ for _ in ()).throw(RuntimeError("archive_mode is already on")),
    )
    monkeypatch.setattr("shared.cluster_lock.acquire_update_lock", lambda *_a, **_kw: True)
    monkeypatch.setattr("shared.cluster_lock.release_update_lock", lambda *_a, **_kw: None)
    called = False

    def snapshot() -> Path:
        nonlocal called
        called = True
        return tmp_path / "unexpected"

    monkeypatch.setattr("cli.commands._update_git.snapshot_pre_activation_data", snapshot)
    assert activation.cmd_pitr_activate(origin="cli") == 1
    assert called is False
    record = load_record(tmp_path)
    assert record is not None and record.phase == "shadow"
    assert record.error == "RuntimeError"
