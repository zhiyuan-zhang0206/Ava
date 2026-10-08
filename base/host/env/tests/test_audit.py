"""Regression tests for the `.env` actor audit trail and integrity guard."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest

from base.host.env import audit
from base.host.env import runtime_config as runtime_config
from base.host.env.audit import check_env_integrity, last_env_write_record, record_env_write
from base.telemetry import Event


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

    monkeypatch.setattr(audit.psutil, "Process", Process)
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
    assert audit._env_key_names(env_path) == ["AVA_DB_URL"]


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
    from base.host.env.dotenv_file import upsert_env

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
    from base.host.env.dotenv_file import upsert_env

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
    captured: list[Event] = []
    monkeypatch.setattr("base.telemetry.emit_prepared", captured.append)
    runtime_config.write_fields(
        {"llm_model": "m3"}, set(), audit_site="test", actor="user_session:administrator"
    )

    assert captured and captured[0].event_name == "env_write"
    payload = captured[0].attributes
    assert payload["actor"] == "user_session:administrator"
    assert "m3" not in json.dumps(payload)


def test_record_env_write_rejects_invalid_metadata_before_audit_artifacts(
    audit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct recording cannot undo the caller's already-landed bytes."""
    env_path = audit_home / ".env"
    env_path.write_text("AVA_MODEL=new-value\n")

    def invalid() -> dict[str, tuple[str, bool]]:
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(audit, "_load_alias_metadata", invalid)
    with pytest.raises(RuntimeError, match="registry unavailable"):
        record_env_write(
            env_path,
            {"AVA_MODEL"},
            set(),
            site="test",
            changes=[{"alias": "AVA_MODEL", "old": "old-value", "new": "new-value"}],
        )
    assert env_path.read_text() == "AVA_MODEL=new-value\n"
    assert not (audit_home / ".env.audit.jsonl").exists()
    assert not (audit_home / ".env.audit.armed").exists()


def test_alias_metadata_projects_declarations_without_runtime_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base.config
    from base.config.admin import metadata

    def runtime_values() -> dict[str, object]:
        pytest.fail("audit declarations must not read runtime values")

    def panel() -> object:
        pytest.fail("audit declarations must not read panel metadata")

    monkeypatch.setattr(base.config, "current_field_values", runtime_values)
    monkeypatch.setattr(metadata, "get_config_metadata", panel)
    projected = audit._load_alias_metadata()
    assert projected["AVA_MODEL"] == ("cluster-default", False)
    assert projected["AVA_DB_URL"][1] is True


def test_alias_metadata_does_not_cache_a_separate_declaration_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.host.env import config_registry

    declarations = deepcopy(config_registry.fields())
    info = declarations["llm_model"].info
    extra = dict(config_registry.schema_extra(info))
    info.json_schema_extra = extra
    monkeypatch.setattr(config_registry, "fields", lambda: declarations)
    extra["sensitive"] = False
    assert audit._load_alias_metadata()["AVA_MODEL"][1] is False
    extra["sensitive"] = True
    assert audit._load_alias_metadata()["AVA_MODEL"][1] is True


@pytest.mark.parametrize("sensitive", [None, "false", 0])
def test_alias_metadata_rejects_invalid_sensitivity_declarations(
    monkeypatch: pytest.MonkeyPatch, sensitive: object
) -> None:
    from base.host.env import config_registry

    declarations = deepcopy(config_registry.fields())
    info = declarations["llm_model"].info
    info.json_schema_extra = {**config_registry.schema_extra(info), "sensitive": sensitive}
    monkeypatch.setattr(config_registry, "fields", lambda: declarations)
    with pytest.raises(TypeError, match="boolean sensitive"):
        audit._load_alias_metadata()


def test_alias_metadata_rejects_ambiguous_declarations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.host.env import config_registry

    declarations = deepcopy(config_registry.fields())
    declarations["gateway_url"].info.serialization_alias = "AVA_MODEL"
    monkeypatch.setattr(config_registry, "fields", lambda: declarations)
    with pytest.raises(ValueError, match="duplicate env alias"):
        audit._load_alias_metadata()


