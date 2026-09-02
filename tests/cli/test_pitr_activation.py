# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from cli.commands import _pitr_activation as activation
from cli.commands import _pitr_activation_config as activation_config
from ops.pitr_restart import PitrRestartContinuation
from services.pitr import activation_runtime
from services.pitr.activation_observability import refusal_message, save_error
from services.pitr.activation_runtime import (
    _restore_exact_file,
    archiver_reached_target,
    pitr_env_is_desired,
    rollback_effect_state,
    wal_metadata,
)
from services.pitr.activation_state import ActivationRecord, load_record, write_record
from services.pitr.base_candidate import BaseCandidateError
from services.pitr.checksums import ObjectChecksum
from services.pitr.object_store import RemoteObjectAck
from services.pitr.uploader import ack_manifest_from_raw
from shared.config import FIELD_INFOS, field_alias, field_domain, settings
from tests._pitr_fixtures import baidu_credential_evidence


def _credentials() -> dict[str, str]:
    return {
        "backend": "gcs",
        "uploader_identity": "u@example.test",
        "viewer_identity": "v@example.test",
        "store_target": "bucket",
        "object_prefix": "pitr",
        "backup_key_id": "key",
        "backup_key_sha256": "0" * 64,
    }


def test_validate_secrets_produces_baidu_evidence_for_the_baidu_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.config import settings

    credentials = tmp_path / "baidu.json"
    credentials.write_text(
        json.dumps({"app_key": "app-key", "secret_key": "secret", "refresh_token": "refresh"})
    )
    credentials.chmod(0o600)
    monkeypatch.setattr(settings.physical_backup, "pitr_store_backend", "baidu")
    monkeypatch.setattr(settings.physical_backup, "pitr_backup_key_file", tmp_path / "backup.key")
    (tmp_path / "backup.key").write_bytes(b"0" * 32)
    (tmp_path / "backup.key").chmod(0o600)
    monkeypatch.setattr(settings.physical_backup, "pitr_baidu_credentials_file", credentials)
    monkeypatch.setattr(settings.physical_backup, "pitr_baidu_token_file", tmp_path / "token.json")
    monkeypatch.setattr(settings.physical_backup, "pitr_baidu_app_root", "/apps/ava/ava-pitr")

    def probe(_path: Path) -> str:
        return "/apps/ava/ava-pitr"

    monkeypatch.setattr(activation, "probe_baidu_read_access", probe)

    evidence = activation._validate_secrets()

    assert evidence == {
        "backend": "baidu",
        "uploader_identity": "app-key",
        "viewer_identity": "app-key",
        "store_target": "/apps/ava/ava-pitr",
        "object_prefix": settings.physical_backup.pitr_gcs_prefix,
        "backup_key_id": settings.physical_backup.pitr_backup_key_id,
        "backup_key_sha256": hashlib.sha256(b"0" * 32).hexdigest(),
    }


def test_validate_secrets_produces_cos_evidence_for_the_cos_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.config import settings

    credentials = tmp_path / "cos.json"
    credentials.write_text(json.dumps({"secret_id": "AKIDcos", "secret_key": "secret-key"}))
    credentials.chmod(0o600)
    monkeypatch.setattr(settings.physical_backup, "pitr_store_backend", "cos")
    monkeypatch.setattr(settings.physical_backup, "pitr_backup_key_file", tmp_path / "backup.key")
    (tmp_path / "backup.key").write_bytes(b"0" * 32)
    (tmp_path / "backup.key").chmod(0o600)
    monkeypatch.setattr(settings.physical_backup, "pitr_cos_credentials_file", credentials)
    monkeypatch.setattr(settings.physical_backup, "pitr_cos_bucket", "ava-pitr-1250000000")
    monkeypatch.setattr(settings.physical_backup, "pitr_cos_region", "ap-guangzhou")

    evidence = activation._validate_secrets()

    assert evidence == {
        "backend": "cos",
        "uploader_identity": "AKIDcos",
        "viewer_identity": "AKIDcos",
        "store_target": "ava-pitr-1250000000",
        "object_prefix": settings.physical_backup.pitr_gcs_prefix,
        "backup_key_id": settings.physical_backup.pitr_backup_key_id,
        "backup_key_sha256": hashlib.sha256(b"0" * 32).hexdigest(),
    }


def test_validate_secrets_fails_closed_for_unknown_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.config import settings

    monkeypatch.setattr(settings.physical_backup, "pitr_store_backend", "nope")
    monkeypatch.setattr(settings.physical_backup, "pitr_backup_key_file", tmp_path / "backup.key")
    (tmp_path / "backup.key").write_bytes(b"0" * 32)
    (tmp_path / "backup.key").chmod(0o600)
    with pytest.raises(RuntimeError, match="unhandled PITR store backend"):
        activation._validate_secrets()


def _mock_activation_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    digest = "0" * 64
    monkeypatch.setattr(activation_config, "_alter", lambda _name, _value: None)
    monkeypatch.setattr(activation_config, "_archive_value", lambda _name: "off")
    monkeypatch.setattr(activation_config, "_enable_pitr_services", lambda _digest: b"a")
    monkeypatch.setattr(activation_config, "_env_payload", lambda _home: b"a")
    monkeypatch.setattr(activation_config, "pitr_env_is_desired", lambda _payload: False)
    monkeypatch.setattr(activation_config, "_file_evidence", lambda _path: ("YQ==", digest))
    monkeypatch.setattr(activation, "_file_evidence", lambda _path: ("YQ==", digest))
    monkeypatch.setattr(
        activation,
        "capture_pitr_env_baseline",
        lambda _path: (
            "YQ==",
            digest,
            dict.fromkeys(activation._PITR_ENV_FIELDS, "[]"),
        ),
    )
    monkeypatch.setattr(
        activation,
        "_pg_auto_conf_baseline",
        lambda _home, _values=None: {
            "archive_mode": "__ABSENT__",
            "archive_command": "__ABSENT__",
            "archive_timeout": "__ABSENT__",
            "wal_compression": "__ABSENT__",
        },
    )

    def spawn_restart(*_args: object, **kwargs: object) -> dict[str, str]:
        binder = kwargs["bind_continuation"]
        assert callable(binder)
        binder()
        return {
            "session": activation._restart_session(),
            "log": "/tmp/restart.log",  # noqa: S108
        }

    monkeypatch.setattr("ops.cluster_deploy.spawn_restart", spawn_restart)


def _wal_config_pending_record(credentials: dict[str, str] | None = None) -> ActivationRecord:
    return (
        ActivationRecord.start(operation_id="op-1", origin="cli")
        .advance(
            "snapshot_pending",
            pre_activation_pg_settings={
                "archive_mode": "off",
                "archive_command": "",
                "archive_timeout": "0",
                "wal_compression": "off",
                "postmaster_started_at": "2026-08-29 00:00:00+00",
            },
            pre_activation_credential_evidence=credentials or _credentials(),
        )
        .advance("snapshot_verified", pre_activation_snapshot="/verified.dump.enc")
        .advance("wal_config_pending")
    )


