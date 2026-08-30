from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.pitr import activation_lease
from services.pitr.activation_state import (
    ActivationRecord,
    load_record,
    mark_pre_mutation_rolled_back,
    record_path,
    write_record,
)


def _credentials() -> dict[str, str]:
    return {
        "uploader_client_email": "u@example.test",
        "uploader_project_id": "project",
        "uploader_private_key_id": "u-key",
        "viewer_client_email": "v@example.test",
        "viewer_project_id": "project",
        "viewer_private_key_id": "v-key",
        "bucket_name": "bucket",
        "object_prefix": "pitr",
        "backup_key_id": "key",
        "backup_key_sha256": "0" * 64,
    }


def test_activation_lease_refuses_long_step_when_initial_ownership_is_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def lose_lease(_holder: str) -> bool:
        return False

    monkeypatch.setattr(activation_lease, "renew_update_lock", lose_lease)
    called = False

    def action(_stop: object) -> None:
        nonlocal called
        called = True

    with pytest.raises(RuntimeError, match="lost its deployment lease"):
        activation_lease.run_while_renewing("pitr:op", action)
    assert not called


def test_activation_lease_cancels_child_and_never_returns_after_renewal_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renewals = iter((True, False))
    monkeypatch.setattr(activation_lease, "LEASE_RENEW_INTERVAL_S", 0.001)

    def renew_then_lose(_holder: str) -> bool:
        return next(renewals, False)

    monkeypatch.setattr(activation_lease, "renew_update_lock", renew_then_lose)

    def action(stop: object) -> str:
        assert isinstance(stop, activation_lease.threading.Event)
        assert stop.wait(1)
        return "must not advance"

    with pytest.raises(RuntimeError, match="lost its deployment lease"):
        activation_lease.run_while_renewing("pitr:op", action)


def test_activation_record_keeps_original_started_at_across_resume(tmp_path: Path) -> None:
    first = ActivationRecord.start(operation_id="op-1", origin="agent:405")
    write_record(tmp_path, first)
    pending = first.advance(
        "snapshot_pending",
        pre_activation_pg_settings={"archive_mode": "off"},
        pre_activation_credential_evidence=_credentials(),
    )
    resumed = pending.advance(
        "snapshot_verified",
        pre_activation_snapshot="/backup.enc",
    )
    write_record(tmp_path, resumed)

    loaded = load_record(tmp_path)
    assert loaded is not None
    assert loaded.started_at == first.started_at
    assert loaded.updated_at >= first.updated_at
    assert loaded.phase == "snapshot_verified"
    assert record_path(tmp_path).stat().st_mode & 0o777 == 0o600


def test_activation_record_rejects_unknown_fields(tmp_path: Path) -> None:
    record = ActivationRecord.start(operation_id="op-1", origin="cli")
    write_record(tmp_path, record)
    path = record_path(tmp_path)
    raw = json.loads(path.read_text())
    raw["future"] = True
    path.write_text(json.dumps(raw))

    with pytest.raises(ValueError, match="fields differ"):
        load_record(tmp_path)


@pytest.mark.parametrize("missing", tuple(ActivationRecord.__dataclass_fields__))
def test_activation_record_v3_rejects_every_single_missing_field(missing: str) -> None:
    """The durable v3 record is closed-schema even when the missing value was null."""

    raw = json.loads(json.dumps(ActivationRecord.start(operation_id="op-1", origin="cli").__dict__))
    raw.pop(missing)

    with pytest.raises(ValueError, match="fields differ"):
        ActivationRecord.from_json(json.dumps(raw))


def test_activation_record_atomic_write_leaves_no_partial(tmp_path: Path) -> None:
    write_record(tmp_path, ActivationRecord.start(operation_id="op-1", origin="cli"))
    assert list(record_path(tmp_path).parent.glob(".operation-*.partial")) == []


def test_activation_record_rejects_unknown_phase(tmp_path: Path) -> None:
    write_record(tmp_path, ActivationRecord.start(operation_id="op-1", origin="cli"))
    path = record_path(tmp_path)
    raw = json.loads(path.read_text())
    raw["phase"] = "future"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="unknown PITR activation phase"):
        load_record(tmp_path)


def test_activation_record_rejects_half_active_evidence(tmp_path: Path) -> None:
    write_record(tmp_path, ActivationRecord.start(operation_id="op-1", origin="cli"))
    path = record_path(tmp_path)
    raw = json.loads(path.read_text())
    raw["phase"] = "wal_config_pending"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="missing logical recovery evidence"):
        load_record(tmp_path)