def test_declaration_projection_works_without_a_runtime_settings_image(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; from base.host.env.audit import _load_alias_metadata; "
            "metadata = _load_alias_metadata(); assert metadata['AVA_DB_URL'][1] is True; "
            "assert 'base.config._full' not in sys.modules; "
            "assert 'base.config.admin.metadata' not in sys.modules",
        ],
        cwd=tmp_path,
        env={
            **{key: value for key, value in os.environ.items() if key != "AVA_CONFIG_BOOT"},
            "AVA_HOME": str(tmp_path / "absent-home"),
            "AVA_CONFIG_FETCH": "skip",
            "AVA_PROCESS_PROFILE": "runner",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("writer", ["fields-update", "fields-remove", "upsert", "remove"])
def test_invalid_metadata_prevents_every_audited_file_write(
    audit_home: Path, monkeypatch: pytest.MonkeyPatch, writer: str
) -> None:
    from base.host.env import dotenv_file

    env_path = audit_home / ".env"
    original = b"AVA_MODEL=before\nUNREGISTERED_SECRET=secret-before\n"
    env_path.write_bytes(original)

    def invalid() -> dict[str, tuple[str, bool]]:
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(audit, "_load_alias_metadata", invalid)
    with pytest.raises(RuntimeError, match="registry unavailable"):
        if writer == "fields-update":
            runtime_config.write_fields({"llm_model": "after"}, set(), audit_site="test")
        elif writer == "fields-remove":
            runtime_config.write_fields({}, {"llm_model"}, audit_site="test")
        elif writer == "upsert":
            dotenv_file.upsert_env(env_path, {"AVA_MODEL": "after"}, audit_site="test")
        else:
            dotenv_file.remove_env(env_path, {"AVA_MODEL"}, audit_site="test")
    assert env_path.read_bytes() == original
    assert not (audit_home / "backups").exists()
    assert not (audit_home / ".env.audit.jsonl").exists()
    assert not (audit_home / ".env.audit.armed").exists()


@pytest.mark.parametrize("writer", ["fields", "upsert", "remove"])
def test_noop_writer_does_not_require_audit_metadata(
    audit_home: Path, monkeypatch: pytest.MonkeyPatch, writer: str
) -> None:
    from base.host.env import dotenv_file

    path = audit_home / ".env"
    original = b"AVA_MODEL=before\n"
    path.write_bytes(original)

    def unexpected() -> dict[str, tuple[str, bool]]:
        pytest.fail("a no-op must not prepare audit metadata")

    monkeypatch.setattr(audit, "_load_alias_metadata", unexpected)
    if writer == "fields":
        runtime_config.write_fields({}, set(), audit_site="test")
    elif writer == "upsert":
        dotenv_file.upsert_env(path, {"AVA_MODEL": "before"}, audit_site="test")
    else:
        dotenv_file.remove_env(path, {"ABSENT"}, audit_site="test")
    assert path.read_bytes() == original
    assert not (audit_home / ".env.audit.jsonl").exists()
    assert not (audit_home / "backups").exists()


def test_stale_digest_fails_before_metadata_preparation(
    audit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = audit_home / ".env"
    original = b"AVA_MODEL=before\n"
    path.write_bytes(original)

    def unexpected() -> dict[str, tuple[str, bool]]:
        pytest.fail("stale CAS must fail before preparing audit metadata")

    monkeypatch.setattr(audit, "_load_alias_metadata", unexpected)
    with pytest.raises(RuntimeError, match="changed before owned"):
        runtime_config.write_fields(
            {"llm_model": "after"}, set(), audit_site="test", expected_digest="stale"
        )
    assert path.read_bytes() == original
    assert not (audit_home / ".env.audit.jsonl").exists()
    assert not (audit_home / "backups").exists()


@pytest.mark.parametrize("writer", ["fields", "upsert", "remove"])
def test_redaction_is_prepared_once_while_the_old_env_is_still_present(
    audit_home: Path, monkeypatch: pytest.MonkeyPatch, writer: str
) -> None:
    from base.host.env import dotenv_file

    env_path = audit_home / ".env"
    original = "AVA_MODEL=before\n"
    env_path.write_text(original)
    reads: list[str] = []

    def declared() -> dict[str, tuple[str, bool]]:
        assert env_path.read_text() == original
        reads.append("prepare")
        return {"AVA_MODEL": ("cluster-default", False)}

    monkeypatch.setattr(audit, "_load_alias_metadata", declared)
    if writer == "fields":
        runtime_config.write_fields({"llm_model": "after"}, set(), audit_site="test")
    elif writer == "upsert":
        dotenv_file.upsert_env(env_path, {"AVA_MODEL": "after"}, audit_site="test")
    else:
        dotenv_file.remove_env(env_path, {"AVA_MODEL"}, audit_site="test")
    assert reads == ["prepare"]
    record = last_env_write_record(env_path)
    assert record is not None
    assert record["changed"] == [
        {
            "alias": "AVA_MODEL",
            "scope": "cluster-default",
            "sensitive": False,
            "old": "before",
            "new": None if writer == "remove" else "after",
        }
    ]


def test_read_env_write_records_returns_newest_first_with_limit(audit_home: Path) -> None:
    env_path = audit_home / ".env"
    env_path.write_text("AVA_MODEL=m\n")
    for i in range(3):
        record_env_write(env_path, {"AVA_MODEL"}, set(), site=f"test-{i}")

    records = audit.read_env_write_records(2)
    assert [record["site"] for record in records] == ["test-2", "test-1"]


def test_read_env_write_records_skips_corrupt_lines_and_missing_history(audit_home: Path) -> None:
    env_path = audit_home / ".env"
    env_path.write_text("AVA_MODEL=m\n")
    record_env_write(env_path, {"AVA_MODEL"}, set(), site="good")
    with (audit_home / ".env.audit.jsonl").open("a") as fh:
        fh.write("{not json\n")
        fh.write("[1, 2]\n")

    records = audit.read_env_write_records(10)
    assert [record["site"] for record in records] == ["good"]

    elsewhere = audit_home / "elsewhere" / ".env"
    elsewhere.parent.mkdir()
    assert audit.read_env_write_records(5, elsewhere) == []


def test_read_env_write_records_reads_beyond_a_full_tail_window(audit_home: Path) -> None:
    """A history larger than the tail window still serves its newest records."""
    audit_path = audit_home / ".env.audit.jsonl"
    with audit_path.open("w") as fh:
        for i in range(400):
            fh.write(json.dumps({"site": f"s{i}", "pad": "x" * 200}) + "\n")

    records = audit.read_env_write_records(3)
    assert [record["site"] for record in records] == ["s399", "s398", "s397"]


def test_read_env_write_records_rejects_nonpositive_limit(audit_home: Path) -> None:
    with pytest.raises(ValueError, match="limit"):
        audit.read_env_write_records(0)
