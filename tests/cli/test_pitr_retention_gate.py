# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false

"""The retention gate commands: arm/disable/status/run-once (task #2150 P3).

These lock the operator surface contract (design v0.3 sections 3.2/3.5/4): the
commands are the only sanctioned writers of the arm carriers, every flip
journals its intent before and its applied state after, `arm` fails closed on a
blocked plan or a stale digest, and `run-once` refuses unless the live carriers
approve the freshly recomputed plan.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from cli.commands import (
    cmd_pitr_retention_arm,
    cmd_pitr_retention_disable,
    cmd_pitr_retention_run_once,
    cmd_pitr_retention_status,
)
from cli.commands import pitr as pitr_commands
from services.pitr import retention_gate, retention_scheduler
from services.pitr.retention_executor import RetentionExecutionSummary
from services.pitr.retention_manifest import (
    PLAN_SCHEMA_VERSION,
    RetentionDecision,
    RetentionObject,
    RetentionPlan,
)
from services.pitr.retention_planner import DryRunResult
from shared import runtime_config
from shared.config.physical_backup import PhysicalBackupSettings

_PIN_TOKEN = "av-test-pin"  # noqa: S105 — opaque test fixture pin, not a secret


def _object(name: str, size: int = 10) -> RetentionObject:
    return RetentionObject(
        object_name=name,
        pin_token=_PIN_TOKEN,
        size=size,
        archive_name=None,
        kind="base",
        checksum_algo="crc32c",
        checksum_value="AAAAAA==",
        metadata=(),
    )


def _decision(name: str, size: int = 10) -> RetentionDecision:
    return RetentionDecision(object=_object(name, size), reason="beyond retention")


def _plan(*, blocked: tuple[str, ...] = ()) -> RetentionPlan:
    retained = (_decision("base/1"), _decision("base/2"))
    eligible = () if blocked else (_decision("old/1"),)
    return RetentionPlan(
        schema_version=PLAN_SCHEMA_VERSION,
        retained_chain_count=2,
        evidence_sha256="ef" * 32,
        protected_chain_ids=("chain-a", "chain-b"),
        unprotected_chain_ids=(),
        oldest_retained_chain_id="chain-a",
        ack_high_water=None,
        blocked_reasons=blocked,
        retained=retained,
        eligible=eligible,
        retained_bytes=sum(item.object.size for item in retained),
        eligible_bytes=sum(item.object.size for item in eligible),
        orphan_sidecars=(),
    )


@pytest.fixture
def gate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the home every layer reads: the CLI, the gate, and the `.env` writer."""
    monkeypatch.setattr(runtime_config, "env_file_path", lambda: tmp_path / ".env")
    monkeypatch.setattr(retention_gate, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(pitr_commands, "ava_home", lambda: tmp_path)
    (tmp_path / "physical-backup" / "retention-plans").mkdir(parents=True)
    return tmp_path


def _write_plan(root: Path, plan: RetentionPlan) -> str:
    (root / "physical-backup" / "retention-plans" / "latest.dry-run.json").write_text(
        plan.to_json()
    )
    return plan.digest()


def _journal_records(root: Path) -> list[dict[str, Any]]:
    path = root / "physical-backup" / "retention-journal" / "journal.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _env_text(root: Path) -> str:
    path = root / ".env"
    return path.read_text() if path.exists() else ""


def test_arm_refuses_when_no_plan(gate_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cmd_pitr_retention_arm(digest="deadbeef", confirm=True) == 1
    assert "no dry-run plan on disk" in capsys.readouterr().err
    assert _env_text(gate_env) == ""


def test_arm_preview_writes_nothing(gate_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    digest = _write_plan(gate_env, _plan())
    assert cmd_pitr_retention_arm(digest=digest, confirm=False) == 0
    out = capsys.readouterr().out
    assert "preview only" in out
    assert digest in out
    assert _env_text(gate_env) == ""


def test_arm_rejects_mismatched_digest(gate_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_plan(gate_env, _plan())
    assert cmd_pitr_retention_arm(digest="deadbeef", confirm=True) == 1
    assert "does not match the plan on disk" in capsys.readouterr().err
    assert _env_text(gate_env) == ""


def test_arm_rejects_blocked_plan(gate_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    digest = _write_plan(gate_env, _plan(blocked=("a chain is unprotected",)))
    assert cmd_pitr_retention_arm(digest=digest, confirm=True) == 1
    assert "blockers" in capsys.readouterr().err
    assert _env_text(gate_env) == ""


def test_arm_writes_carriers_and_journals_both_phases(gate_env: Path) -> None:
    digest = _write_plan(gate_env, _plan())
    assert cmd_pitr_retention_arm(digest=digest, confirm=True) == 0
    env_text = _env_text(gate_env)
    assert "AVA_PITR_RETENTION_DELETE_ARMED='true'" in env_text
    assert f"AVA_PITR_RETENTION_DELETE_APPROVED_DIGEST='{digest}'" in env_text

    carriers = retention_gate.CarrierState.read()
    assert carriers.armed is True
    assert carriers.approved_digest == digest

    records = _journal_records(gate_env)
    assert [(r["event"], r["phase"]) for r in records] == [
        ("arm", "intent"),
        ("arm", "applied"),
    ]
    assert records[0]["before"] == {"armed": None, "approved_digest": None}
    assert records[1]["after"] == {"armed": True, "approved_digest": digest}
    assert records[0]["actor_user"]


def test_disable_clears_carriers_and_journals(gate_env: Path) -> None:
    digest = _write_plan(gate_env, _plan())
    assert cmd_pitr_retention_arm(digest=digest, confirm=True) == 0
    assert cmd_pitr_retention_disable(confirm=True) == 0

    carriers = retention_gate.CarrierState.read()
    assert carriers.armed is None
    assert carriers.approved_digest is None
    assert "AVA_PITR_RETENTION_DELETE_ARMED" not in _env_text(gate_env)

    records = _journal_records(gate_env)
    assert [(r["event"], r["phase"]) for r in records[-2:]] == [
        ("disable", "intent"),
        ("disable", "applied"),
    ]
    assert records[-2]["before"] == {"armed": True, "approved_digest": digest}
    assert records[-1]["after"] == {"armed": None, "approved_digest": None}


def test_disable_preview_writes_nothing(gate_env: Path) -> None:
    assert cmd_pitr_retention_disable(confirm=False) == 0
    assert _env_text(gate_env) == ""


def test_status_degrades_without_daemon(
    gate_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(retention_gate, "read_daemon_record", lambda: None)
    digest = _write_plan(gate_env, _plan())
    assert cmd_pitr_retention_status() == 0
    out = capsys.readouterr().out
    assert "retention gate @" in out
    assert digest in out
    assert "unreachable" in out


def test_run_once_requires_armed(gate_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cmd_pitr_retention_run_once(confirm=True) == 1
    assert "not armed" in capsys.readouterr().err


def test_run_once_preview_writes_nothing(
    gate_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    digest = _write_plan(gate_env, _plan())
    assert cmd_pitr_retention_arm(digest=digest, confirm=True) == 0
    capsys.readouterr()
    assert cmd_pitr_retention_run_once(confirm=False) == 0
    assert "preview only" in capsys.readouterr().out


def _fake_config() -> PhysicalBackupSettings:
    """A duck-typed stand-in: run_operator_once reads only these two fields."""
    return cast(
        PhysicalBackupSettings,
        SimpleNamespace(pitr_store_backend="oss", pitr_retained_weekly_chains=2),
    )


class _FakeViewer:
    def stat(self, object_name: str) -> object:
        return None


class _FakeGroup:
    def retention_inventory_reader(self) -> object:
        return object()

    def retention_delete_store(self) -> object:
        return object()

    def viewer_object_store(self) -> _FakeViewer:
        return _FakeViewer()


def _recheck(digest: str) -> DryRunResult:
    return DryRunResult(
        path=Path("plan.json"),
        digest=digest,
        blocked=False,
        retained_objects=2,
        eligible_objects=1,
        retained_bytes=20,
        eligible_bytes=10,
        remote_object_count=3,
        remote_bytes=100,
    )


def test_run_operator_once_refuses_unarmed(gate_env: Path) -> None:
    with pytest.raises(ValueError, match="not armed"):
        retention_scheduler.run_operator_once(_fake_config())
    assert _journal_records(gate_env) == []


def test_run_operator_once_refuses_changed_digest(
    gate_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _write_plan(gate_env, _plan())
    assert cmd_pitr_retention_arm(digest=digest, confirm=True) == 0
    monkeypatch.setattr(retention_scheduler, "get_store_group", _FakeGroup)
    monkeypatch.setattr(
        retention_scheduler,
        "write_dry_run_plan",
        lambda _root, **_kw: _recheck("some-other-digest"),
    )
    with pytest.raises(ValueError, match="differs from the approved digest"):
        retention_scheduler.run_operator_once(_fake_config())
    assert _journal_records(gate_env)[-1]["phase"] == "refused"


def test_run_operator_once_executes_through_the_executor(
    gate_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = _write_plan(gate_env, _plan())
    assert cmd_pitr_retention_arm(digest=digest, confirm=True) == 0
    monkeypatch.setattr(retention_scheduler, "get_store_group", _FakeGroup)
    monkeypatch.setattr(
        retention_scheduler, "write_dry_run_plan", lambda _root, **_kw: _recheck(digest)
    )
    monkeypatch.setattr(retention_scheduler, "inspect_dry_run_plan", lambda _root: object())
    captured: dict[str, Any] = {}

    def fake_execute(plan: object, **kwargs: Any) -> RetentionExecutionSummary:
        captured.update(kwargs)
        return RetentionExecutionSummary(
            plan_digest=digest,
            refused_reason=None,
            attempted=2,
            deleted=1,
            absent=1,
            mismatched=0,
            failed=0,
            verify_failed=0,
            skipped=0,
        )

    monkeypatch.setattr(retention_scheduler, "execute_retention_plan", fake_execute)
    summary = retention_scheduler.run_operator_once(_fake_config())
    assert summary.deleted == 1
    assert captured["expected_digest"] == digest
    assert captured["remote_total_bytes"] == 100
    events = [(r["event"], r["phase"]) for r in _journal_records(gate_env)]
    assert ("run-once", "intent") in events
    assert ("run-once", "result") in events


def test_run_once_command_reports_executor_counts(
    gate_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    digest = _write_plan(gate_env, _plan())
    assert cmd_pitr_retention_arm(digest=digest, confirm=True) == 0

    def fake_run(config: object) -> RetentionExecutionSummary:
        return RetentionExecutionSummary(
            plan_digest=digest,
            refused_reason=None,
            attempted=2,
            deleted=2,
            absent=0,
            mismatched=0,
            failed=0,
            verify_failed=0,
            skipped=1,
        )

    monkeypatch.setattr(retention_scheduler, "run_operator_once", fake_run)
    assert cmd_pitr_retention_run_once(confirm=True) == 0
    out = capsys.readouterr().out
    assert "deleted=2" in out
    assert "skipped=1" in out


def test_inspect_reports_the_live_gate_state(
    gate_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`retention inspect` no longer hardcodes the gate off (P1c stub)."""
    from cli.commands import cmd_pitr_retention_inspect

    digest = _write_plan(gate_env, _plan())
    assert cmd_pitr_retention_inspect() == 0
    assert json.loads(capsys.readouterr().out)["delete_enabled"] is False
    assert cmd_pitr_retention_arm(digest=digest, confirm=True) == 0
    capsys.readouterr()
    assert cmd_pitr_retention_inspect() == 0
    assert json.loads(capsys.readouterr().out)["delete_enabled"] is True
