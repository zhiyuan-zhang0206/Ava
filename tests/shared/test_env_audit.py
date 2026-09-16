"""Regression tests for the `.env` actor audit trail and integrity guard."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

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

    record_env_write(env_path, {"AVA_MODEL"}, set(), site="test")

    audit_path = audit_home / ".env.audit.jsonl"
    armed_path = audit_home / ".env.audit.armed"
    line = audit_path.read_text()
    record = json.loads(line)
    assert audit_path.stat().st_mode & 0o777 == 0o600
    assert armed_path.stat().st_mode & 0o777 == 0o600
    assert record["site"] == "test"
    assert record["keys_written"] == ["AVA_MODEL"]
    assert record["keys_removed"] == []
    assert record["digest_after"] == hashlib.sha256(env_path.read_bytes()).hexdigest()
    assert {"ts", "pid", "process", "cmdline"} <= set(record)
    assert written_value not in line
    assert last_env_write_record() == record


def test_record_env_write_redacts_command_arguments(
    audit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process argument must not let a configuration value enter the audit."""

    class Process:
        def name(self) -> str:
            return "python"

        def cmdline(self) -> list[str]:
            return ["python", "-c", "cluster-secret-value"]

    monkeypatch.setattr(env_audit.psutil, "Process", Process)
    (audit_home / ".env").write_text("AVA_MODEL=test-model\n")

    record_env_write(audit_home / ".env", {"AVA_MODEL"}, set(), site="test")

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


def test_armed_guard_rebuilds_deleted_audit_history(audit_home: Path) -> None:
    """Deleting history after an official write must be reported and repaired."""
    runtime_config.write_fields({"llm_model": "test-model"}, set(), audit_site="test")
    (audit_home / ".env.audit.jsonl").unlink()

    detection = check_env_integrity()

    assert detection is not None
    assert detection["kind"] == "unauthorized"
    assert detection["reason"] == "audit_history_missing"
    assert last_env_write_record() == detection
    assert check_env_integrity() is None


def test_armed_guard_rebuilds_empty_audit_history(audit_home: Path) -> None:
    """An empty history must not recreate the fresh-home unarmed branch."""
    runtime_config.write_fields({"llm_model": "test-model"}, set(), audit_site="test")
    (audit_home / ".env.audit.jsonl").write_text("")

    detection = check_env_integrity()

    assert detection is not None
    assert detection["reason"] == "audit_history_empty"
    assert last_env_write_record() == detection
    assert check_env_integrity() is None


def test_armed_guard_rebuilds_corrupt_audit_history(audit_home: Path) -> None:
    """A malformed final JSONL line must be reported and replaced safely."""
    runtime_config.write_fields({"llm_model": "test-model"}, set(), audit_site="test")
    (audit_home / ".env.audit.jsonl").write_text("not-json\n")

    detection = check_env_integrity()

    assert detection is not None
    assert detection["reason"] == "audit_history_corrupt"
    assert last_env_write_record() == detection
    assert check_env_integrity() is None


def test_armed_guard_rebuilds_history_without_a_digest(audit_home: Path) -> None:
    """A record without its digest must not become an implicit unarmed state."""
    runtime_config.write_fields({"llm_model": "test-model"}, set(), audit_site="test")
    (audit_home / ".env.audit.jsonl").write_text('{"site":"test"}\n')

    detection = check_env_integrity()

    assert detection is not None
    assert detection["reason"] == "audit_history_missing_digest"
    assert last_env_write_record() == detection
    assert check_env_integrity() is None


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
    records = [
        json.loads(line) for line in (audit_home / ".env.audit.jsonl").read_text().splitlines()
    ]
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


def test_empty_audited_runtime_write_keeps_audit_unarmed(audit_home: Path) -> None:
    """An empty patch must not create an actor record or armed marker."""
    runtime_config.write_fields({}, set(), audit_site="test")

    assert not (audit_home / ".env.audit.jsonl").exists()
    assert not (audit_home / ".env.audit.armed").exists()


def test_record_env_write_tolerates_non_utf8_env_bytes(audit_home: Path) -> None:
    """A malformed non-value byte must not prevent the record from landing."""
    env_path = audit_home / ".env"
    env_path.write_bytes(b"AVA_MODEL=test-model\n\xff\n")

    record_env_write(env_path, {"AVA_MODEL"}, set(), site="test")

    record = last_env_write_record()
    assert record is not None
    assert record["keys_after"] == ["AVA_MODEL"]


def test_env_key_names_read_export_prefixed_assignments(audit_home: Path) -> None:
    """The audited key-name surface matches the settings parser: `export KEY=v`
    records KEY (never `export KEY`), and comments/bare keys stay out (#2981)."""
    env_path = audit_home / ".env"
    env_path.write_text("export AVA_DB_URL=postgresql://x\n# export AVA_SKIP_ME=1\nBARE_KEY\n")
    assert env_audit._env_key_names(env_path) == ["AVA_DB_URL"]