def _wal_restart_pending_record(credentials: dict[str, str] | None = None) -> ActivationRecord:
    return (
        _wal_config_pending_record(credentials)
        .advance(
            "wal_config_applying",
            wal_config_before_digest="before",
            wal_config_desired_digest="desired",
            pre_activation_pitr_env=dict.fromkeys(activation._PITR_ENV_FIELDS, "[]"),
            pre_activation_pg_auto_conf={
                "archive_mode": "__ABSENT__",
                "archive_command": "__ABSENT__",
                "archive_timeout": "__ABSENT__",
                "wal_compression": "__ABSENT__",
            },
            pre_activation_env_b64="YQ==",
            pre_activation_env_digest="env-digest",
            pre_activation_auto_conf_b64="Yg==",
            pre_activation_auto_conf_digest="auto-digest",
        )
        .advance(
            "wal_restart_pending",
            restart_handoff="handoff-1",
            restart_orchestration="orchestration-1",
            rollback_expected_env_digest="owned-env-digest",
            rollback_expected_auto_conf_digest="owned-auto-digest",
        )
    )


def test_typed_restart_handoff_binds_inside_spawn_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = _wal_restart_pending_record()
    write_record(tmp_path, record)
    observed: list[tuple[str, str, str | None, str]] = []

    def spawn(origin: str, **kwargs: object) -> dict[str, str]:
        continuation = kwargs["continuation"]
        assert isinstance(continuation, PitrRestartContinuation)
        binder = kwargs["bind_continuation"]
        assert callable(binder)
        binder()
        bound = load_record(tmp_path)
        assert bound is not None and bound.restart_handoff_consumed_at is not None
        observed.append(
            (
                origin,
                continuation.expected_phase,
                continuation.expected_digest,
                continuation.resume_origin(),
            )
        )
        return {
            "session": activation._restart_session(),
            "log": "/tmp/restart.log",  # noqa: S108
        }

    monkeypatch.setattr("ops.cluster_deploy.spawn_restart", spawn)
    replacement = activation._dispatch_restart_handoff(tmp_path, record)
    assert replacement.restart_handoff_consumed_at is not None
    assert replacement.restart_dispatch_session == activation._restart_session()
    assert observed == [
        (
            "pitr-activation:op-1:orchestration-1",
            "wal_restart_pending",
            "desired",
            "restart-continuation:op-1:orchestration-1:handoff-1:wal_restart_pending:desired",
        )
    ]

    # A crash after durable binding but before the detached child exists retries
    # the same deterministic orchestration session.  The lifecycle-locked binder
    # rearms and consumes the same token instead of inventing a second owner.
    retried = activation._dispatch_restart_handoff(tmp_path, replacement)
    assert retried.restart_dispatch_session == activation._restart_session()
    assert load_record(tmp_path) == retried


def test_restart_handoff_retries_same_session_after_post_bind_spawn_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = _wal_restart_pending_record()
    write_record(tmp_path, record)
    calls = 0

    def spawn(_origin: str, **kwargs: object) -> dict[str, str]:
        nonlocal calls
        calls += 1
        binder = kwargs["bind_continuation"]
        assert callable(binder)
        binder()
        if calls == 1:
            raise RuntimeError("detached session declined after durable bind")
        return {
            "session": activation._restart_session(),
            "log": "/tmp/restart.log",  # noqa: S108
        }

    monkeypatch.setattr("ops.cluster_deploy.spawn_restart", spawn)
    with pytest.raises(RuntimeError, match="declined"):
        activation._dispatch_restart_handoff(tmp_path, record)
    bound = load_record(tmp_path)
    assert bound is not None
    assert bound.restart_dispatch_session == activation._restart_session()

    retried = activation._dispatch_restart_handoff(tmp_path, bound)
    assert calls == 2
    assert retried.restart_handoff == record.restart_handoff
    assert retried.restart_dispatch_session == activation._restart_session()


def test_exact_file_rollback_is_digest_cas_and_crash_idempotent(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    original = b"A='raw'\nA=duplicate # exact\n"
    activated = b"A=true\n"
    path.write_bytes(activated)
    payload_b64 = base64.b64encode(original).decode("ascii")
    target = hashlib.sha256(original).hexdigest()
    expected = hashlib.sha256(activated).hexdigest()

    _restore_exact_file(
        path, payload_b64=payload_b64, target_digest=target, expected_digest=expected
    )
    _restore_exact_file(
        path, payload_b64=payload_b64, target_digest=target, expected_digest=expected
    )
    assert path.read_bytes() == original

    path.write_bytes(b"concurrent=true\n")
    with pytest.raises(RuntimeError, match="changed concurrently"):
        _restore_exact_file(
            path, payload_b64=payload_b64, target_digest=target, expected_digest=expected
        )


def test_pre_activation_env_evidence_and_baseline_share_one_exact_capture(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".env"
    payload = b"AVA_PITR_ENABLED='false'\nAVA_PITR_ENABLED=true # duplicate\nOTHER=x\n"
    path.write_bytes(payload)

    encoded, digest, baseline = activation.capture_pitr_env_baseline(path)

    assert base64.b64decode(encoded) == payload
    assert digest == hashlib.sha256(payload).hexdigest()
    assert json.loads(baseline["pitr_enabled"]) == [
        "AVA_PITR_ENABLED='false'",
        "AVA_PITR_ENABLED=true # duplicate",
    ]


def test_archiver_target_is_timeline_aware_and_allows_later_success() -> None:
    target = "00000001000000000000000A"
    assert archiver_reached_target(
        last_archived="00000001000000000000000B", timeline="1", target=target
    )
    assert not archiver_reached_target(
        last_archived="00000002000000000000000B", timeline="1", target=target
    )
    assert not archiver_reached_target(
        last_archived="000000010000000000000009", timeline="1", target=target
    )


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
        "postmaster_started_at": "2026-08-29 00:00:00+00",
    }
    credentials = _credentials()
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
    _mock_activation_mutation(monkeypatch)

    assert activation.cmd_pitr_activate(origin="agent:405") == 0
    record = load_record(tmp_path)
    assert record is not None
    assert record.phase == "wal_restart_pending"
    assert record.pre_activation_snapshot == str(snapshot)
    assert record.pre_activation_pg_settings == pg
    assert record.pre_activation_credential_evidence == credentials


def test_activate_resume_never_repeats_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_record(
        tmp_path,
        _wal_config_pending_record(),
    )
    monkeypatch.setattr(activation, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(activation, "_validate_snapshot", lambda _record: None)
    pg = {
        **(_wal_config_pending_record().pre_activation_pg_settings or {}),
        "archive_command": "",
        "archive_timeout": "0",
        "wal_compression": "off",
        "postmaster_started_at": "2026-08-29 00:00:00+00",
    }
    monkeypatch.setattr(activation, "_read_pg_state", lambda: pg)
    monkeypatch.setattr(
        activation,
        "_validate_secrets",
        lambda: dict(_wal_config_pending_record().pre_activation_credential_evidence or {}),
    )
    monkeypatch.setattr("shared.cluster_lock.acquire_update_lock", lambda *_a, **_kw: True)
    monkeypatch.setattr("shared.cluster_lock.release_update_lock", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        "cli.commands._update_git.snapshot_pre_activation_data",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("snapshot repeated")),
    )
    _mock_activation_mutation(monkeypatch)

    assert activation.cmd_pitr_activate(origin="cli") == 0


@pytest.mark.parametrize("drift", ["system_identifier", "bucket_name", "backup_key_id"])
def test_wal_config_pending_drift_refuses_before_any_config_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, drift: str
) -> None:
    record = _wal_config_pending_record()
    write_record(tmp_path, record)
    pg = dict(record.pre_activation_pg_settings or {})
    credentials = dict(record.pre_activation_credential_evidence or {})
    if drift == "system_identifier":
        pg[drift] = "replacement-cluster"
    else:
        credentials[drift] = "replacement-target"
    called = False

    def mutate(*_args: object, **_kwargs: object) -> ActivationRecord:
        nonlocal called
        called = True
        raise AssertionError("config mutation ran after frozen identity drift")

    monkeypatch.setattr(activation, "_read_pg_state", lambda: pg)
    monkeypatch.setattr(activation, "_validate_secrets", lambda: credentials)
    monkeypatch.setattr(activation, "_validate_snapshot", lambda _record: None)
    monkeypatch.setattr(activation, "apply_wal_config", mutate)

    with pytest.raises(RuntimeError, match=r"changed before (wal_config_pending|PITR mutation)"):
        activation._advance_activation(tmp_path, record, "holder")
    assert called is False
    assert load_record(tmp_path) == record


