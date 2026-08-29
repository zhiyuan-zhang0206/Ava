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
from services.pitr.activation_observability import save_error
from services.pitr.activation_runtime import (
    _restore_exact_file,
    archiver_reached_target,
    pitr_env_is_desired,
    rollback_effect_state,
)
from services.pitr.activation_state import ActivationRecord, load_record, write_record


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
        "_pitr_env_baseline",
        lambda: dict.fromkeys(activation._PITR_ENV_FIELDS, "[]"),
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


def _wal_config_pending_record() -> ActivationRecord:
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
            pre_activation_credential_evidence=_credentials(),
        )
        .advance("snapshot_verified", pre_activation_snapshot="/verified.dump.enc")
        .advance("wal_config_pending")
    )


def _wal_restart_pending_record() -> ActivationRecord:
    return (
        _wal_config_pending_record()
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
    monkeypatch.setattr(
        activation,
        "_validate_secrets",
        lambda: dict(_wal_config_pending_record().pre_activation_credential_evidence or {}),
    )
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
    monkeypatch.setattr(activation, "_validate_secrets", lambda: {"viewer": "separate"})
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


def test_env_baseline_restores_exact_raw_presence_and_duplicate_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = tmp_path / ".env"
    env.write_text("KEEP='unchanged'\nAVA_PITR_ENABLED='false'\nAVA_PITR_ENABLED=false # exact\n")
    monkeypatch.setattr(activation, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(activation_runtime, "ava_home", lambda: tmp_path)
    baseline = activation._pitr_env_baseline()
    env.write_text(
        "KEEP='unchanged'\n"
        "AVA_PITR_ENABLED=true\n"
        "AVA_PITR_BASE_BACKUP_ENABLED=true\n"
        "AVA_PITR_RESTORE_PROOF_ENABLED=true\n"
        "AVA_PITR_RETENTION_PLANNER_ENABLED=false\n"
    )

    from services.pitr.activation_runtime import _restore_pitr_env

    _restore_pitr_env(baseline)

    lines = env.read_text().splitlines()
    assert lines == [
        "KEEP='unchanged'",
        "AVA_PITR_ENABLED='false'",
        "AVA_PITR_ENABLED=false # exact",
    ]
    assert json.loads(baseline["pitr_base_backup_enabled"]) == []


def test_pitr_env_desired_requires_all_four_owned_aliases() -> None:
    assert pitr_env_is_desired(
        b"AVA_PITR_ENABLED=true\n"
        b"AVA_PITR_BASE_BACKUP_ENABLED=true\n"
        b"AVA_PITR_RESTORE_PROOF_ENABLED=true\n"
        b"AVA_PITR_RETENTION_PLANNER_ENABLED=false\n"
    )
    assert not pitr_env_is_desired(b"AVA_PITR_ENABLED=true\n")


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
        lambda: {**_credentials(), "viewer_client_email": "new@example.test"},
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
    import subprocess
    from types import SimpleNamespace

    from shared.pg_tools import pg_tool

    # TCP-free and isolated by this test's private socket directory. Releasing
    # an ephemeral TCP socket before pg_ctl binds it races every xdist worker.
    port = 39613
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
            f"-p {port} -c listen_addresses='' -c unix_socket_directories={tmp_path} "
            "-c fsync=off -c full_page_writes=off -c synchronous_commit=off",
        ],
        check=True,
        capture_output=True,
    )
    try:
        subprocess.run(  # noqa: S603
            [pg_tool("createdb"), "-h", str(tmp_path), "-p", str(port), "-U", "ava", "ava"],
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
            lambda _pg_port: f"postgresql://ava@/postgres?host={tmp_path}&port={port}",
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
