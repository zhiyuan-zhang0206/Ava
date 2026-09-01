"""Regression tests for the `.env` actor audit trail and integrity guard."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from shared import env_audit
from shared import runtime_config as runtime_config
from shared.env_audit import check_env_integrity, last_env_write_record, record_env_write


@pytest.fixture
def audit_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect the audited unit `.env` to a scratch home."""
    monkeypatch.setattr(runtime_config, "_ava_home", lambda: tmp_path)
    return tmp_path


def test_record_env_write_records_metadata_without_config_values(audit_home: Path) -> None:
    """Removing the JSONL append or leaking values makes this regression fail."""
    env_path = audit_home / ".env"
    written_value = "audit-test-value"
    env_path.write_text(f"AVA_MODEL={written_value}\n")

    record_env_write({"AVA_MODEL"}, set(), site="test")

    audit_path = audit_home / ".env.audit.jsonl"
    line = audit_path.read_text()
    record = json.loads(line)
    assert audit_path.stat().st_mode & 0o777 == 0o600
    assert record["site"] == "test"
    assert record["keys_written"] == ["AVA_MODEL"]
    assert record["keys_removed"] == []
    assert record["digest_after"] == hashlib.sha256(env_path.read_bytes()).hexdigest()
    assert {"ts", "pid", "process", "cmdline"} <= set(record)
    assert written_value not in line
    assert last_env_write_record() == record


def test_record_env_write_redacts_command_arguments(audit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A process argument must not let a configuration value enter the audit."""

    class Process:
        def name(self) -> str:
            return "python"

        def cmdline(self) -> list[str]:
            return ["python", "-c", "cluster-secret-value"]

    monkeypatch.setattr(env_audit.psutil, "Process", Process)
    (audit_home / ".env").write_text("AVA_MODEL=test-model\n")

    record_env_write({"AVA_MODEL"}, set(), site="test")

    assert "cluster-secret-value" not in (audit_home / ".env.audit.jsonl").read_text()


def test_audited_runtime_write_leaves_integrity_healthy(audit_home: Path) -> None:
    """Removing the post-write record must make the guard report a mismatch."""
    runtime_config.write_fields({"llm_model": "test-model"}, set(), audit_site="test")

    assert check_env_integrity() is None


def test_audited_runtime_write_records_env_aliases(audit_home: Path) -> None:
    """Recording typed field names instead of shared `.env` keys must fail."""
    runtime_config.write_fields({"llm_model": "test-model"}, set(), audit_site="test")

    record = last_env_write_record()

    assert record is not None
    assert record["keys_written"] == ["AVA_MODEL"]


def test_integrity_guard_records_one_out_of_band_write(audit_home: Path) -> None:
    """Removing anomaly recording or its digest self-rate-limit breaks this test."""
    runtime_config.write_fields({"llm_model": "test-model"}, set(), audit_site="test")
    env_path = audit_home / ".env"
    env_path.write_text(env_path.read_text() + "AVA_EXEC_TIMEOUT_SECONDS=12\n")

    detection = check_env_integrity()

    assert detection is not None
    assert detection["kind"] == "unauthorized"
    assert detection["last_official_site"] == "test"
    assert detection["keys"] == ["AVA_EXEC_TIMEOUT_SECONDS"]
    assert check_env_integrity() is None
    records = [json.loads(line) for line in (audit_home / ".env.audit.jsonl").read_text().splitlines()]
    assert records[-1]["kind"] == "unauthorized"


def test_integrity_guard_is_unarmed_without_an_audit_file(audit_home: Path) -> None:
    """Creating an audit record for a fresh home must make this test fail."""
    (audit_home / ".env").write_text("AVA_MODEL=test-model\n")

    assert check_env_integrity() is None
    assert not (audit_home / ".env.audit.jsonl").exists()


def test_runtime_write_without_audit_site_keeps_audit_unarmed(audit_home: Path) -> None:
    """Ignoring the opt-in audit site must make this compatibility test fail."""
    runtime_config.write_fields({"llm_model": "test-model"}, set())

    assert not (audit_home / ".env.audit.jsonl").exists()