def test_config_apply_journals_intent_before_alter_and_resumes_partial_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "pg").mkdir()
    (tmp_path / "pg" / "postgresql.auto.conf").write_text("")
    (tmp_path / ".env").write_text("OTHER=kept\n")
    record = _wal_config_pending_record().advance(
        "wal_config_applying",
        wal_config_before_digest="before",
        wal_config_desired_digest="desired",
        pre_activation_pitr_env=dict.fromkeys(activation._PITR_ENV_FIELDS, "[]"),
        pre_activation_pg_auto_conf={"archive_mode": "__ABSENT__"},
        pre_activation_env_b64="T1RIRVI9a2VwdAo=",
        pre_activation_env_digest=hashlib.sha256(b"OTHER=kept\n").hexdigest(),
        pre_activation_auto_conf_b64="",
        pre_activation_auto_conf_digest=hashlib.sha256(b"").hexdigest(),
        rollback_expected_env_digest=hashlib.sha256(b"OTHER=kept\n").hexdigest(),
        rollback_expected_auto_conf_digest=hashlib.sha256(b"").hexdigest(),
    )
    write_record(tmp_path, record)

    def crash_after_intent(_name: str, _value: str) -> None:
        durable = load_record(tmp_path)
        assert durable is not None and durable.config_apply_intent is not None
        raise RuntimeError("crash after intent")

    monkeypatch.setattr(activation_config, "_archive_value", lambda _name: "off")
    monkeypatch.setattr(activation_config, "_alter", crash_after_intent)
    with pytest.raises(RuntimeError, match="crash after intent"):
        activation_config.apply_wal_config(tmp_path, record, {"archive_mode": "on"})
    durable = load_record(tmp_path)
    assert durable is not None
    assert durable.phase == "wal_config_applying"
    assert durable.config_apply_intent == {
        "kind": "postgresql_auto_conf",
        "name": "archive_mode",
        "expected_digest": hashlib.sha256(b"").hexdigest(),
        "desired_value": "on",
    }


def test_rollback_preserves_snapshot_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = _wal_config_pending_record()
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


@pytest.mark.parametrize(
    "crash_setting",
    ["archive_mode", "archive_command", "archive_timeout", "wal_compression"],
)
@pytest.mark.parametrize("window", ["before_effect", "after_effect_before_journal"])
def test_rollback_setting_crash_matrix_resumes_each_owned_alter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    crash_setting: str,
    window: str,
) -> None:
    baseline = {
        "archive_mode": "__ABSENT__",
        "archive_command": "__ABSENT__",
        "archive_timeout": "__ABSENT__",
        "wal_compression": "__ABSENT__",
    }
    current = dict.fromkeys(baseline, "activation-owned")
    record = _wal_restart_pending_record().advance(
        "rollback_pending",
        wal_config_before_digest="before",
        restart_handoff="rollback-handoff",
        restart_orchestration="rollback-orchestration",
        rollback_postmaster_started_at="2026-08-29 01:00:00+00",
    )
    write_record(tmp_path, record)
    crashed = False

    monkeypatch.setattr(
        activation_config, "_persistent_archive_settings", lambda _home: dict(current)
    )
    monkeypatch.setattr(
        activation_config,
        "_file_evidence",
        lambda _path: (
            "",
            hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest(),
        ),
    )

    def alter(name: str, desired: str) -> None:
        nonlocal crashed
        if name == crash_setting and not crashed:
            crashed = True
            if window == "after_effect_before_journal":
                current[name] = desired
            raise RuntimeError(window)
        current[name] = desired

    monkeypatch.setattr(activation_config, "_alter_restore", alter)
    with pytest.raises(RuntimeError, match=window):
        activation_config.restore_archive_settings(tmp_path, record, baseline)

    durable = load_record(tmp_path)
    assert durable is not None
    intent = durable.rollback_setting_intent
    assert intent is not None
    assert durable.rollback_setting_intent == {
        "name": crash_setting,
        "expected_digest": intent["expected_digest"],
        "current_value": "activation-owned",
        "desired_value": "__ABSENT__",
    }
    resumed = activation_config.restore_archive_settings(tmp_path, durable, baseline)
    assert resumed.rollback_setting_intent is None
    assert set(resumed.rollback_settings_applied or {}) == set(baseline)
    assert current == baseline


def test_rollback_leaves_config_owned_env_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The four PITR gate keys are config-owned since the 2026-08-31
    rework: rolling an activation back must never strip them (that used to
    stop every PITR service mid-rollback)."""
    record = _wal_restart_pending_record().advance(
        "rollback_pending",
        restart_handoff="rollback-handoff",
        restart_orchestration="rollback-orchestration",
        rollback_postmaster_started_at="2026-08-31 12:00:00+00",
        rollback_expected_auto_conf_digest="a" * 64,
        pre_activation_auto_conf_digest="a" * 64,
    )
    write_record(tmp_path, record)
    (tmp_path / "pg").mkdir()
    (tmp_path / "pg" / "postgresql.auto.conf").write_text("")
    (tmp_path / ".env").write_text(
        "AVA_PITR_ENABLED=true\n"
        "AVA_PITR_BASE_BACKUP_ENABLED=true\n"
        "AVA_PITR_RESTORE_PROOF_ENABLED=true\n"
        "AVA_PITR_RETENTION_PLANNER_ENABLED=false\n"
    )
    monkeypatch.setattr(activation, "ava_home", lambda: tmp_path)
    monkeypatch.setattr("shared.cluster_lock.acquire_update_lock", lambda *_a, **_kw: True)
    monkeypatch.setattr("shared.cluster_lock.release_update_lock", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        activation_config,
        "_persistent_archive_settings",
        lambda _home: {
            "archive_mode": "__ABSENT__",
            "archive_command": "__ABSENT__",
            "archive_timeout": "__ABSENT__",
            "wal_compression": "__ABSENT__",
        },
    )
    monkeypatch.setattr(
        activation_config,
        "_file_evidence",
        lambda _path: ("", "a" * 64),
    )
    monkeypatch.setattr(
        activation,
        "_read_pg_state",
        lambda: {
            "archive_mode": "on",
            "archive_command": "shim",
            "archive_timeout": "60min",
            "wal_compression": "pglz",
            "postmaster_started_at": "2026-08-31 12:00:00+00",
        },
    )
    monkeypatch.setattr(activation, "_file_evidence", lambda _path: ("", "a" * 64))

    def spawn_restart(*_args: object, **kwargs: object) -> dict[str, str]:
        binder = kwargs["bind_continuation"]
        assert callable(binder)
        binder()
        return {"session": activation._restart_session(), "log": "/tmp/restart.log"}  # noqa: S108

    monkeypatch.setattr("ops.cluster_deploy.spawn_restart", spawn_restart)

    assert activation.cmd_pitr_rollback() == 0
    lines = (tmp_path / ".env").read_text().splitlines()
    assert lines == [
        "AVA_PITR_ENABLED=true",
        "AVA_PITR_BASE_BACKUP_ENABLED=true",
        "AVA_PITR_RESTORE_PROOF_ENABLED=true",
        "AVA_PITR_RETENTION_PLANNER_ENABLED=false",
    ]


def test_pitr_env_desired_requires_all_four_owned_aliases() -> None:
    assert pitr_env_is_desired(
        b"AVA_PITR_ENABLED=true\n"
        b"AVA_PITR_BASE_BACKUP_ENABLED=true\n"
        b"AVA_PITR_RESTORE_PROOF_ENABLED=true\n"
        b"AVA_PITR_RETENTION_PLANNER_ENABLED=false\n"
    )
    assert not pitr_env_is_desired(b"AVA_PITR_ENABLED=true\n")


def test_pitr_env_absent_detects_all_four_missing() -> None:
    from services.pitr.activation_runtime import pitr_env_absent

    assert pitr_env_absent(b"OTHER=kept\n")
    assert pitr_env_absent(b"")
    assert not pitr_env_absent(b"AVA_PITR_ENABLED=true\n")


def _service_account(email: str, key_id: str) -> str:
    return json.dumps(
        {
            "type": "service_account",
            "client_email": email,
            "project_id": "test-project",
            "private_key_id": key_id,
        }
    )


def _valid_pitr_baseline(tmp_path: Path) -> str:
    """Return file-backed GCS prerequisites for the real activation write path."""
    key = tmp_path / "backup.key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    uploader = tmp_path / "gcs-uploader.json"
    uploader.write_text(_service_account("uploader@example.com", "uploader-key"))
    uploader.chmod(0o600)
    viewer = tmp_path / "gcs-viewer.json"
    viewer.write_text(_service_account("viewer@example.com", "viewer-key"))
    viewer.chmod(0o600)
    return (
        "AVA_PITR_STORE_BACKEND=gcs\n"
        f"AVA_PITR_BACKUP_KEY_FILE={key}\n"
        "AVA_PITR_BACKUP_KEY_ID=test\n"
        "AVA_PITR_REPLICATION_DB_URL=postgresql://repl@127.0.0.1:1/x\n"
        "AVA_PITR_GCS_PROJECT=test-project\n"
        "AVA_PITR_GCS_BUCKET=test-bucket\n"
        f"AVA_PITR_GCS_CREDENTIALS_FILE={uploader}\n"
        f"AVA_PITR_RESTORE_GCS_CREDENTIALS_FILE={viewer}\n"
    )


def _clear_pitr_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real candidate check independent of inherited PITR settings."""
    for name in FIELD_INFOS:
        if field_domain(name) == "physical_backup":
            monkeypatch.delenv(field_alias(name), raising=False)