def test_activation_record_v3_crash_resume_requires_phase_evidence(tmp_path: Path) -> None:
    record = (
        ActivationRecord.start(operation_id="op-1", origin="cli")
        .advance(
            "snapshot_pending",
            pre_activation_pg_settings={"archive_mode": "off"},
            pre_activation_credential_evidence=_credentials(),
        )
        .advance("snapshot_verified", pre_activation_snapshot="/backup.enc")
        .advance("wal_config_pending")
        .advance(
            "wal_config_applying",
            wal_config_before_digest="before",
            wal_config_desired_digest="desired",
            pre_activation_pitr_env={"pitr_enabled": "__ABSENT__"},
            pre_activation_pg_auto_conf={"archive_mode": "__ABSENT__"},
            pre_activation_env_b64="YQ==",
            pre_activation_env_digest="env-digest",
            pre_activation_auto_conf_b64="Yg==",
            pre_activation_auto_conf_digest="auto-digest",
        )
    )
    write_record(tmp_path, record)

    loaded = load_record(tmp_path)
    assert loaded == record
    assert loaded is not None
    with pytest.raises(ValueError, match="restart orchestration evidence"):
        loaded.advance("wal_restart_pending")


def test_activation_record_v3_rejects_non_string_nested_evidence(tmp_path: Path) -> None:
    record = ActivationRecord.start(operation_id="op-1", origin="cli")
    raw = json.loads(json.dumps(record.__dict__))
    raw["wal_exact_evidence"] = {
        "timeline": "1",
        "segment": 1,
        "switch_lsn": "0/1",
        "failed_count": "0",
    }
    record_path(tmp_path).parent.mkdir(parents=True)
    record_path(tmp_path).write_text(json.dumps(raw))

    with pytest.raises(ValueError, match="must contain string pairs"):
        load_record(tmp_path)


def test_activation_record_v3_rejects_unknown_evidence_fields(tmp_path: Path) -> None:
    record = ActivationRecord.start(operation_id="op-1", origin="cli")
    raw = json.loads(json.dumps(record.__dict__))
    raw["wal_exact_evidence"] = {
        "timeline": "1",
        "segment": "000000010000000000000001",
        "switch_lsn": "0/1",
        "failed_count": "0",
        "approximate": "true",
    }
    record_path(tmp_path).parent.mkdir(parents=True)
    record_path(tmp_path).write_text(json.dumps(raw))

    with pytest.raises(ValueError, match="wal_exact_evidence fields differ"):
        load_record(tmp_path)


def test_activation_record_v3_rejects_unknown_config_journal_kind() -> None:
    record = (
        ActivationRecord.start(operation_id="op-1", origin="cli")
        .advance(
            "snapshot_pending",
            pre_activation_pg_settings={"archive_mode": "off"},
            pre_activation_credential_evidence=_credentials(),
        )
        .advance("snapshot_verified", pre_activation_snapshot="/backup.enc")
        .advance("wal_config_pending")
        .advance(
            "wal_config_applying",
            wal_config_before_digest="before",
            wal_config_desired_digest="desired",
            pre_activation_pitr_env={"pitr_enabled": "[]"},
            pre_activation_pg_auto_conf={"archive_mode": "__ABSENT__"},
            pre_activation_env_b64="YQ==",
            pre_activation_env_digest="env",
            pre_activation_auto_conf_b64="Yg==",
            pre_activation_auto_conf_digest="auto",
        )
    )
    raw = record.__dict__ | {"config_apply_intent": {"kind": "future"}}
    with pytest.raises(ValueError, match="kind is unknown"):
        ActivationRecord.from_json(json.dumps(raw))


def test_activation_record_v3_rejects_non_sha256_backup_key_fingerprint() -> None:
    raw = ActivationRecord.start(operation_id="op-1", origin="cli").__dict__ | {
        "pre_activation_credential_evidence": _credentials() | {"backup_key_sha256": "bad"}
    }
    with pytest.raises(ValueError, match="fingerprint"):
        ActivationRecord.from_json(json.dumps(raw))