def test_record_env_write_redacts_sensitive_and_unregistered_changes(audit_home: Path) -> None:
    """The v2 `changed` diff keeps values only for `sensitive: false` fields."""
    env_path = audit_home / ".env"
    env_path.write_text("AVA_MODEL=test-model\nANTHROPIC_API_KEY=sk-old\nEXTRA_UNKNOWN_KEY=x\n")
    record_env_write(
        env_path,
        {"AVA_MODEL", "ANTHROPIC_API_KEY", "EXTRA_UNKNOWN_KEY"},
        set(),
        site="test",
        actor="user_session:administrator",
        trace_id="trace-1",
        changes=[
            {"alias": "AVA_MODEL", "old": "old-model", "new": "test-model"},
            {"alias": "ANTHROPIC_API_KEY", "old": "sk-old", "new": "sk-SECRET-NEW"},
            {"alias": "EXTRA_UNKNOWN_KEY", "old": "x", "new": "y"},
        ],
    )

    line = (audit_home / ".env.audit.jsonl").read_text()
    record = last_env_write_record()
    assert record is not None
    assert record["actor"] == "user_session:administrator"
    assert record["trace_id"] == "trace-1"
    entries = cast("list[dict[str, object]]", record["changed"])
    changed = {str(entry["alias"]): entry for entry in entries}
    assert changed["AVA_MODEL"] == {
        "alias": "AVA_MODEL",
        "scope": "cluster-default",
        "sensitive": False,
        "old": "old-model",
        "new": "test-model",
    }
    assert changed["ANTHROPIC_API_KEY"]["sensitive"] is True
    assert changed["ANTHROPIC_API_KEY"]["old"] is None
    assert changed["ANTHROPIC_API_KEY"]["new"] is None
    assert changed["EXTRA_UNKNOWN_KEY"]["scope"] is None
    assert changed["EXTRA_UNKNOWN_KEY"]["sensitive"] is None
    assert "sk-SECRET-NEW" not in line and "sk-old" not in line


def test_write_fields_records_old_to_new_values(audit_home: Path) -> None:
    """A second write records the actual old→new transition and the actor."""
    runtime_config.write_fields({"llm_model": "m1"}, set(), audit_site="test")
    runtime_config.write_fields(
        {"llm_model": "m2"}, set(), audit_site="test", actor="cluster_bearer:administrator"
    )

    records = [
        json.loads(line) for line in (audit_home / ".env.audit.jsonl").read_text().splitlines()
    ]
    assert records[-1]["actor"] == "cluster_bearer:administrator"
    assert records[-1]["changed"] == [
        {
            "alias": "AVA_MODEL",
            "scope": "cluster-default",
            "sensitive": False,
            "old": "m1",
            "new": "m2",
        }
    ]


def test_noop_upsert_leaves_no_record_and_a_real_change_still_records(audit_home: Path) -> None:
    """A repeated byte-identical upsert must not manufacture an audit record.

    This is the WSL converge noise of task #3637: converge presents the same app
    port on every `ava start`, and a boot-retry storm re-runs that start per
    attempt. The change chain stays whole — a real change records its old→new
    diff, and the integrity guard sees a consistent file after both paths.
    """
    from shared.envfile import upsert_env

    env_path = audit_home / ".env"
    env_path.write_text("AVA_MODEL=first-model\n")
    audit_path = audit_home / ".env.audit.jsonl"

    upsert_env(env_path, {"AVA_MODEL": "second-model"}, audit_site="test")
    records_after_change = audit_path.read_text().splitlines()
    assert len(records_after_change) == 1

    upsert_env(env_path, {"AVA_MODEL": "second-model"}, audit_site="converge_gateway_port")

    assert audit_path.read_text().splitlines() == records_after_change
    assert env_path.read_text() == "AVA_MODEL=second-model\n"
    assert check_env_integrity() is None  # the skip left the recorded digest standing

    upsert_env(env_path, {"AVA_MODEL": "first-model"}, audit_site="test")

    record = last_env_write_record()
    assert record is not None
    assert record["keys_written"] == ["AVA_MODEL"]
    assert record["changed"] == [
        {
            "alias": "AVA_MODEL",
            "scope": "cluster-default",
            "sensitive": False,
            "old": "second-model",
            "new": "first-model",
        }
    ]
    assert check_env_integrity() is None


