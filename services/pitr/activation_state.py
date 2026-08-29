"""Durable, crash-resumable state for explicit PITR activation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from shared.private_storage import ensure_private_dir

ActivationPhase = Literal[
    "shadow",
    "snapshot_pending",
    "snapshot_verified",
    "wal_config_pending",
    "wal_config_applying",
    "wal_restart_pending",
    "wal_ack_pending",
    "wal_remote_verified",
    "base_pending",
    "restore_pending",
    "protected",
    "rollback_pending",
    "rollback_restart_pending",
    "rolled_back",
]

_SCHEMA_VERSION = 3
_V2_FIELDS = {
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
_PHASES = frozenset(
    {
        "shadow",
        "snapshot_pending",
        "snapshot_verified",
        "wal_config_pending",
        "wal_config_applying",
        "wal_restart_pending",
        "wal_ack_pending",
        "wal_remote_verified",
        "base_pending",
        "restore_pending",
        "protected",
        "rollback_pending",
        "rollback_restart_pending",
        "rolled_back",
    }
)
_EVIDENCE_PHASES = _PHASES - {"shadow", "snapshot_pending", "rolled_back"}
_FORWARD_PHASES: tuple[ActivationPhase, ...] = (
    "shadow",
    "snapshot_pending",
    "snapshot_verified",
    "wal_config_pending",
    "wal_config_applying",
    "wal_restart_pending",
    "wal_ack_pending",
    "wal_remote_verified",
    "base_pending",
    "restore_pending",
    "protected",
)
_ROLLBACK_ENTRY_PHASES = frozenset(_FORWARD_PHASES[3:])
_EVIDENCE_KEYS = {
    "pre_activation_credential_evidence": frozenset(
        {
            "uploader_client_email",
            "uploader_project_id",
            "uploader_private_key_id",
            "viewer_client_email",
            "viewer_project_id",
            "viewer_private_key_id",
            "bucket_name",
            "object_prefix",
            "backup_key_id",
            "backup_key_sha256",
        }
    ),
    "wal_exact_evidence": frozenset(
        {
            "timeline",
            "segment",
            "switch_lsn",
            "failed_count",
            "archived_count",
            "switch_intent_at",
        }
    ),
    "wal_ack_evidence": frozenset(
        {
            "timeline",
            "segment",
            "bucket_name",
            "object_prefix",
            "object_name",
            "generation",
            "ciphertext_size",
            "ciphertext_crc32c",
            "source_sha256",
            "source_size",
            "key_id",
            "encryption_format",
            "acknowledged_at",
        }
    ),
    "wal_viewer_proof": frozenset(
        {
            "viewer_id",
            "timeline",
            "segment",
            "bucket_name",
            "object_prefix",
            "object_name",
            "generation",
            "ciphertext_size",
            "ciphertext_crc32c",
            "source_sha256",
            "source_size",
            "key_id",
            "encryption_format",
            "observed_at",
        }
    ),
}


@dataclass(frozen=True)
class ActivationRecord:
    schema_version: int
    operation_id: str
    phase: ActivationPhase
    started_at: str
    updated_at: str
    origin: str
    pre_activation_snapshot: str | None = None
    pre_activation_pg_settings: dict[str, str] | None = None
    pre_activation_credential_evidence: dict[str, str] | None = None
    pre_activation_pitr_env: dict[str, str] | None = None
    pre_activation_pg_auto_conf: dict[str, str] | None = None
    pre_activation_env_b64: str | None = None
    pre_activation_env_digest: str | None = None
    pre_activation_auto_conf_b64: str | None = None
    pre_activation_auto_conf_digest: str | None = None
    rollback_expected_env_digest: str | None = None
    rollback_expected_auto_conf_digest: str | None = None
    switched_wal: str | None = None
    protected_manifest: str | None = None
    wal_config_before_digest: str | None = None
    wal_config_desired_digest: str | None = None
    config_apply_intent: dict[str, str] | None = None
    config_apply_applied: dict[str, str] | None = None
    rollback_setting_intent: dict[str, str] | None = None
    rollback_settings_applied: dict[str, str] | None = None
    restart_handoff: str | None = None
    restart_orchestration: str | None = None
    rollback_postmaster_started_at: str | None = None
    restart_handoff_consumed_at: str | None = None
    restart_dispatch_session: str | None = None
    wal_exact_evidence: dict[str, str] | None = None
    wal_verification_deadline: str | None = None
    wal_ack_evidence: dict[str, str] | None = None
    wal_viewer_proof: dict[str, str] | None = None
    candidate_digest: str | None = None
    candidate_chain_id: str | None = None
    protected_digest: str | None = None
    error: str | None = None
    error_code: str | None = None
    error_detail: str | None = None

    @classmethod
    def start(cls, *, operation_id: str, origin: str) -> ActivationRecord:
        now = datetime.now(UTC).isoformat()
        return cls(
            schema_version=_SCHEMA_VERSION,
            operation_id=operation_id,
            phase="shadow",
            started_at=now,
            updated_at=now,
            origin=origin,
        )

    @classmethod
    def from_json(cls, payload: str) -> ActivationRecord:  # noqa: PLR0915
        raw_value: object = json.loads(payload)
        if not isinstance(raw_value, dict):
            raise TypeError("PITR activation record fields differ")
        raw = cast(dict[str, object], raw_value)
        if set(raw) == _V2_FIELDS and raw.get("schema_version") == 2:
            if raw.get("phase") not in {
                "shadow",
                "snapshot_pending",
                "snapshot_verified",
                "wal_config_pending",
            }:
                raise ValueError("PITR activation v2 operation cannot be safely upgraded")
            raw = {**dict.fromkeys(cls.__dataclass_fields__), **raw}
            raw["schema_version"] = _SCHEMA_VERSION
        if set(raw) != set(cls.__dataclass_fields__):
            raise ValueError("PITR activation record fields differ")
        phase = raw["phase"]
        if not isinstance(phase, str) or phase not in _PHASES:
            raise ValueError("unknown PITR activation phase")

        def required_string(name: str) -> str:
            value = raw[name]
            if not isinstance(value, str):
                raise TypeError(f"PITR activation {name} must be a string")
            return value

        def optional_string(name: str) -> str | None:
            value = raw[name]
            if value is not None and not isinstance(value, str):
                raise ValueError(f"PITR activation {name} must be a string or null")
            return value

        def optional_string_map(name: str) -> dict[str, str] | None:
            value = raw[name]
            if value is None:
                return None
            if not isinstance(value, dict):
                raise TypeError(f"PITR activation {name} must be an object or null")
            items = cast(dict[object, object], value)
            if not all(
                isinstance(key, str) and isinstance(item, str) for key, item in items.items()
            ):
                raise ValueError(f"PITR activation {name} must contain string pairs")
            if (
                name in _EVIDENCE_KEYS
                and frozenset(cast(dict[str, str], value)) != _EVIDENCE_KEYS[name]
            ):
                raise ValueError(f"PITR activation {name} fields differ")
            return cast(dict[str, str], value)

        def utc_timestamp(value: str, name: str) -> None:
            timestamp = datetime.fromisoformat(value)
            if timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(timestamp):
                raise ValueError(f"PITR activation {name} must be a UTC timestamp")

        schema_version = raw["schema_version"]
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise TypeError("PITR activation schema_version must be an integer")
        record = cls(
            schema_version=schema_version,
            operation_id=required_string("operation_id"),
            phase=cast(ActivationPhase, phase),
            started_at=required_string("started_at"),
            updated_at=required_string("updated_at"),
            origin=required_string("origin"),
            pre_activation_snapshot=optional_string("pre_activation_snapshot"),
            pre_activation_pg_settings=optional_string_map("pre_activation_pg_settings"),
            pre_activation_credential_evidence=optional_string_map(
                "pre_activation_credential_evidence"
            ),
            pre_activation_pitr_env=optional_string_map("pre_activation_pitr_env"),
            pre_activation_pg_auto_conf=optional_string_map("pre_activation_pg_auto_conf"),
            pre_activation_env_b64=optional_string("pre_activation_env_b64"),
            pre_activation_env_digest=optional_string("pre_activation_env_digest"),
            pre_activation_auto_conf_b64=optional_string("pre_activation_auto_conf_b64"),
            pre_activation_auto_conf_digest=optional_string("pre_activation_auto_conf_digest"),
            rollback_expected_env_digest=optional_string("rollback_expected_env_digest"),
            rollback_expected_auto_conf_digest=optional_string(
                "rollback_expected_auto_conf_digest"
            ),
            switched_wal=optional_string("switched_wal"),
            protected_manifest=optional_string("protected_manifest"),
            wal_config_before_digest=optional_string("wal_config_before_digest"),
            wal_config_desired_digest=optional_string("wal_config_desired_digest"),
            config_apply_intent=optional_string_map("config_apply_intent"),
            config_apply_applied=optional_string_map("config_apply_applied"),
            rollback_setting_intent=optional_string_map("rollback_setting_intent"),
            rollback_settings_applied=optional_string_map("rollback_settings_applied"),
            restart_handoff=optional_string("restart_handoff"),
            restart_orchestration=optional_string("restart_orchestration"),
            rollback_postmaster_started_at=optional_string("rollback_postmaster_started_at"),
            restart_handoff_consumed_at=optional_string("restart_handoff_consumed_at"),
            restart_dispatch_session=optional_string("restart_dispatch_session"),
            wal_exact_evidence=optional_string_map("wal_exact_evidence"),
            wal_verification_deadline=optional_string("wal_verification_deadline"),
            wal_ack_evidence=optional_string_map("wal_ack_evidence"),
            wal_viewer_proof=optional_string_map("wal_viewer_proof"),
            candidate_digest=optional_string("candidate_digest"),
            candidate_chain_id=optional_string("candidate_chain_id"),
            protected_digest=optional_string("protected_digest"),
            error=optional_string("error"),
            error_code=optional_string("error_code"),
            error_detail=optional_string("error_detail"),
        )
        if record.schema_version != _SCHEMA_VERSION:
            raise ValueError("unsupported PITR activation record schema")
        started = datetime.fromisoformat(record.started_at)
        updated = datetime.fromisoformat(record.updated_at)
        if started.tzinfo is None or updated.tzinfo is None:
            raise ValueError("PITR activation timestamps must carry timezone")
        if started.utcoffset() != UTC.utcoffset(started) or updated.utcoffset() != UTC.utcoffset(
            updated
        ):
            raise ValueError("PITR activation timestamps must be UTC")
        if updated < started:
            raise ValueError("PITR activation updated_at precedes started_at")
        if record.wal_verification_deadline is not None:
            utc_timestamp(record.wal_verification_deadline, "wal_verification_deadline")
        if record.wal_ack_evidence is not None:
            utc_timestamp(record.wal_ack_evidence["acknowledged_at"], "acknowledged_at")
        if record.wal_viewer_proof is not None:
            utc_timestamp(record.wal_viewer_proof["observed_at"], "observed_at")
        if record.wal_exact_evidence is not None:
            utc_timestamp(record.wal_exact_evidence["switch_intent_at"], "switch_intent_at")
            exact = record.wal_exact_evidence
            segment = exact["segment"]
            if not re.fullmatch(r"[0-9A-F]{24}", segment) or segment[:8] != (
                f"{int(exact['timeline']):08X}"
            ):
                raise ValueError("PITR WAL intent has a non-canonical segment")
        for journal, label in (
            (record.config_apply_intent, "intent"),
            (record.config_apply_applied, "applied"),
        ):
            if journal is None:
                continue
            if journal.get("kind") not in {"env", "postgresql_auto_conf"}:
                raise ValueError(f"PITR config apply {label} kind is unknown")
            expected = (
                {"kind", "expected_digest", "desired_digest"}
                if journal.get("kind") == "env" and label == "intent"
                else {"kind", "digest"}
                if journal.get("kind") == "env"
                else {"kind", "name", "expected_digest", "desired_value"}
                if label == "intent"
                else {"kind", "name", "digest"}
            )
            if set(journal) != expected:
                raise ValueError(f"PITR config apply {label} fields differ")
        rollback_names = {
            "archive_mode",
            "archive_command",
            "archive_timeout",
            "wal_compression",
        }
        if record.rollback_setting_intent is not None and set(record.rollback_setting_intent) != {
            "name",
            "expected_digest",
            "current_value",
            "desired_value",
        }:
            raise ValueError("PITR rollback setting intent fields differ")
        if (
            record.rollback_setting_intent is not None
            and record.rollback_setting_intent["name"] not in rollback_names
        ):
            raise ValueError("PITR rollback setting intent name is unknown")
        if record.rollback_settings_applied is not None:
            if not set(record.rollback_settings_applied) <= rollback_names:
                raise ValueError("PITR rollback applied setting name is unknown")
            for evidence in record.rollback_settings_applied.values():
                loaded: object = json.loads(evidence)
                if not isinstance(loaded, dict) or not all(isinstance(key, str) for key in loaded):
                    raise ValueError("PITR rollback applied evidence fields differ")
                value = cast(dict[str, object], loaded)
                if set(value) != {"desired_value", "post_digest"}:
                    raise ValueError("PITR rollback applied evidence fields differ")
                desired_value = value["desired_value"]
                post_digest = value["post_digest"]
                if not isinstance(desired_value, str) or not isinstance(post_digest, str):
                    raise TypeError("PITR rollback applied evidence fields differ")
                if not re.fullmatch(r"[0-9a-f]{64}", post_digest):
                    raise ValueError("PITR rollback applied digest is invalid")
        baseline = record.pre_activation_pg_auto_conf or {}
        if record.rollback_setting_intent is not None and record.rollback_setting_intent[
            "desired_value"
        ] != baseline.get(record.rollback_setting_intent["name"]):
            raise ValueError("PITR rollback setting intent differs from baseline")
        if record.rollback_settings_applied is not None and any(
            json.loads(evidence)["desired_value"] != baseline.get(name)
            for name, evidence in record.rollback_settings_applied.items()
        ):
            raise ValueError("PITR rollback applied evidence differs from baseline")
        if record.wal_ack_evidence is not None and record.wal_viewer_proof is not None:
            ack, viewer = record.wal_ack_evidence, record.wal_viewer_proof
            immutable = (
                "timeline",
                "segment",
                "bucket_name",
                "object_prefix",
                "object_name",
                "generation",
                "ciphertext_size",
                "ciphertext_crc32c",
                "key_id",
                "encryption_format",
                "source_sha256",
                "source_size",
            )
            if any(ack[name] != viewer[name] for name in immutable):
                raise ValueError("PITR ACK and viewer immutable evidence differ")
            frozen = record.pre_activation_credential_evidence or {}
            if record.wal_exact_evidence is None or any(
                (
                    ack["segment"] != record.wal_exact_evidence["segment"],
                    ack["timeline"] != record.wal_exact_evidence["timeline"],
                    ack["bucket_name"] != frozen.get("bucket_name"),
                    ack["object_prefix"] != frozen.get("object_prefix"),
                    ack["key_id"] != frozen.get("backup_key_id"),
                    viewer["viewer_id"] != frozen.get("viewer_client_email"),
                )
            ):
                raise ValueError("PITR remote evidence differs from WAL intent")
            expected_object = (
                f"{frozen.get('object_prefix', '').rstrip('/')}/wal/"
                f"{ack['segment'][:8]}/{ack['segment']}.enc"
            )
            if ack["object_name"] != expected_object:
                raise ValueError("PITR remote object name is not canonical")
            if int(ack["generation"]) <= 0 or int(ack["ciphertext_size"]) <= 0:
                raise ValueError("PITR remote identity must have positive generation and size")
            if int(ack["source_size"]) <= 0 or not re.fullmatch(
                r"[0-9a-f]{64}", ack["source_sha256"]
            ):
                raise ValueError("PITR remote evidence has invalid source identity")
            if not re.fullmatch(r"[A-Za-z0-9+/]{6}==", ack["ciphertext_crc32c"]):
                raise ValueError("PITR remote evidence has invalid CRC32C")
            intent_at = datetime.fromisoformat(record.wal_exact_evidence["switch_intent_at"])
            acknowledged = datetime.fromisoformat(ack["acknowledged_at"])
            observed = datetime.fromisoformat(viewer["observed_at"])
            if record.wal_verification_deadline is None:
                raise ValueError("PITR remote proof lacks its durable deadline")
            deadline = datetime.fromisoformat(record.wal_verification_deadline)
            if not intent_at <= acknowledged <= observed <= deadline:
                raise ValueError("PITR remote evidence falls outside its durable deadline")
        if record.error_code is not None and record.error_code not in {
            "gcs_forbidden",
            "wal_deadline",
            "credential_drift",
            "state_cas",
            "restore_mismatch",
            "restart_failure",
            "activation_failure",
        }:
            raise ValueError("PITR activation error code is unknown")
        if (record.error_code is None) != (record.error_detail is None):
            raise ValueError("PITR activation diagnostics are incomplete")
        if record.pre_activation_credential_evidence is not None and not re.fullmatch(
            r"[0-9a-f]{64}",
            record.pre_activation_credential_evidence["backup_key_sha256"],
        ):
            raise ValueError("PITR backup key fingerprint must be lowercase SHA256")
        if record.phase in _EVIDENCE_PHASES and (
            not record.pre_activation_snapshot
            or not record.pre_activation_pg_settings
            or not record.pre_activation_credential_evidence
        ):
            raise ValueError("PITR activation phase is missing logical recovery evidence")
        if record.phase == "snapshot_pending" and (
            not record.pre_activation_pg_settings or not record.pre_activation_credential_evidence
        ):
            raise ValueError("PITR activation snapshot phase is missing shadow evidence")
        phase_index = _FORWARD_PHASES.index(record.phase) if record.phase in _FORWARD_PHASES else -1
        if phase_index >= _FORWARD_PHASES.index("wal_config_applying") and (
            not record.wal_config_before_digest
            or not record.wal_config_desired_digest
            or not record.pre_activation_pitr_env
            or not record.pre_activation_pg_auto_conf
            or record.pre_activation_env_b64 is None
            or not record.pre_activation_env_digest
            or record.pre_activation_auto_conf_b64 is None
            or not record.pre_activation_auto_conf_digest
        ):
            raise ValueError("PITR activation phase is missing WAL config digests")
        if phase_index >= _FORWARD_PHASES.index("wal_restart_pending") and (
            not record.restart_handoff
            or not record.restart_orchestration
            or not record.rollback_expected_env_digest
            or not record.rollback_expected_auto_conf_digest
        ):
            raise ValueError("PITR activation phase is missing restart orchestration evidence")
        if (record.restart_handoff_consumed_at is None) != (
            record.restart_dispatch_session is None
        ):
            raise ValueError("PITR restart handoff binding is incomplete")
        if phase_index >= _FORWARD_PHASES.index("wal_ack_pending") and (
            not record.wal_exact_evidence or not record.wal_verification_deadline
        ):
            raise ValueError("PITR activation phase is missing exact WAL evidence")
        if phase_index >= _FORWARD_PHASES.index("wal_remote_verified") and (
            not record.wal_ack_evidence or not record.wal_viewer_proof
        ):
            raise ValueError("PITR activation phase is missing remote WAL proof")
        if phase_index >= _FORWARD_PHASES.index("base_pending") and not record.candidate_chain_id:
            raise ValueError("PITR activation phase is missing candidate chain intent")
        if phase_index >= _FORWARD_PHASES.index("restore_pending") and not record.candidate_digest:
            raise ValueError("PITR activation phase is missing candidate digest")
        if record.phase == "protected" and (
            not record.protected_manifest or not record.protected_digest
        ):
            raise ValueError("protected PITR activation is missing protected evidence")
        if record.phase == "protected" and record.config_apply_intent is not None:
            raise ValueError("protected PITR activation retains a config intent")
        if record.phase in {"rollback_pending", "rollback_restart_pending"} and (
            not record.wal_config_before_digest
            or not record.restart_handoff
            or not record.restart_orchestration
            or not record.rollback_postmaster_started_at
            or not record.rollback_expected_env_digest
            or not record.rollback_expected_auto_conf_digest
            or record.pre_activation_env_b64 is None
            or not record.pre_activation_env_digest
            or record.pre_activation_auto_conf_b64 is None
            or not record.pre_activation_auto_conf_digest
            or not record.pre_activation_pitr_env
            or not record.pre_activation_pg_auto_conf
            or not record.pre_activation_pg_settings
        ):
            raise ValueError("PITR rollback phase is missing restart evidence")
        if record.phase == "rollback_restart_pending" and (
            record.rollback_setting_intent is not None
            or set(record.rollback_settings_applied or {}) != rollback_names
        ):
            raise ValueError("PITR rollback restart lacks per-setting applied evidence")
        if (
            record.phase == "rolled_back"
            and record.wal_config_before_digest is not None
            and (
                not record.restart_handoff
                or not record.restart_orchestration
                or not record.restart_handoff_consumed_at
                or not record.restart_dispatch_session
                or not record.rollback_postmaster_started_at
                or not record.rollback_expected_env_digest
                or not record.rollback_expected_auto_conf_digest
                or record.pre_activation_env_b64 is None
                or not record.pre_activation_env_digest
                or record.pre_activation_auto_conf_b64 is None
                or not record.pre_activation_auto_conf_digest
                or not record.pre_activation_pitr_env
                or not record.pre_activation_pg_auto_conf
                or not record.pre_activation_pg_settings
            )
        ):
            raise ValueError("mutated PITR rollback is missing full ownership evidence")
        if record.phase == "restore_pending":
            from services.pitr.base_manifest import CandidateManifest

            if record.protected_manifest is None:
                raise ValueError("restore-pending PITR activation lacks candidate manifest")
            candidate = CandidateManifest.from_json(record.protected_manifest)
            canonical = candidate.to_json()
            if (
                candidate.chain_id != record.candidate_chain_id
                or not candidate.chain_id.endswith(f"-{record.operation_id}")
                or hashlib.sha256(canonical.encode()).hexdigest() != record.candidate_digest
            ):
                raise ValueError("restore-pending candidate differs from activation evidence")
        if record.phase == "protected":
            from services.pitr.restore_manifest import ProtectedManifest

            protected = ProtectedManifest.from_json(cast(str, record.protected_manifest))
            canonical = protected.to_json()
            if (
                protected.chain_id != record.candidate_chain_id
                or not protected.chain_id.endswith(f"-{record.operation_id}")
                or protected.candidate_sha256 != record.candidate_digest
                or hashlib.sha256(canonical.encode()).hexdigest() != record.protected_digest
                or protected.candidate.system_identifier
                != (record.pre_activation_pg_settings or {}).get("system_identifier")
                or protected.candidate.base_object.key_id
                != (record.pre_activation_credential_evidence or {}).get("backup_key_id")
            ):
                raise ValueError("protected manifest differs from activation evidence")
        return record

    def advance(self, phase: ActivationPhase, **changes: object) -> ActivationRecord:
        if phase == self.phase:
            if set(changes) - {"error", "error_code", "error_detail"}:
                raise ValueError("same-phase PITR activation update may only change diagnostics")
        elif phase == "rollback_pending":
            if self.phase not in _ROLLBACK_ENTRY_PHASES:
                raise ValueError("illegal PITR activation rollback entry")
        elif self.phase in _FORWARD_PHASES:
            current_index = _FORWARD_PHASES.index(self.phase)
            if (
                current_index + 1 >= len(_FORWARD_PHASES)
                or phase != _FORWARD_PHASES[current_index + 1]
            ):
                raise ValueError("illegal PITR activation phase transition")
        elif self.phase == "rollback_pending":
            if phase != "rollback_restart_pending":
                raise ValueError("illegal PITR activation phase transition")
        elif self.phase == "rollback_restart_pending":
            if phase != "rolled_back":
                raise ValueError("illegal PITR activation phase transition")
        else:
            raise ValueError("terminal PITR activation phase cannot advance")
        if set(changes) & {"schema_version", "operation_id", "started_at", "origin"}:
            raise ValueError("PITR activation identity fields are immutable")
        raw = asdict(self)
        raw.update(changes)
        if phase != self.phase and changes.get("error") is None:
            raw["error_code"] = None
            raw["error_detail"] = None
        raw["phase"] = phase
        raw["updated_at"] = datetime.now(UTC).isoformat()
        return ActivationRecord.from_json(json.dumps(raw))

    def journal_config(self, **changes: object) -> ActivationRecord:
        allowed = {
            "config_apply_intent",
            "config_apply_applied",
            "rollback_expected_env_digest",
            "rollback_expected_auto_conf_digest",
        }
        if self.phase != "wal_config_applying" or set(changes) - allowed:
            raise ValueError("invalid PITR config application journal update")
        raw = asdict(self)
        raw.update(changes)
        raw["updated_at"] = datetime.now(UTC).isoformat()
        return ActivationRecord.from_json(json.dumps(raw))

    def journal_rollback(self, **changes: object) -> ActivationRecord:
        allowed = {
            "rollback_setting_intent",
            "rollback_settings_applied",
            "rollback_expected_auto_conf_digest",
        }
        if self.phase != "rollback_pending" or set(changes) - allowed:
            raise ValueError("invalid PITR rollback setting journal update")
        raw = asdict(self)
        raw.update(changes)
        raw["updated_at"] = datetime.now(UTC).isoformat()
        return ActivationRecord.from_json(json.dumps(raw))


def activation_root(home: Path) -> Path:
    return home / "physical-backup" / "activation"


def record_path(home: Path) -> Path:
    return activation_root(home) / "operation.json"


def lock_path(home: Path) -> Path:
    return activation_root(home) / "operation.lock"


def load_record(home: Path) -> ActivationRecord | None:
    path = record_path(home)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("PITR activation record is not a regular file")
    return ActivationRecord.from_json(path.read_text())


def write_record(home: Path, record: ActivationRecord) -> None:
    ActivationRecord.from_json(json.dumps(asdict(record)))
    directory = ensure_private_dir(activation_root(home))
    path = record_path(home)
    fd, raw = tempfile.mkstemp(prefix=".operation-", suffix=".partial", dir=directory)
    partial = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(asdict(record), output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        partial.chmod(0o600)
        partial.replace(path)
        directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        partial.unlink(missing_ok=True)


def write_record_cas(
    home: Path, *, expected: ActivationRecord, replacement: ActivationRecord
) -> None:
    """Replace exactly the state the caller read; activation.lock serializes writers."""

    current = load_record(home)
    if current != expected:
        raise RuntimeError("PITR activation state changed before durable transition")
    if replacement.operation_id != expected.operation_id:
        raise RuntimeError("PITR activation CAS cannot change operation identity")
    write_record(home, replacement)


def consume_restart_handoff(
    home: Path, expected: ActivationRecord, *, session: str
) -> ActivationRecord:
    if expected.phase not in {"wal_restart_pending", "rollback_restart_pending"}:
        raise ValueError("PITR restart handoff is not pending")
    if expected.restart_handoff_consumed_at is not None:
        raise RuntimeError("PITR restart handoff token was already consumed")
    raw = asdict(expected)
    now = datetime.now(UTC).isoformat()
    raw["restart_handoff_consumed_at"] = now
    raw["restart_dispatch_session"] = session
    raw["updated_at"] = now
    replacement = ActivationRecord.from_json(json.dumps(raw))
    write_record_cas(home, expected=expected, replacement=replacement)
    return replacement


def rearm_restart_handoff(
    home: Path, expected: ActivationRecord, *, session: str
) -> ActivationRecord:
    """Rearm a bound handoff after the orchestration seam proved no child exists.

    The caller must hold the cluster lifecycle lock.  This CAS is deliberately
    separate from consumption: a retry can only clear the exact session it is
    about to bind again, never turn an arbitrary consumed token back into work.
    """

    if expected.phase not in {"wal_restart_pending", "rollback_restart_pending"}:
        raise ValueError("PITR restart handoff is not pending")
    if expected.restart_handoff_consumed_at is None or expected.restart_dispatch_session != session:
        raise RuntimeError("PITR restart handoff is not bound to this session")
    raw = asdict(expected)
    now = datetime.now(UTC).isoformat()
    raw["restart_handoff_consumed_at"] = None
    raw["restart_dispatch_session"] = None
    raw["updated_at"] = now
    replacement = ActivationRecord.from_json(json.dumps(raw))
    write_record_cas(home, expected=expected, replacement=replacement)
    return replacement


def mark_pre_mutation_rolled_back(home: Path, expected: ActivationRecord) -> ActivationRecord:
    if expected.phase not in {
        "shadow",
        "snapshot_pending",
        "snapshot_verified",
        "wal_config_pending",
    }:
        raise ValueError("PITR operation already crossed the mutation boundary")
    raw = asdict(expected)
    now = datetime.now(UTC).isoformat()
    raw.update(phase="rolled_back", updated_at=now, error=None)
    replacement = ActivationRecord.from_json(json.dumps(raw))
    write_record_cas(home, expected=expected, replacement=replacement)
    return replacement