def _env_apply_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    env_text: str,
    *,
    valid_pitr_baseline: bool = False,
) -> ActivationRecord:
    _clear_pitr_environment(monkeypatch)
    if valid_pitr_baseline:
        env_text += _valid_pitr_baseline(tmp_path)
    (tmp_path / "pg").mkdir()
    (tmp_path / "pg" / "postgresql.auto.conf").write_text("")
    (tmp_path / ".env").write_text(env_text)
    record = _wal_config_pending_record().advance(
        "wal_config_applying",
        wal_config_before_digest="before",
        wal_config_desired_digest="desired",
        pre_activation_pitr_env=dict.fromkeys(activation._PITR_ENV_FIELDS, "[]"),
        pre_activation_pg_auto_conf={
            "archive_mode": "__ABSENT__",
            "archive_command": "__ABSENT__",
            "archive_timeout": "__ABSENT__",
            "wal_compression": "__ABSENT__",
        },
        pre_activation_env_b64="YQ==",
        pre_activation_env_digest="env-digest",
        pre_activation_auto_conf_b64="Yg==",
        pre_activation_auto_conf_digest="auto-digest",
    )
    write_record(tmp_path, record)
    monkeypatch.setattr(activation_config, "_archive_value", lambda _name: "on")
    monkeypatch.setattr(activation_config, "_alter", lambda _name, _value: None)
    monkeypatch.setattr(activation_config, "_file_evidence", lambda _path: ("YQ==", "0" * 64))
    monkeypatch.setattr(activation_config, "_settings_digest", lambda _values: "0" * 64)
    return record


def test_env_apply_refuses_config_owned_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Present-but-not-desired PITR keys are the operator's explicit config;
    the activation must refuse instead of clobbering them."""
    record = _env_apply_fixture(
        monkeypatch,
        tmp_path,
        "AVA_PITR_ENABLED=true\n"
        "AVA_PITR_BASE_BACKUP_ENABLED=true\n"
        "AVA_PITR_RESTORE_PROOF_ENABLED=true\n"
        "AVA_PITR_RETENTION_PLANNER_ENABLED=true\n",
    )
    with pytest.raises(RuntimeError, match="config-owned"):
        activation_config.apply_wal_config(tmp_path, record, {"archive_mode": "on"})
    assert "AVA_PITR_RETENTION_PLANNER_ENABLED=true" in (tmp_path / ".env").read_text()


def test_env_apply_refuses_partial_pitr_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = _env_apply_fixture(monkeypatch, tmp_path, "AVA_PITR_ENABLED=true\n")
    with pytest.raises(RuntimeError, match="config-owned"):
        activation_config.apply_wal_config(tmp_path, record, {"archive_mode": "on"})


def test_env_apply_provisions_only_when_all_four_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one-shot provisioning path survives: with no PITR keys in the env
    the activation writes the desired set and advances to wal_restart_pending."""
    called: list[str] = []

    def enable(digest: str) -> bytes:
        called.append(digest)
        return b"a"

    monkeypatch.setattr(activation_config, "_enable_pitr_services", enable)
    record = _env_apply_fixture(monkeypatch, tmp_path, "OTHER=kept\n")
    replacement = activation_config.apply_wal_config(tmp_path, record, {"archive_mode": "on"})
    assert called == [hashlib.sha256(b"OTHER=kept\n").hexdigest()]
    assert replacement.phase == "wal_restart_pending"
    assert replacement.config_apply_applied == {
        "kind": "env",
        "digest": hashlib.sha256(b"a").hexdigest(),
    }


def test_env_apply_resumes_after_provisioning_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """QA nit 1: the provisioning write crashes after the intent journal;
    the resume re-run finds the durable intent and completes the write."""
    calls: list[str] = []

    def crash_after_intent(digest: str) -> bytes:
        calls.append(digest)
        raise RuntimeError("crash during env write")

    monkeypatch.setattr(activation_config, "_enable_pitr_services", crash_after_intent)
    record = _env_apply_fixture(monkeypatch, tmp_path, "OTHER=kept\n")
    with pytest.raises(RuntimeError, match="crash during env write"):
        activation_config.apply_wal_config(tmp_path, record, {"archive_mode": "on"})
    durable = load_record(tmp_path)
    assert durable is not None
    assert durable.config_apply_intent is not None
    assert durable.config_apply_intent["kind"] == "env"

    monkeypatch.setattr(activation_config, "_enable_pitr_services", lambda _digest: b"a")
    replacement = activation_config.apply_wal_config(tmp_path, durable, {"archive_mode": "on"})
    assert calls == [hashlib.sha256(b"OTHER=kept\n").hexdigest()]
    assert replacement.phase == "wal_restart_pending"