def test_activation_record_v3_requires_utc_verification_deadline() -> None:
    record = ActivationRecord.start(operation_id="op-1", origin="cli")
    raw = record.__dict__ | {"wal_verification_deadline": "2026-08-29T10:00:00+08:00"}

    with pytest.raises(ValueError, match="must be a UTC timestamp"):
        ActivationRecord.from_json(json.dumps(raw))


def test_activation_record_rejects_phase_jump_backtrack_and_terminal_advance() -> None:
    record = ActivationRecord.start(operation_id="op-1", origin="cli")
    with pytest.raises(ValueError, match="illegal PITR activation phase transition"):
        record.advance("snapshot_verified")

    pending = record.advance(
        "snapshot_pending",
        pre_activation_pg_settings={"archive_mode": "off"},
        pre_activation_credential_evidence=_credentials(),
    )
    with pytest.raises(ValueError, match="illegal PITR activation phase transition"):
        pending.advance("shadow")
    with pytest.raises(ValueError, match="rollback entry"):
        pending.advance("rollback_pending")


def test_activation_record_allows_same_phase_error_update_only() -> None:
    record = ActivationRecord.start(operation_id="op-1", origin="cli")
    failed = record.advance("shadow", error="interrupted")
    assert failed.error == "interrupted"
    assert failed.started_at == record.started_at

    with pytest.raises(ValueError, match="may only change diagnostics"):
        failed.advance("shadow", origin="other")

    with pytest.raises(ValueError, match="identity fields are immutable"):
        failed.advance("snapshot_pending", started_at="2026-08-29T00:00:00+00:00")


def test_v2_wal_config_pending_upgrades_strictly_and_can_roll_back(tmp_path: Path) -> None:
    v2 = {
        "schema_version": 2,
        "operation_id": "op-1",
        "phase": "wal_config_pending",
        "started_at": "2026-08-29T00:00:00+00:00",
        "updated_at": "2026-08-29T00:01:00+00:00",
        "origin": "cli",
        "pre_activation_snapshot": "/verified.dump.enc",
        "pre_activation_pg_settings": {"archive_mode": "off"},
        "pre_activation_credential_evidence": _credentials(),
        "switched_wal": None,
        "protected_manifest": None,
        "error": None,
    }
    record_path(tmp_path).parent.mkdir(parents=True)
    record_path(tmp_path).write_text(json.dumps(v2))
    upgraded = load_record(tmp_path)
    assert upgraded is not None and upgraded.schema_version == 4
    rolled_back = mark_pre_mutation_rolled_back(tmp_path, upgraded)
    assert rolled_back.phase == "rolled_back"


def test_v2_post_mutation_phase_refuses_unsafe_upgrade() -> None:
    record = ActivationRecord.start(operation_id="op-1", origin="cli")
    raw = {
        name: value
        for name, value in record.__dict__.items()
        if name
        in {
            "schema_version",
            "operation_id",
            "phase",
            "started_at",
            "updated_at",
            "origin",
            "pre_activation_snapshot",
            "pre_activation_pg_settings",
            "pre_activation_credential_evidence",
            "switched_wal",
            "protected_manifest",
            "error",
        }
    }
    raw.update(schema_version=2, phase="wal_restart_pending")
    with pytest.raises(ValueError, match="cannot be safely upgraded"):
        ActivationRecord.from_json(json.dumps(raw))


def test_v3_record_upgrades_to_v4_with_null_error_message(tmp_path: Path) -> None:
    """The 2026-08-30 base_pending operation on disk was written by schema v3;
    the v4 bump adds only the optional error_message field, so any phase must
    load in place (the activation stays resumable across the upgrade)."""
    from dataclasses import asdict

    record = ActivationRecord.start(operation_id="op-1", origin="cli")
    raw = asdict(record)
    assert "error_message" in raw
    raw.pop("error_message")
    raw["schema_version"] = 3
    path = record_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw))
    loaded = load_record(tmp_path)
    assert loaded is not None
    assert loaded.schema_version == 4
    assert loaded.operation_id == "op-1"
    assert loaded.error_message is None


def test_same_phase_update_may_set_error_message(tmp_path: Path) -> None:
    record = ActivationRecord.start(operation_id="op-1", origin="cli")
    advanced = record.advance(
        record.phase,
        error="BaseCandidateError",
        error_message="pg_basebackup exited 1: FATAL no pg_hba.conf entry",
    )
    assert advanced.error_message == "pg_basebackup exited 1: FATAL no pg_hba.conf entry"
