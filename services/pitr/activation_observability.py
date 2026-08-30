"""Safe status and durable diagnostics for PITR activation."""

from __future__ import annotations

from pathlib import Path

from services.pitr.activation_state import ActivationRecord, load_record, write_record
from shared.log import logger

# Message persistence is scoped to BaseCandidateError: its raise sites carry the
# static, secret-free failure vocabulary of the candidate pipeline (exit codes +
# bounded child stderr — credentials ride env vars, never argv), so the text is
# safe to durably record. Foreign exception types stay type-only: their messages
# can carry credential-bearing conninfo and are not worth the redaction risk
# (2026-08-30: "BaseCandidateError" with no message was the entire record while
# the real FATAL sat in a DEVNULL'd pipe).


def save_error(home: Path, record: ActivationRecord, exc: BaseException) -> None:
    latest = load_record(home)
    if latest is None or latest.operation_id != record.operation_id:
        raise RuntimeError("PITR activation operation changed while recording failure") from exc
    lowered = str(exc).lower()
    if isinstance(exc, PermissionError) or "403" in lowered:
        code, detail = "gcs_forbidden", "GCS denied the activation operation"
    elif "deadline" in lowered:
        code, detail = "wal_deadline", "WAL proof missed its durable deadline"
    elif "credential" in lowered or "bucket evidence" in lowered:
        code, detail = "credential_drift", "credential or remote target evidence drifted"
    elif "cas" in lowered or "state changed" in lowered:
        code, detail = "state_cas", "durable activation ownership changed"
    elif "restore" in lowered or "protected proof" in lowered:
        code, detail = "restore_mismatch", "restore proof did not match the candidate"
    elif "restart" in lowered:
        code, detail = "restart_failure", "typed restart continuation failed"
    else:
        code, detail = "activation_failure", "activation step failed closed"
    logger.exception(
        "PITR activation failed operation_id=%s phase=%s code=%s",
        record.operation_id,
        record.phase,
        code,
    )
    from services.pitr.base_candidate import BaseCandidateError

    message = str(exc)[:500] if isinstance(exc, BaseCandidateError) else None
    write_record(
        home,
        latest.advance(
            latest.phase,
            error=type(exc).__name__,
            error_code=code,
            error_detail=detail,
            error_message=message,
        ),
    )