def test_env_apply_noop_when_already_desired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """QA nit 2: a desired env is adopted untouched — no provisioning write,
    no intent journal, env bytes byte-identical after the apply."""

    def unexpected_write(_digest: str) -> bytes:
        raise AssertionError("provisioning write ran although the env was desired")

    monkeypatch.setattr(activation_config, "_enable_pitr_services", unexpected_write)
    desired = (
        "AVA_PITR_ENABLED=true\n"
        "AVA_PITR_BASE_BACKUP_ENABLED=true\n"
        "AVA_PITR_RESTORE_PROOF_ENABLED=true\n"
        "AVA_PITR_RETENTION_PLANNER_ENABLED=false\n"
    )
    record = _env_apply_fixture(monkeypatch, tmp_path, desired)
    replacement = activation_config.apply_wal_config(tmp_path, record, {"archive_mode": "on"})
    assert replacement.phase == "wal_restart_pending"
    assert replacement.config_apply_applied == {
        "kind": "env",
        "digest": hashlib.sha256(desired.encode()).hexdigest(),
    }
    assert (tmp_path / ".env").read_bytes() == desired.encode()


def test_env_apply_provisioning_round_trips_real_env_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """QA nit 3: the real write_fields path lands the four aliases in the
    env file and keeps the unrelated lines."""
    record = _env_apply_fixture(
        monkeypatch,
        tmp_path,
        "OTHER=kept\n",
        valid_pitr_baseline=True,
    )
    monkeypatch.setattr("shared.runtime_config.env_file_path", lambda: tmp_path / ".env")
    replacement = activation_config.apply_wal_config(tmp_path, record, {"archive_mode": "on"})
    assert replacement.phase == "wal_restart_pending"
    payload = (tmp_path / ".env").read_text()
    # write_fields quotes env values — dotenv reads them back unquoted,
    # which is what pitr_env_is_desired validates on the next apply.
    assert "AVA_PITR_ENABLED='true'" in payload
    assert "AVA_PITR_BASE_BACKUP_ENABLED='true'" in payload
    assert "AVA_PITR_RESTORE_PROOF_ENABLED='true'" in payload
    assert "AVA_PITR_RETENTION_PLANNER_ENABLED='false'" in payload
    assert "OTHER=kept" in payload


def test_rollback_effect_accepts_only_exact_pre_or_owned_postimage() -> None:
    assert rollback_effect_state(current="before", before="before", owned="after") is False
    assert rollback_effect_state(current="after", before="before", owned="after") is True
    with pytest.raises(RuntimeError, match="neither pre-effect nor exact owned post-effect"):
        rollback_effect_state(current="third-party", before="before", owned="after")


def test_pg_auto_conf_baseline_preserves_absence_and_last_owned_line(
    tmp_path: Path,
) -> None:
    pg = tmp_path / "pg"
    pg.mkdir()
    (pg / "postgresql.auto.conf").write_text(
        "archive_mode = 'off'\narchive_mode = 'on' # last owner\n"
    )
    effective = {
        "archive_mode": "on",
        "archive_command": "",
        "archive_timeout": "0",
        "wal_compression": "off",
    }
    assert activation._pg_auto_conf_baseline(tmp_path, effective) == {
        "archive_mode": "on",
        "archive_command": "__ABSENT__",
        "archive_timeout": "__ABSENT__",
        "wal_compression": "__ABSENT__",
    }


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


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (PermissionError("403 secret-token"), "gcs_forbidden"),
        (RuntimeError("WAL deadline https://credential.example"), "wal_deadline"),
        (RuntimeError("credential drift private-key"), "credential_drift"),
        (RuntimeError("state CAS changed"), "state_cas"),
        (RuntimeError("restore candidate mismatch"), "restore_mismatch"),
        (RuntimeError("restart session failed"), "restart_failure"),
        (
            BaseCandidateError(
                "pg_basebackup exited 1: FATAL: no pg_hba.conf entry for replication connection"
            ),
            "activation_failure",
        ),
    ],
)
def test_activation_error_diagnostics_are_stable_and_redacted(
    tmp_path: Path, failure: BaseException, code: str
) -> None:
    record = ActivationRecord.start(operation_id="op-1", origin="cli")
    write_record(tmp_path, record)

    save_error(tmp_path, record, failure)

    saved = load_record(tmp_path)
    assert saved is not None
    assert saved.error_code == code
    assert saved.error_detail is not None
    assert "secret-token" not in saved.error_detail
    assert "credential.example" not in saved.error_detail
    assert "private-key" not in saved.error_detail
    # The candidate pipeline's own errors persist their secret-free message;
    # every other type stays type-only (its text may carry credentials).
    if isinstance(failure, BaseCandidateError):
        assert saved.error_message == str(failure)
    else:
        assert saved.error_message is None


def test_concurrent_rollback_persists_error_and_preserves_phase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = _wal_config_pending_record()
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


def test_shadow_pg_gate_accepts_pg17_disabled_archive_command() -> None:
    """PG17 always reports archive_command as '(disabled)' while archive_mode
    is off; the gate must accept that display, not only an empty string (the
    CI mocks previously hid the real PostgreSQL behavior, so activation
    failed at shadow on a pristine cluster)."""
    assert activation._shadow_pg_gate({"archive_mode": "off", "archive_command": "(disabled)"})
    assert activation._shadow_pg_gate({"archive_mode": "off", "archive_command": "  (disabled)  "})
    assert activation._shadow_pg_gate({"archive_mode": "off", "archive_command": ""})
    assert activation._shadow_pg_gate({"archive_mode": "off", "archive_command": "   "})
    assert not activation._shadow_pg_gate({"archive_mode": "on", "archive_command": "(disabled)"})
    assert not activation._shadow_pg_gate(
        {"archive_mode": "off", "archive_command": "cp %p /spool/%f"}
    )


