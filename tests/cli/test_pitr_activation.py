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
    pg = {
        "archive_mode": "off",
        "archive_command": "",
        "archive_timeout": "0",
        "wal_compression": "off",
        "system_identifier": "42",
        "direct_db_url": "dbname=ava",
    }
    credentials = {"uploader_client_email": "writer@example.test"}
    monkeypatch.setattr(activation, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(
        activation,
        "_shadow_readiness",
        lambda: activation.ShadowReadiness(pg=pg, credentials=credentials),
    )
    monkeypatch.setattr(
        "cli.commands._update_git.snapshot_pre_activation_data", lambda **_kwargs: snapshot
    )
    monkeypatch.setattr("services.backup.activation_snapshot", lambda _operation_id: None)
    monkeypatch.setattr(activation, "_read_pg_state", lambda: pg)
    monkeypatch.setattr(activation, "_validate_secrets", lambda: credentials)
    monkeypatch.setattr(activation, "_validate_snapshot", lambda _record: None)
    monkeypatch.setattr("shared.cluster_lock.acquire_update_lock", lambda *_a, **_kw: True)
    monkeypatch.setattr("shared.cluster_lock.release_update_lock", lambda *_a, **_kw: None)

    assert activation.cmd_pitr_activate(origin="agent:405") == 0
    record = load_record(tmp_path)
    assert record is not None
    assert record.phase == "wal_config_pending"
    assert record.pre_activation_snapshot == str(snapshot)
    assert record.pre_activation_pg_settings == pg
    assert record.pre_activation_credential_evidence == credentials


def test_activate_resume_never_repeats_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_record(
        tmp_path,
        ActivationRecord.start(operation_id="op-1", origin="cli").advance(
            "wal_config_pending",
            pre_activation_snapshot="/verified.dump.enc",
            pre_activation_pg_settings={"archive_mode": "off"},
            pre_activation_credential_evidence={"viewer": "separate"},
        ),
    )
    monkeypatch.setattr(activation, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(activation, "_validate_snapshot", lambda _record: None)
    monkeypatch.setattr("shared.cluster_lock.acquire_update_lock", lambda *_a, **_kw: True)
    monkeypatch.setattr("shared.cluster_lock.release_update_lock", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        "cli.commands._update_git.snapshot_pre_activation_data",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("snapshot repeated")),
    )

    assert activation.cmd_pitr_activate(origin="cli") == 0


def test_rollback_preserves_snapshot_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = ActivationRecord.start(operation_id="op-1", origin="cli").advance(
        "wal_config_pending",
        pre_activation_snapshot="/verified.dump.enc",
        pre_activation_pg_settings={"archive_mode": "off"},
        pre_activation_credential_evidence={"viewer": "separate"},
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

    def snapshot(**_kwargs: object) -> Path:
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
        pre_activation_credential_evidence={"viewer": "separate"},
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

    def snapshot(**_kwargs: object) -> Path:
        nonlocal called
        called = True
        return tmp_path / "unexpected"

    monkeypatch.setattr("cli.commands._update_git.snapshot_pre_activation_data", snapshot)
    assert activation.cmd_pitr_activate(origin="cli") == 1
    assert called is False
    record = load_record(tmp_path)
    assert record is not None and record.phase == "shadow"
    assert record.error == "RuntimeError"


def test_release_failure_does_not_roll_back_durable_phase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot = tmp_path / "verified.dump.enc"
    pg = {"archive_mode": "off", "direct_db_url": "dbname=ava"}
    credentials = {"viewer": "separate"}
    monkeypatch.setattr(activation, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(
        activation,
        "_shadow_readiness",
        lambda: activation.ShadowReadiness(pg=pg, credentials=credentials),
    )
    monkeypatch.setattr(activation, "_read_pg_state", lambda: pg)
    monkeypatch.setattr(activation, "_validate_secrets", lambda: credentials)
    monkeypatch.setattr(activation, "_validate_snapshot", lambda _record: None)
    monkeypatch.setattr("services.backup.activation_snapshot", lambda _operation_id: None)
    monkeypatch.setattr(
        "cli.commands._update_git.snapshot_pre_activation_data", lambda **_kwargs: snapshot
    )
    monkeypatch.setattr("shared.cluster_lock.acquire_update_lock", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        "shared.cluster_lock.release_update_lock",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("release failed")),
    )

    assert activation.cmd_pitr_activate(origin="cli") == 1
    record = load_record(tmp_path)
    assert record is not None
    assert record.phase == "wal_config_pending"
    assert record.pre_activation_snapshot == str(snapshot)
    assert record.error == "RuntimeError"


def test_credential_evidence_changes_fail_independently_of_pg_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pg = {"archive_mode": "off", "direct_db_url": "dbname=ava"}
    record = ActivationRecord.start(operation_id="op-1", origin="cli").advance(
        "snapshot_pending",
        pre_activation_pg_settings=pg,
        pre_activation_credential_evidence={"viewer_client_email": "old@example.test"},
    )
    write_record(tmp_path, record)
    monkeypatch.setattr(activation, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(activation, "_read_pg_state", lambda: pg)
    monkeypatch.setattr(
        activation,
        "_validate_secrets",
        lambda: {"viewer_client_email": "new@example.test"},
    )
    monkeypatch.setattr("shared.cluster_lock.acquire_update_lock", lambda *_a, **_kw: True)
    monkeypatch.setattr("shared.cluster_lock.release_update_lock", lambda *_a, **_kw: None)

    assert activation.cmd_pitr_activate(origin="cli") == 1
    failed = load_record(tmp_path)
    assert failed is not None
    assert failed.phase == "snapshot_pending"
    assert failed.pre_activation_pg_settings == pg
    assert failed.error == "RuntimeError"


def test_frozen_pg_state_contract_with_real_reader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """QA #4788 contract: the frozen pre_activation_pg_settings must be exactly
    the REAL _read_pg_state() face — nothing merged into it. Runs the actual
    reader against a real throwaway PG17 (only the registry record and the
    admin-URL formatter are faked; the reader's connections, SQL, key-set
    construction, pg_controldata and psutil cross-checks all execute for real).

    Discriminator: a frozen dict carrying extra credential-evidence keys — the
    shape the old `current.update(credential_evidence)` merge produced — must
    FAIL the same comparison, so a re-merge is caught instead of hidden by a
    mock that copies the defect."""
    import socket
    import subprocess
    from contextlib import closing
    from types import SimpleNamespace

    from shared.pg_tools import pg_tool

    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    data = tmp_path / "pg"
    log = tmp_path / "pg.log"
    subprocess.run(  # noqa: S603 — resolved pg binaries + static flags
        [pg_tool("initdb"), "-D", str(data), "-U", "ava", "-A", "trust"],
        check=True,
        capture_output=True,
    )
    subprocess.run(  # noqa: S603
        [
            pg_tool("pg_ctl"),
            "-D",
            str(data),
            "-l",
            str(log),
            "-w",
            "-t",
            "30",
            "start",
            "-o",
            f"-p {port} -c listen_addresses=127.0.0.1 "
            "-c fsync=off -c full_page_writes=off -c synchronous_commit=off",
        ],
        check=True,
        capture_output=True,
    )
    try:
        subprocess.run(  # noqa: S603
            [pg_tool("createdb"), "-h", "127.0.0.1", "-p", str(port), "-U", "ava", "ava"],
            check=True,
            capture_output=True,
        )
        # Environment plumbing is faked; the reader itself runs for real.
        monkeypatch.setattr(activation, "ava_home", lambda: tmp_path)
        monkeypatch.setattr(
            "shared.cluster.get_record",
            lambda _home: SimpleNamespace(ports={"postgres": port}, db_name="ava"),
        )
        monkeypatch.setattr(
            "cli.commands._cluster_instance.pg_admin_url",
            lambda _pg_port: f"postgresql://ava@127.0.0.1:{port}/postgres",
        )

        frozen = activation._read_pg_state()
        # The frozen face round-trips: unchanged cluster -> comparison passes.
        activation._require_same_pg_state(frozen, "contract")
        # Reading twice yields the identical 12-key face (stable, no drift).
        assert activation._read_pg_state() == frozen
        # Old defect shape: any extra credential-evidence key must fail.
        with pytest.raises(RuntimeError, match="changed"):
            activation._require_same_pg_state(
                {**frozen, "uploader_client_email": "writer@example.test"}, "contract"
            )
    finally:
        subprocess.run(  # noqa: S603
            [pg_tool("pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
            check=False,
            capture_output=True,
        )
