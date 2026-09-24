"""Durable, crash-resumable state for explicit PITR activation."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from services.pitr.activation_evidence import (
    _embedded_candidate_raw,
    stored_digest_matches,
    validate_wal_remote_evidence,
)
from services.pitr.base_manifest import CandidateManifest
from shared.api_contracts import strict_decode
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

_SCHEMA_VERSION = 4
_FIELDS_ERROR = "PITR activation record fields differ"
_STRING_FIELD_ERROR = "PITR activation {name} must be a string"
_OPTIONAL_STRING_FIELD_ERROR = "PITR activation {name} must be a string or null"
_INTEGER_FIELD_ERROR = "PITR activation {name} must be an integer"
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
            "backend",
            "uploader_identity",
            "viewer_identity",
            "store_target",
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
    error_message: str | None = None

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
    def from_json(cls, payload: str) -> ActivationRecord:
        raw_value: object = json.loads(payload)
        fields = strict_decode.object_fields(raw_value, error_type=TypeError, message=_FIELDS_ERROR)
        raw = cls._upgrade_schema(fields)
        record = cls._from_fields(raw)
        # Keep validation order: corrupt records can violate several invariants.
        record._validate_schema_and_timestamps()
        record._validate_config_journals()
        rollback_names = {"archive_mode", "archive_command", "archive_timeout", "wal_compression"}
        record._validate_rollback_journals(rollback_names)
        record._validate_remote_evidence_and_diagnostics()
        record._validate_snapshot_evidence()
        phase_index = _FORWARD_PHASES.index(record.phase) if record.phase in _FORWARD_PHASES else -1
        record._validate_wal_config_evidence(phase_index)
        record._validate_restart_and_wal_evidence(phase_index)
        record._validate_candidate_evidence(phase_index)
        record._validate_rollback_evidence(rollback_names)
        record._validate_manifests()
        return record

    @classmethod
    def _upgrade_schema(cls, raw: dict[str, object]) -> dict[str, object]:
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
        if (
            set(raw) == set(cls.__dataclass_fields__) - {"error_message"}
            and raw.get("schema_version") == 3
        ):
            # v3 -> v4 adds only the optional error_message field; every phase
            # upgrades in place (a base_pending v3 operation must stay
            # resumable across the schema bump).
            raw = {**dict.fromkeys(cls.__dataclass_fields__), **raw}
            raw["schema_version"] = _SCHEMA_VERSION
        evidence = raw.get("pre_activation_credential_evidence")
        if isinstance(evidence, dict) and "viewer_client_email" in evidence:
            # GCS-vocabulary credential evidence (pre-Baidu-backend records):
            # the same identities under backend-neutral keys, so a live
            # activation record stays readable across the backend field (QA #1147).
            legacy = cast(dict[str, object], evidence)
            raw["pre_activation_credential_evidence"] = {
                "backend": "gcs",
                "uploader_identity": str(legacy.get("uploader_client_email") or ""),
                "viewer_identity": str(legacy.get("viewer_client_email") or ""),
                "store_target": str(legacy.get("bucket_name") or ""),
                "object_prefix": str(legacy.get("object_prefix") or ""),
                "backup_key_id": str(legacy.get("backup_key_id") or ""),
                "backup_key_sha256": str(legacy.get("backup_key_sha256") or ""),
            }
        return raw

    @classmethod
    def _from_fields(cls, raw: dict[str, object]) -> ActivationRecord:
        strict_decode.exact_fields(
            raw, cls.__dataclass_fields__, error_type=ValueError, message=_FIELDS_ERROR
        )
        phase = raw["phase"]
        if not isinstance(phase, str) or phase not in _PHASES:
            raise ValueError("unknown PITR activation phase")

        def string(name: str) -> str:
            return strict_decode.strict_string(
                raw, name, error_type=TypeError, message_template=_STRING_FIELD_ERROR
            )

        def opt_string(name: str) -> str | None:
            return strict_decode.optional_string(
                raw, name, error_type=ValueError, message_template=_OPTIONAL_STRING_FIELD_ERROR
            )

        def string_map(name: str) -> dict[str, str] | None:
            value = strict_decode.string_map(
                raw,
                name,
                object_error_type=TypeError,
                object_message_template="PITR activation {name} must be an object or null",
                pairs_error_type=ValueError,
                pairs_message_template="PITR activation {name} must contain string pairs",
            )
            if (
                value is not None
                and name in _EVIDENCE_KEYS
                and frozenset(value) != _EVIDENCE_KEYS[name]
            ):
                raise ValueError(f"PITR activation {name} fields differ")
            return value

        schema_version = strict_decode.strict_int(
            raw, "schema_version", error_type=TypeError, message_template=_INTEGER_FIELD_ERROR
        )
        return cls(
            schema_version=schema_version,
            operation_id=string("operation_id"),
            phase=cast(ActivationPhase, phase),
            started_at=string("started_at"),
            updated_at=string("updated_at"),
            origin=string("origin"),
            pre_activation_snapshot=opt_string("pre_activation_snapshot"),
            pre_activation_pg_settings=string_map("pre_activation_pg_settings"),
            pre_activation_credential_evidence=string_map("pre_activation_credential_evidence"),
            pre_activation_pitr_env=string_map("pre_activation_pitr_env"),
            pre_activation_pg_auto_conf=string_map("pre_activation_pg_auto_conf"),
            pre_activation_env_b64=opt_string("pre_activation_env_b64"),
            pre_activation_env_digest=opt_string("pre_activation_env_digest"),
            pre_activation_auto_conf_b64=opt_string("pre_activation_auto_conf_b64"),
            pre_activation_auto_conf_digest=opt_string("pre_activation_auto_conf_digest"),
            rollback_expected_env_digest=opt_string("rollback_expected_env_digest"),
            rollback_expected_auto_conf_digest=opt_string("rollback_expected_auto_conf_digest"),
            switched_wal=opt_string("switched_wal"),
            protected_manifest=opt_string("protected_manifest"),
            wal_config_before_digest=opt_string("wal_config_before_digest"),
            wal_config_desired_digest=opt_string("wal_config_desired_digest"),
            config_apply_intent=string_map("config_apply_intent"),
            config_apply_applied=string_map("config_apply_applied"),
            rollback_setting_intent=string_map("rollback_setting_intent"),
            rollback_settings_applied=string_map("rollback_settings_applied"),
            restart_handoff=opt_string("restart_handoff"),
            restart_orchestration=opt_string("restart_orchestration"),
            rollback_postmaster_started_at=opt_string("rollback_postmaster_started_at"),
            restart_handoff_consumed_at=opt_string("restart_handoff_consumed_at"),
            restart_dispatch_session=opt_string("restart_dispatch_session"),
            wal_exact_evidence=string_map("wal_exact_evidence"),
            wal_verification_deadline=opt_string("wal_verification_deadline"),
            wal_ack_evidence=string_map("wal_ack_evidence"),
            wal_viewer_proof=string_map("wal_viewer_proof"),
            candidate_digest=opt_string("candidate_digest"),
            candidate_chain_id=opt_string("candidate_chain_id"),
            protected_digest=opt_string("protected_digest"),
            error=opt_string("error"),
            error_code=opt_string("error_code"),
            error_detail=opt_string("error_detail"),
            error_message=opt_string("error_message"),
        )

    @staticmethod
    def _utc_timestamp(value: str, name: str) -> None:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(timestamp):
            raise ValueError(f"PITR activation {name} must be a UTC timestamp")

    def _validate_schema_and_timestamps(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("unsupported PITR activation record schema")
        started = datetime.fromisoformat(self.started_at)
        updated = datetime.fromisoformat(self.updated_at)
        if started.tzinfo is None or updated.tzinfo is None:
            raise ValueError("PITR activation timestamps must carry timezone")
        if started.utcoffset() != UTC.utcoffset(started) or updated.utcoffset() != UTC.utcoffset(
            updated
        ):
            raise ValueError("PITR activation timestamps must be UTC")
        if updated < started:
            raise ValueError("PITR activation updated_at precedes started_at")
        if self.wal_verification_deadline is not None:
            self._utc_timestamp(self.wal_verification_deadline, "wal_verification_deadline")
        if self.wal_ack_evidence is not None:
            self._utc_timestamp(self.wal_ack_evidence["acknowledged_at"], "acknowledged_at")
        if self.wal_viewer_proof is not None:
            self._utc_timestamp(self.wal_viewer_proof["observed_at"], "observed_at")
        if self.wal_exact_evidence is not None:
            self._utc_timestamp(self.wal_exact_evidence["switch_intent_at"], "switch_intent_at")
            exact = self.wal_exact_evidence
            segment = exact["segment"]
            if not re.fullmatch(r"[0-9A-F]{24}", segment) or segment[:8] != (
                f"{int(exact['timeline']):08X}"
            ):
                raise ValueError("PITR WAL intent has a non-canonical segment")

    def _validate_config_journals(self) -> None:
        for journal, label in (
            (self.config_apply_intent, "intent"),
            (self.config_apply_applied, "applied"),
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

    def _validate_rollback_journals(self, rollback_names: set[str]) -> None:
        intent = self.rollback_setting_intent
        intent_fields = {"name", "expected_digest", "current_value", "desired_value"}
        if intent is not None and set(intent) != intent_fields:
            raise ValueError("PITR rollback setting intent fields differ")
        if intent is not None and intent["name"] not in rollback_names:
            raise ValueError("PITR rollback setting intent name is unknown")
        if self.rollback_settings_applied is not None:
            if not set(self.rollback_settings_applied) <= rollback_names:
                raise ValueError("PITR rollback applied setting name is unknown")
            for evidence in self.rollback_settings_applied.values():
                self._validate_rollback_applied(evidence)
        baseline = self.pre_activation_pg_auto_conf or {}
        if intent is not None and intent["desired_value"] != baseline.get(intent["name"]):
            raise ValueError("PITR rollback setting intent differs from baseline")
        if self.rollback_settings_applied is not None and any(
            json.loads(evidence)["desired_value"] != baseline.get(name)
            for name, evidence in self.rollback_settings_applied.items()
        ):
            raise ValueError("PITR rollback applied evidence differs from baseline")

    @staticmethod
    def _validate_rollback_applied(evidence: str) -> None:
        loaded: object = json.loads(evidence)
        if not isinstance(loaded, dict):
            raise TypeError("PITR rollback applied evidence fields differ")
        untyped = cast(dict[object, object], loaded)
        if not all(isinstance(key, str) for key in untyped):
            raise ValueError("PITR rollback applied evidence fields differ")
        value = cast(dict[str, object], untyped)
        if set(value) != {"desired_value", "post_digest"}:
            raise ValueError("PITR rollback applied evidence fields differ")
        desired_value = value["desired_value"]
        post_digest = value["post_digest"]
        if not isinstance(desired_value, str) or not isinstance(post_digest, str):
            raise TypeError("PITR rollback applied evidence fields differ")
        if not re.fullmatch(r"[0-9a-f]{64}", post_digest):
            raise ValueError("PITR rollback applied digest is invalid")

    def _validate_remote_evidence_and_diagnostics(self) -> None:
        if self.wal_ack_evidence is not None and self.wal_viewer_proof is not None:
            validate_wal_remote_evidence(
                ack=self.wal_ack_evidence,
                viewer=self.wal_viewer_proof,
                exact=self.wal_exact_evidence,
                verification_deadline=self.wal_verification_deadline,
                credential_evidence=self.pre_activation_credential_evidence,
            )
        if self.error_code is not None and self.error_code not in {
            "gcs_forbidden",
            "wal_deadline",
            "credential_drift",
            "state_cas",
            "restore_mismatch",
            "restart_failure",
            "activation_failure",
        }:
            raise ValueError("PITR activation error code is unknown")
        if (self.error_code is None) != (self.error_detail is None):
            raise ValueError("PITR activation diagnostics are incomplete")
        if self.pre_activation_credential_evidence is not None and not re.fullmatch(
            r"[0-9a-f]{64}",
            self.pre_activation_credential_evidence["backup_key_sha256"],
        ):
            raise ValueError("PITR backup key fingerprint must be lowercase SHA256")

    def _validate_snapshot_evidence(self) -> None:
        if self.phase in _EVIDENCE_PHASES and (
            not self.pre_activation_snapshot
            or not self.pre_activation_pg_settings
            or not self.pre_activation_credential_evidence
        ):
            raise ValueError("PITR activation phase is missing logical recovery evidence")
        if self.phase == "snapshot_pending" and (
            not self.pre_activation_pg_settings or not self.pre_activation_credential_evidence
        ):
            raise ValueError("PITR activation snapshot phase is missing shadow evidence")

    def _validate_wal_config_evidence(self, phase_index: int) -> None:
        if phase_index >= _FORWARD_PHASES.index("wal_config_applying") and (
            not self.wal_config_before_digest
            or not self.wal_config_desired_digest
            or not self.pre_activation_pitr_env
            or not self.pre_activation_pg_auto_conf
            or self.pre_activation_env_b64 is None
            or not self.pre_activation_env_digest
            or self.pre_activation_auto_conf_b64 is None
            or not self.pre_activation_auto_conf_digest
        ):
            raise ValueError("PITR activation phase is missing WAL config digests")

    def _validate_restart_and_wal_evidence(self, phase_index: int) -> None:
        if phase_index >= _FORWARD_PHASES.index("wal_restart_pending") and (
            not self.restart_handoff
            or not self.restart_orchestration
            or not self.rollback_expected_env_digest
            or not self.rollback_expected_auto_conf_digest
        ):
            raise ValueError("PITR activation phase is missing restart orchestration evidence")
        if (self.restart_handoff_consumed_at is None) != (self.restart_dispatch_session is None):
            raise ValueError("PITR restart handoff binding is incomplete")
        if phase_index >= _FORWARD_PHASES.index("wal_ack_pending") and (
            not self.wal_exact_evidence or not self.wal_verification_deadline
        ):
            raise ValueError("PITR activation phase is missing exact WAL evidence")
        if phase_index >= _FORWARD_PHASES.index("wal_remote_verified") and (
            not self.wal_ack_evidence or not self.wal_viewer_proof
        ):
            raise ValueError("PITR activation phase is missing remote WAL proof")

    def _validate_candidate_evidence(self, phase_index: int) -> None:
        if phase_index >= _FORWARD_PHASES.index("base_pending") and not self.candidate_chain_id:
            raise ValueError("PITR activation phase is missing candidate chain intent")
        if phase_index >= _FORWARD_PHASES.index("restore_pending") and not self.candidate_digest:
            raise ValueError("PITR activation phase is missing candidate digest")
        if self.phase == "protected" and (not self.protected_manifest or not self.protected_digest):
            raise ValueError("protected PITR activation is missing protected evidence")
        if self.phase == "protected" and self.config_apply_intent is not None:
            raise ValueError("protected PITR activation retains a config intent")

    def _validate_rollback_evidence(self, rollback_names: set[str]) -> None:
        if self.phase in {"rollback_pending", "rollback_restart_pending"} and (
            not self.wal_config_before_digest or self._missing_rollback_restart(consumed=False)
        ):
            raise ValueError("PITR rollback phase is missing restart evidence")
        if self.phase == "rollback_restart_pending" and (
            self.rollback_setting_intent is not None
            or set(self.rollback_settings_applied or {}) != rollback_names
        ):
            raise ValueError("PITR rollback restart lacks per-setting applied evidence")
        if (
            self.phase == "rolled_back"
            and self.wal_config_before_digest is not None
            and self._missing_rollback_restart(consumed=True)
        ):
            raise ValueError("mutated PITR rollback is missing full ownership evidence")

    def _missing_rollback_restart(self, *, consumed: bool) -> bool:
        return (
            not self.restart_handoff
            or not self.restart_orchestration
            or (
                consumed
                and (not self.restart_handoff_consumed_at or not self.restart_dispatch_session)
            )
            or not self.rollback_postmaster_started_at
            or not self.rollback_expected_env_digest
            or not self.rollback_expected_auto_conf_digest
            or self._missing_rollback_snapshot()
        )

    def _missing_rollback_snapshot(self) -> bool:
        return (
            self.pre_activation_env_b64 is None
            or not self.pre_activation_env_digest
            or self.pre_activation_auto_conf_b64 is None
            or not self.pre_activation_auto_conf_digest
            or not self.pre_activation_pitr_env
            or not self.pre_activation_pg_auto_conf
            or not self.pre_activation_pg_settings
        )

    def _validate_manifests(self) -> None:
        if self.phase == "restore_pending":
            if self.protected_manifest is None:
                raise ValueError("restore-pending PITR activation lacks candidate manifest")
            candidate = CandidateManifest.from_json(self.protected_manifest)
            canonical = candidate.to_json()
            if (
                candidate.chain_id != self.candidate_chain_id
                or not candidate.chain_id.endswith(f"-{self.operation_id}")
                or not stored_digest_matches(
                    raw=self.protected_manifest,
                    canonical=canonical,
                    expected=cast(str, self.candidate_digest),
                )
            ):
                raise ValueError("restore-pending candidate differs from activation evidence")
        if self.phase == "protected":
            from services.pitr.restore_manifest import ProtectedManifest

            protected = ProtectedManifest.from_json(cast(str, self.protected_manifest))
            canonical = protected.to_json()
            if (
                protected.chain_id != self.candidate_chain_id
                or not protected.chain_id.endswith(f"-{self.operation_id}")
                or not stored_digest_matches(
                    raw=_embedded_candidate_raw(cast(str, self.protected_manifest)),
                    canonical=protected.candidate.to_json(),
                    expected=cast(str, self.candidate_digest),
                )
                or not stored_digest_matches(
                    raw=cast(str, self.protected_manifest),
                    canonical=canonical,
                    expected=cast(str, self.protected_digest),
                )
                or protected.candidate.system_identifier
                != (self.pre_activation_pg_settings or {}).get("system_identifier")
                or protected.candidate.base_object.key_id
                != (self.pre_activation_credential_evidence or {}).get("backup_key_id")
            ):
                raise ValueError("protected manifest differs from activation evidence")

    def advance(self, phase: ActivationPhase, **changes: object) -> ActivationRecord:
        if phase == self.phase:
            if set(changes) - {"error", "error_code", "error_detail", "error_message"}:
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

    def renew_wal_deadline(self, deadline_iso: str) -> ActivationRecord:
        """Re-stamp the WAL verification deadline for THIS attempt.

        The persisted absolute deadline lapsed once an earlier attempt crashed
        (the 2026-08-30 failure: the CLI died at the switch step, nobody ran
        the proof loop, and the non-renewable deadline forced a rollback +
        full re-activation). The deadline is now a per-attempt window; the
        switch intent inside `wal_exact_evidence` stays immutable (the ACK
        lower bound), so a resume re-verifies the SAME target segment under a
        fresh upper bound."""
        if self.phase != "wal_ack_pending":
            raise ValueError("PITR WAL deadline renew is only valid at wal_ack_pending")
        raw = asdict(self)
        raw["wal_verification_deadline"] = deadline_iso
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