def test_release_failure_does_not_roll_back_durable_phase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot = tmp_path / "verified.dump.enc"
    pg = {
        "archive_mode": "off",
        "archive_command": "",
        "archive_timeout": "0",
        "wal_compression": "off",
        "direct_db_url": "dbname=ava",
        "postmaster_started_at": "2026-08-29 00:00:00+00",
    }
    credentials = _credentials()
    monkeypatch.setattr(activation, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(
        activation,
        "_shadow_readiness",
        lambda: activation.ShadowReadiness(pg=pg, credentials=credentials),
    )
    _mock_activation_mutation(monkeypatch)
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
    assert record.phase == "wal_restart_pending"
    assert record.pre_activation_snapshot == str(snapshot)
    assert record.error == "RuntimeError"


def test_credential_evidence_changes_fail_independently_of_pg_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pg = {"archive_mode": "off", "direct_db_url": "dbname=ava"}
    record = ActivationRecord.start(operation_id="op-1", origin="cli").advance(
        "snapshot_pending",
        pre_activation_pg_settings=pg,
        pre_activation_credential_evidence=_credentials(),
    )
    write_record(tmp_path, record)
    monkeypatch.setattr(activation, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(activation, "_read_pg_state", lambda: pg)
    monkeypatch.setattr(
        activation,
        "_validate_secrets",
        lambda: {**_credentials(), "viewer_identity": "new@example.test"},
    )
    monkeypatch.setattr("shared.cluster_lock.acquire_update_lock", lambda *_a, **_kw: True)
    monkeypatch.setattr("shared.cluster_lock.release_update_lock", lambda *_a, **_kw: None)

    assert activation.cmd_pitr_activate(origin="cli") == 1
    failed = load_record(tmp_path)
    assert failed is not None
    assert failed.phase == "snapshot_pending"
    assert failed.pre_activation_pg_settings == pg
    assert failed.error == "RuntimeError"


def test_alter_system_accepts_only_literal_values_on_real_pg17(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ALTER SYSTEM rejects bound parameters on real PostgreSQL 17, so _alter
    and _alter_restore must interpolate the value as a quoted literal. The CI
    mocks previously hid this: the 2026-08-30 activation failed at
    wal_config_applying with a psycopg SyntaxError on `$1`."""
    import shutil
    import subprocess
    import tempfile
    from types import SimpleNamespace

    from shared.pg_tools import pg_tool

    port = 39617
    # Short socket root: the default pytest tmp_path on macOS exceeds
    # PostgreSQL's 103-byte unix-socket path limit (QA #1076 nit 7 class).
    sock = Path(tempfile.mkdtemp(prefix="ava-pg-sock-", dir="/tmp"))
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
            f"-p {port} -c listen_addresses='' -c unix_socket_directories={sock} "
            "-c fsync=off -c full_page_writes=off -c synchronous_commit=off",
        ],
        check=True,
        capture_output=True,
    )
    try:
        monkeypatch.setattr(activation_config, "ava_home", lambda: tmp_path)
        monkeypatch.setattr(
            activation_config,
            "get_record",
            lambda _home: SimpleNamespace(ports={"postgres": port}, db_name="ava"),
        )
        monkeypatch.setattr(
            activation_config,
            "pg_admin_url",
            lambda _pg_port: f"postgresql://ava@/postgres?host={sock}&port={port}",
        )

        value = "cp %p /spool/%f --hard-bytes 123"
        activation_config._alter("archive_command", value)
        activation_config._alter("archive_timeout", "300")
        # ALTER SYSTEM only writes postgresql.auto.conf — the running server's
        # SHOW values change on reload/restart — so the file-level view read
        # through pg_file_settings is the ground truth here.
        persistent = activation_config._persistent_archive_settings(tmp_path)
        assert persistent["archive_command"] == value
        assert persistent["archive_timeout"] == "300"
        assert persistent["archive_mode"] == "__ABSENT__"

        activation_config._alter_restore("archive_command", "__ABSENT__")
        activation_config._alter_restore("archive_timeout", "__ABSENT__")
        assert activation_config._persistent_archive_settings(tmp_path) == {
            "archive_mode": "__ABSENT__",
            "archive_command": "__ABSENT__",
            "archive_timeout": "__ABSENT__",
            "wal_compression": "__ABSENT__",
        }
    finally:
        subprocess.run(  # noqa: S603
            [pg_tool("pg_ctl"), "-D", str(data), "stop", "-m", "fast"],
            check=False,
            capture_output=True,
        )
        shutil.rmtree(sock, ignore_errors=True)


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
    import shutil
    import subprocess
    import tempfile
    from types import SimpleNamespace

    from shared.pg_tools import pg_tool

    # TCP-free and isolated by this test's private socket directory. Releasing
    # an ephemeral TCP socket before pg_ctl binds it races every xdist worker.
    port = 39613
    # Short socket root: the default pytest tmp_path on macOS exceeds
    # PostgreSQL's 103-byte unix-socket path limit (QA #1076 nit 7 class).
    sock = Path(tempfile.mkdtemp(prefix="ava-pg-sock-", dir="/tmp"))
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
            f"-p {port} -c listen_addresses='' -c unix_socket_directories={sock} "
            "-c fsync=off -c full_page_writes=off -c synchronous_commit=off",
        ],
        check=True,
        capture_output=True,
    )
    try:
        subprocess.run(  # noqa: S603
            [pg_tool("createdb"), "-h", str(sock), "-p", str(port), "-U", "ava", "ava"],
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
            lambda _pg_port: f"postgresql://ava@/postgres?host={sock}&port={port}",
        )

        frozen = activation._read_pg_state()
        # Real PG17 masks archive_command as '(disabled)' while mode is off —
        # the shadow gate must accept exactly this display.
        assert frozen["archive_command"] == "(disabled)"
        # The frozen face round-trips: unchanged cluster -> comparison passes.
        activation._require_same_pg_state(frozen, "contract")
        # Reading twice yields the identical 12-key face (stable, no drift).
        assert activation._read_pg_state() == frozen
        # Old defect shape: any extra credential-evidence key must fail.
        with pytest.raises(RuntimeError, match="changed"):
            activation._require_same_pg_state(
                {**frozen, "uploader_identity": "writer@example.test"}, "contract"
            )
    finally:
        subprocess.run(  # noqa: S603
            [pg_tool("pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
            check=False,
            capture_output=True,
        )
        shutil.rmtree(sock, ignore_errors=True)


def _wal_ack_pending_record(
    credentials: dict[str, str] | None = None, **overrides: object
) -> ActivationRecord:
    record = _wal_restart_pending_record(credentials).advance(
        "wal_ack_pending",
        wal_exact_evidence={
            "timeline": "1",
            "segment": "00000001000000A20000008C",
            "switch_lsn": "A2/8C896608",
            "failed_count": "0",
            "archived_count": "1",
            "switch_intent_at": "2026-08-30T02:27:04.646291+00:00",
        },
        wal_verification_deadline="2026-08-30T02:32:04.646606+00:00",
    )
    deadline = overrides.pop("wal_verification_deadline", None)
    if deadline is not None:
        record = record.renew_wal_deadline(str(deadline))
    assert not overrides, f"unhandled overrides: {overrides}"
    return record


def test_switch_wal_runs_on_pitr_admin_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2026-08-30 failure: the switch ran on shared.db.direct_db_url (the
    runtime identity, no pg_switch_wal) while every read-only preflight check
    passed on the superuser connection. The mutation must dial the SAME admin
    URL the privilege probe certifies."""
    dialed: list[str] = []

    class _FakeRow:
        @staticmethod
        def fetchone() -> tuple[str]:
            return ("00000001000000A20000008D",)

    class _FakeConn:
        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, query: str) -> _FakeRow:
            assert "pg_switch_wal" in query
            return _FakeRow()

    monkeypatch.setattr(
        activation_runtime,
        "pitr_admin_url",
        lambda: "postgresql://super@/postgres?host=/sock&port=5433",
    )
    monkeypatch.setattr(
        "services.pitr.activation_runtime.psycopg.connect",
        lambda conninfo, **_kw: (dialed.append(conninfo), _FakeConn())[1],
    )
    assert activation._switch_wal() == "00000001000000A20000008D"
    assert dialed == ["postgresql://super@/postgres?host=/sock&port=5433"]


def test_probe_switch_privilege_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection that cannot EXECUTE pg_switch_wal must refuse the shadow
    gate BEFORE any config mutation — not crash the activation mid-flight."""
    monkeypatch.setattr(activation_runtime, "pitr_admin_url", lambda: "admin-url")
    called: list[str] = []

    class _FakeRow:
        @staticmethod
        def fetchone() -> list[bool]:
            return [False]

    class _FakeConn:
        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, query: str) -> _FakeRow:
            called.append(query)
            return _FakeRow()

    def fake_connect(conninfo: str, **_kw: object) -> _FakeConn:
        assert conninfo == "admin-url"
        return _FakeConn()

    monkeypatch.setattr(
        "services.pitr.activation_runtime.psycopg.connect",
        fake_connect,
    )
    with pytest.raises(RuntimeError, match="pg_switch_wal"):
        activation_runtime.probe_switch_privilege()
    assert "has_function_privilege" in called[0]


def test_renew_wal_deadline_only_at_wal_ack_pending() -> None:
    record = _wal_ack_pending_record()
    fresh = "2026-08-30T10:40:00+00:00"
    renewed = record.renew_wal_deadline(fresh)
    assert renewed.wal_verification_deadline == fresh
    assert renewed.phase == "wal_ack_pending"
    # the switch intent is untouched — it is the ACK lower bound
    assert renewed.wal_exact_evidence == record.wal_exact_evidence
    with pytest.raises(ValueError, match="wal_ack_pending"):
        _wal_config_pending_record().renew_wal_deadline(fresh)


def test_wal_ack_attempt_renews_expired_deadline_before_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An attempt that crashed at the switch step left an expired persisted
    deadline (2026-08-30). The next attempt re-stamps the window BEFORE the
    switch so the proof loop gets a live upper bound — the operation resumes
    instead of forcing rollback + full re-activation."""
    from datetime import UTC, datetime, timedelta

    expired = (datetime.now(UTC) - timedelta(minutes=3)).isoformat()
    record = _wal_ack_pending_record(wal_verification_deadline=expired)
    write_record(tmp_path, record)
    monkeypatch.setattr(activation, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(activation, "_require_same_credentials", lambda *_a: None)

    def fake_switch() -> str:
        # The ordering lock: when the switch runs, the durable record must
        # already carry the renewed deadline (renew happens BEFORE switch).
        durable_at_switch = load_record(tmp_path)
        assert durable_at_switch is not None
        fresh_at_switch = datetime.fromisoformat(durable_at_switch.wal_verification_deadline or "")
        assert fresh_at_switch > datetime.now(UTC) - timedelta(minutes=1)
        raise RuntimeError("switch reached")

    def proof(_rec: ActivationRecord, _stop: object) -> tuple[dict[str, str], dict[str, str]]:
        raise RuntimeError("proof must not run before the switch")

    monkeypatch.setattr(activation, "_switch_wal", fake_switch)
    monkeypatch.setattr(activation, "_remote_wal_proof", proof)
    monkeypatch.setattr(activation, "run_while_renewing", lambda _h, fn: fn(None))
    with pytest.raises(RuntimeError, match="switch reached"):
        activation._advance_activation(tmp_path, record, "holder")
    durable = load_record(tmp_path)
    assert durable is not None
    fresh = datetime.fromisoformat(durable.wal_verification_deadline or "")
    assert (
        datetime.now(UTC) - timedelta(minutes=1) < fresh < datetime.now(UTC) + timedelta(minutes=6)
    )
    assert durable.wal_exact_evidence == record.wal_exact_evidence


def test_probe_switch_privilege_against_real_pg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Real-PG regression for the 2026-08-30 privilege gap: the probe passes on
    the admin (superuser) connection and fails closed on a role without the
    grant — the exact discrimination the old preflight missed."""
    import shutil
    import subprocess
    import tempfile

    from shared.pg_tools import pg_tool

    port = 39614
    # The socket directory must live under a SHORT root: the default pytest
    # tmp_path on macOS (/var/folders/...) exceeds PostgreSQL's 103-byte
    # unix-socket path limit and fails every real-PG test locally, while CI's
    # short /tmp path masks it (QA #1076 nit 7).
    sock = Path(tempfile.mkdtemp(prefix="ava-pg-sock-", dir="/tmp"))
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
            f"-p {port} -c listen_addresses='' -c unix_socket_directories={sock} "
            "-c fsync=off -c full_page_writes=off -c synchronous_commit=off",
        ],
        check=True,
        capture_output=True,
    )
    try:
        admin_url = f"postgresql://ava@/postgres?host={sock}&port={port}"
        monkeypatch.setattr(activation_runtime, "pitr_admin_url", lambda: admin_url)
        activation_runtime.probe_switch_privilege()  # superuser: passes
        # A role without the grant: the probe must refuse (the prod failure shape).
        import psycopg

        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute("CREATE ROLE limited LOGIN")
        limited_url = f"postgresql://limited@/postgres?host={sock}&port={port}"
        monkeypatch.setattr(activation_runtime, "pitr_admin_url", lambda: limited_url)
        with pytest.raises(RuntimeError, match="pg_switch_wal"):
            activation_runtime.probe_switch_privilege()
    finally:
        subprocess.run(  # noqa: S603
            [pg_tool("pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
            check=False,
            capture_output=True,
        )
        shutil.rmtree(sock, ignore_errors=True)


def _durable_ack_raw(*, backend: str) -> dict[str, object]:
    """One durable WAL ACK payload, shaped for the backend under test."""
    segment = "00000001000000A20000008C"
    if backend == "gcs":
        checksum, pin_token, checksum_algo = "AAAAAA==", "123", "crc32c"
    else:
        checksum = "b" * 32
        pin_token = "123456789:" + checksum
        checksum_algo = "md5"
    return {
        "archive_name": segment,
        "source_sha256": "1" * 64,
        "source_size": 1,
        "object_name": f"pitr/wal/{segment[:8]}/{segment}.enc",
        "pin_token": pin_token,
        "ciphertext_size": 10,
        "ciphertext_crc32c": checksum,
        "ciphertext_checksum_algo": checksum_algo,
        "ciphertext_checksum_value": checksum,
        "encryption_format": "AVAPITR1",
        "key_id": "key",
        "acknowledged_at": "2026-08-30T03:30:00+00:00",
    }


class _ArchiverRow:
    def __init__(self, values: tuple[str, str, str]) -> None:
        self._values = values

    def fetchone(self) -> tuple[str, str, str]:
        return self._values


class _ArchiverConn:
    """A psycopg connection whose pg_stat_archiver row reports the target
    segment archived with zero failures past the durable count."""

    def __init__(self, segment: str) -> None:
        self._segment = segment

    def __enter__(self) -> _ArchiverConn:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, _query: str) -> _ArchiverRow:
        return _ArchiverRow((self._segment, "0", "2"))


class _ViewerStore:
    def __init__(self, observed: RemoteObjectAck) -> None:
        self._observed = observed

    def stat(self, object_name: str) -> RemoteObjectAck | None:
        return self._observed


class _ViewerStoreGroup:
    def __init__(self, observed: RemoteObjectAck) -> None:
        self._observed = observed

    def viewer_object_store(self) -> _ViewerStore:
        return _ViewerStore(self._observed)


def _wire_proof_world(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    backend: str,
    ack_raw: dict[str, object],
    observed: RemoteObjectAck | None = None,
    ack_file_stem: str | None = None,
) -> None:
    """Wire the proof loop's external world onto a temp ava_home: the
    backend config, the archiver stats row, the durable ACK file, and the
    viewer store's stat observation."""
    config = settings.physical_backup
    monkeypatch.setattr(config, "pitr_store_backend", backend)
    monkeypatch.setattr(config, "pitr_gcs_prefix", "pitr")
    if backend == "gcs":
        monkeypatch.setattr(config, "pitr_gcs_bucket", "bucket")
        monkeypatch.setattr(config, "pitr_restore_gcs_credentials_file", tmp_path / "viewer.json")
    else:
        monkeypatch.setattr(config, "pitr_baidu_app_root", "/apps/ava/ava-pitr")

    stem = ack_file_stem or str(ack_raw["archive_name"])
    monkeypatch.setattr(activation_runtime, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(
        "services.pitr.activation_runtime.psycopg.connect",
        lambda _conninfo, **_kw: _ArchiverConn(stem),
    )
    if observed is None:
        ack = ack_manifest_from_raw(ack_raw)
        observed = RemoteObjectAck(
            object_name=ack.object_name,
            pin_token=ack.pin_token,
            size=ack.ciphertext_size,
            checksum=ObjectChecksum(
                algo=ack.ciphertext_checksum_algo, value=ack.ciphertext_checksum_value
            ),
            metadata=wal_metadata(ack),
            created=True,
        )
    monkeypatch.setattr(
        "services.pitr.store_factory.get_store_group", lambda: _ViewerStoreGroup(observed)
    )
    ack_dir = tmp_path / "physical-backup" / "ack"
    ack_dir.mkdir(parents=True)
    (ack_dir / f"{stem}.ack.json").write_text(json.dumps(ack_raw))


def _remote_wal_proof_round_trips(
    record: ActivationRecord,
    ack_evidence: dict[str, str],
    viewer_evidence: dict[str, str],
) -> None:
    """The transferred evidence must clear the durable record validator —
    the same gate the CLI's advance-to-wal_remote_verified runs."""
    verified = record.advance(
        "wal_remote_verified",
        wal_ack_evidence=ack_evidence,
        wal_viewer_proof=viewer_evidence,
    )
    assert verified.phase == "wal_remote_verified"


def test_remote_wal_proof_transfers_gcs_evidence_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The proof loop must transfer the durable ACK and the viewer stat into
    the (ack, viewer) evidence pair, and that pair must round-trip through
    the record validator's GCS vocabulary."""
    from datetime import UTC, datetime, timedelta

    deadline = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    record = _wal_ack_pending_record(wal_verification_deadline=deadline)
    ack_raw = _durable_ack_raw(backend="gcs")
    _wire_proof_world(monkeypatch, tmp_path, backend="gcs", ack_raw=ack_raw)

    ack_evidence, viewer_evidence = activation_runtime.remote_wal_proof(record)

    assert ack_evidence["bucket_name"] == "bucket"
    assert ack_evidence["generation"] == "123"
    assert ack_evidence["ciphertext_crc32c"] == "AAAAAA=="
    assert ack_evidence["acknowledged_at"] == "2026-08-30T03:30:00+00:00"
    assert viewer_evidence["viewer_id"] == "v@example.test"
    assert datetime.fromisoformat(viewer_evidence["observed_at"]) > datetime.fromisoformat(
        ack_evidence["acknowledged_at"]
    )
    _remote_wal_proof_round_trips(record, ack_evidence, viewer_evidence)


def test_remote_wal_proof_transfers_baidu_evidence_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same transfer on the Baidu vocabulary: the app-root store target and
    the fs_id:md5 pin token must flow through and re-validate."""
    from datetime import UTC, datetime, timedelta

    deadline = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    record = _wal_ack_pending_record(
        baidu_credential_evidence(), wal_verification_deadline=deadline
    )
    ack_raw = _durable_ack_raw(backend="baidu")
    _wire_proof_world(monkeypatch, tmp_path, backend="baidu", ack_raw=ack_raw)

    ack_evidence, viewer_evidence = activation_runtime.remote_wal_proof(record)

    assert ack_evidence["bucket_name"] == "/apps/ava/ava-pitr"
    assert ack_evidence["generation"] == "123456789:" + "b" * 32
    assert ack_evidence["ciphertext_crc32c"] == "b" * 32
    assert viewer_evidence["viewer_id"] == "app-key"
    _remote_wal_proof_round_trips(record, ack_evidence, viewer_evidence)


def test_remote_wal_proof_refuses_viewer_evidence_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A viewer stat whose pin token drifted from the durable ACK must fail
    the proof instead of transferring mismatched evidence."""
    from datetime import UTC, datetime, timedelta

    deadline = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    record = _wal_ack_pending_record(wal_verification_deadline=deadline)
    ack_raw = _durable_ack_raw(backend="gcs")
    ack = ack_manifest_from_raw(ack_raw)
    drifted = RemoteObjectAck(
        object_name=ack.object_name,
        pin_token="456",  # noqa: S106 — test fixture drift value
        size=ack.ciphertext_size,
        checksum=ObjectChecksum(
            algo=ack.ciphertext_checksum_algo, value=ack.ciphertext_checksum_value
        ),
        metadata=wal_metadata(ack),
        created=True,
    )
    _wire_proof_world(monkeypatch, tmp_path, backend="gcs", ack_raw=ack_raw, observed=drifted)
    with pytest.raises(RuntimeError, match="viewer observed WAL differs"):
        activation_runtime.remote_wal_proof(record)


def test_remote_wal_proof_refuses_viewer_metadata_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A viewer stat whose metadata drifted from the durable ACK's metadata
    must fail the proof — the store's observed tags are part of the
    evidence, not decoration."""
    from datetime import UTC, datetime, timedelta

    deadline = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    record = _wal_ack_pending_record(wal_verification_deadline=deadline)
    ack_raw = _durable_ack_raw(backend="gcs")
    ack = ack_manifest_from_raw(ack_raw)
    drifted = RemoteObjectAck(
        object_name=ack.object_name,
        pin_token=ack.pin_token,
        size=ack.ciphertext_size,
        checksum=ObjectChecksum(
            algo=ack.ciphertext_checksum_algo, value=ack.ciphertext_checksum_value
        ),
        metadata={"ava-archive-name": "tampered"},
        created=True,
    )
    _wire_proof_world(monkeypatch, tmp_path, backend="gcs", ack_raw=ack_raw, observed=drifted)
    with pytest.raises(RuntimeError, match="metadata differs"):
        activation_runtime.remote_wal_proof(record)


def test_remote_wal_proof_refuses_an_ack_for_a_different_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A durable ACK whose archive_name differs from the activation's exact
    segment must fail the proof — the file's name is not its identity."""
    from datetime import UTC, datetime, timedelta

    deadline = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    record = _wal_ack_pending_record(wal_verification_deadline=deadline)
    ack_raw = _durable_ack_raw(backend="gcs")
    ack_raw["archive_name"] = "00000001000000A20000008D"
    _wire_proof_world(
        monkeypatch,
        tmp_path,
        backend="gcs",
        ack_raw=ack_raw,
        ack_file_stem="00000001000000A20000008C",
    )
    with pytest.raises(RuntimeError, match="targets a different archive"):
        activation_runtime.remote_wal_proof(record)


def test_remote_wal_proof_refuses_after_the_deadline_passed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An expired verification deadline must end the loop with the durable
    deadline error instead of transferring stale evidence."""
    from datetime import UTC, datetime, timedelta

    expired = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    record = _wal_ack_pending_record(wal_verification_deadline=expired)
    ack_raw = _durable_ack_raw(backend="gcs")
    _wire_proof_world(monkeypatch, tmp_path, backend="gcs", ack_raw=ack_raw)
    with pytest.raises(RuntimeError, match="deadline expired"):
        activation_runtime.remote_wal_proof(record)


def test_refusal_message_shows_the_tail_of_a_long_detail() -> None:
    """A refusal must expose the CAUSE, and wrapped tracebacks put it in the
    last lines — the 2026-09-03 activation #7 root cause (sandbox postmaster
    socket-path failure) was invisible behind the old head-only truncation."""
    # Short details pass through whole.
    assert refusal_message(RuntimeError("short cause")) == ("RuntimeError: short cause")
    # Long details keep their tail, bounded, with an ellipsis marker.
    head = "Traceback (most recent call last):\n" + ("  File ... prove_candidate\n" * 12)
    cause = "RestoreProofError: restore command exited 1: pg_ctl: FATAL: socket path too long"
    detail = head + cause
    message = refusal_message(RuntimeError(detail))
    assert message.startswith("RuntimeError: …")
    assert message.endswith(cause)
    assert len(message) <= len("RuntimeError: ") + 300 + 1
    # An empty detail stays type-only.
    assert refusal_message(RuntimeError("  \n")) == "RuntimeError"