def test_noop_upsert_does_not_arm_a_fresh_home(audit_home: Path) -> None:
    """A no-op is not a write: it neither records nor arms.

    A fresh home arms at its first CHANGING write — the deliberate semantics of
    the skip (task #3637); the no-op cannot vouch for a file it did not write.
    """
    from shared.envfile import upsert_env

    env_path = audit_home / ".env"
    env_path.write_text("AVA_MODEL=test-model\n")

    upsert_env(env_path, {"AVA_MODEL": "test-model"}, audit_site="converge_gateway_port")

    assert not (audit_home / ".env.audit.jsonl").exists()
    assert not (audit_home / ".env.audit.armed").exists()

    upsert_env(env_path, {"AVA_MODEL": "other-model"}, audit_site="converge_gateway_port")

    assert (audit_home / ".env.audit.armed").exists()


def test_write_fields_withholds_sensitive_values(audit_home: Path) -> None:
    """A secret write lands by name only — its value never reaches the JSONL."""
    runtime_config.write_fields({"deepseek_api_key": "sk-SECRET-VALUE"}, set(), audit_site="test")

    line = (audit_home / ".env.audit.jsonl").read_text()
    assert "sk-SECRET-VALUE" not in line
    record = last_env_write_record()
    assert record is not None
    entry = cast("list[dict[str, object]]", record["changed"])[0]
    assert entry["sensitive"] is True
    assert entry["old"] is None and entry["new"] is None


def test_env_write_event_carries_actor_without_values(
    audit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The value-free event stream also gains the initiating actor."""
    captured: list[dict[str, object]] = []

    def _capture(**kwargs: object) -> None:
        captured.append(kwargs)

    monkeypatch.setattr("shared.audit_events.insert_event_log", _capture)
    runtime_config.write_fields(
        {"llm_model": "m3"}, set(), audit_site="test", actor="user_session:administrator"
    )

    assert captured and captured[0]["event_type"] == "env_write"
    payload = cast("dict[str, object]", captured[0]["payload"])
    assert payload["actor"] == "user_session:administrator"
    assert "m3" not in json.dumps(captured)


def test_record_env_write_withholds_every_value_when_metadata_fails(
    audit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: with the config registry unavailable, even a normally-recordable
    field's values are withheld (names only), and no value text reaches the file.

    The loader is patched (not `get_config_metadata`) because a successful load is
    cached per process — patching the loader keeps the failure deterministic whatever
    order the suite runs in.
    """
    env_path = audit_home / ".env"
    env_path.write_text("AVA_MODEL=new-value\n")

    def _boom() -> dict[str, tuple[str, bool]]:
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(env_audit, "_load_alias_metadata", _boom)
    record_env_write(
        env_path,
        {"AVA_MODEL"},
        set(),
        site="test",
        changes=[{"alias": "AVA_MODEL", "old": "old-value", "new": "new-value"}],
    )

    record = last_env_write_record(env_path)
    assert record is not None
    assert record["changed"] == [
        {"alias": "AVA_MODEL", "scope": None, "sensitive": None, "old": None, "new": None}
    ]
    raw = (audit_home / ".env.audit.jsonl").read_text()
    assert "old-value" not in raw
    assert "new-value" not in raw


def test_read_env_write_records_returns_newest_first_with_limit(audit_home: Path) -> None:
    env_path = audit_home / ".env"
    env_path.write_text("AVA_MODEL=m\n")
    for i in range(3):
        record_env_write(env_path, {"AVA_MODEL"}, set(), site=f"test-{i}")

    records = env_audit.read_env_write_records(2)
    assert [record["site"] for record in records] == ["test-2", "test-1"]


def test_read_env_write_records_skips_corrupt_lines_and_missing_history(audit_home: Path) -> None:
    env_path = audit_home / ".env"
    env_path.write_text("AVA_MODEL=m\n")
    record_env_write(env_path, {"AVA_MODEL"}, set(), site="good")
    with (audit_home / ".env.audit.jsonl").open("a") as fh:
        fh.write("{not json\n")
        fh.write("[1, 2]\n")

    records = env_audit.read_env_write_records(10)
    assert [record["site"] for record in records] == ["good"]

    elsewhere = audit_home / "elsewhere" / ".env"
    elsewhere.parent.mkdir()
    assert env_audit.read_env_write_records(5, elsewhere) == []


def test_read_env_write_records_reads_beyond_a_full_tail_window(audit_home: Path) -> None:
    """A history larger than the tail window still serves its newest records."""
    audit_path = audit_home / ".env.audit.jsonl"
    with audit_path.open("w") as fh:
        for i in range(400):
            fh.write(json.dumps({"site": f"s{i}", "pad": "x" * 200}) + "\n")

    records = env_audit.read_env_write_records(3)
    assert [record["site"] for record in records] == ["s399", "s398", "s397"]


def test_read_env_write_records_rejects_nonpositive_limit(audit_home: Path) -> None:
    with pytest.raises(ValueError, match="limit"):
        env_audit.read_env_write_records(0)
