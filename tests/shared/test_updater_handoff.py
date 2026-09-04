# pyright: reportUnknownArgumentType=warning, reportUnknownLambdaType=warning
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import psutil
import pytest

from shared import updater_handoff as handoff
from shared import updater_recovery as recovery
from shared.managed_writer_barrier import RolloutIdentity
from shared.managed_writer_observation import (
    ExpectedProcess,
    ExpectedUnitWriters,
    ObservationChallenge,
)
from shared.managed_writer_publication import (
    NormalService,
    NormalServiceReadback,
    PublishedUnit,
    SelectorReadback,
    UnitActivationReadback,
)


def _bootstrap_journal(stage: str, *, normal_release_planned: bool = False) -> dict[str, object]:
    return {
        "request": "/unit/run/bootstrap.json",
        "request_digest": "a" * 64,
        "inventory_digest": "b" * 64,
        "candidate_context_digest": "c" * 64,
        "recovery_context_digest": "d" * 64,
        "normal_release_planned": normal_release_planned,
        "stage": stage,
        "cron": "",
        "phases": [
            {
                "stage": stage,
                "observed_at": "2026-09-04T00:00:00Z",
                "monotonic_s": 0.0,
                "pid": 1,
                "elapsed_s": None,
            }
        ],
        "normal_release": None,
    }


def _normal_journal(stage: str) -> dict[str, object]:
    now = dt.datetime.now(dt.UTC)
    unit = PublishedUnit(
        machine="machine",
        home="/unit",
        inventory_digest="1" * 64,
        prepared_receipt_digest="2" * 64,
        artifact_digest="3" * 64,
        manifest_digest="4" * 64,
    )
    process = ExpectedProcess(pid=1, create_time=1.0, starttime=1)
    service = NormalService(
        session="ava-ops",
        module="services.agent_ops.daemon",
        executable="/unit/releases/" + "3" * 64 + "/python/bin/python",
        entrypoint="/unit/releases/" + "3" * 64 + "/venv/services/agent_ops/daemon.py",
        command_digest="5" * 64,
    )
    challenge = UUID(int=1)
    readback = UnitActivationReadback(
        selector=SelectorReadback(
            unit=unit,
            challenge=challenge,
            previous_digest=None,
            current_digest="6" * 64,
            observed_at=now,
            valid_until=now + dt.timedelta(minutes=1),
        ),
        services=(
            NormalServiceReadback(
                service=service,
                supervisor=process,
                child=process,
                loaded_module=service.entrypoint,
                executable=service.executable,
                entrypoint=service.entrypoint,
                artifact_digest=unit.artifact_digest,
                manifest_digest=unit.manifest_digest,
                readiness="normal",
                challenge=challenge,
                observed_at=now,
                valid_until=now + dt.timedelta(minutes=1),
                observation_digest="7" * 64,
            ),
        ),
    )
    payload: dict[str, object] = {
        "request_path": "/unit/run/normal.json",
        "operation_context": recovery.PreparedObservationRecovery(
            expected=ExpectedUnitWriters(
                machine="machine",
                home="/unit",
                artifact_digest=unit.artifact_digest,
                manifest_digest=unit.manifest_digest,
                processes=(),
                sessions=(),
                launchers=(),
            ),
            operation=RolloutIdentity(holder="holder", acquired_at=now, target_sha="8" * 40),
            challenge=ObservationChallenge(
                challenge=challenge, valid_until=now + dt.timedelta(minutes=1)
            ),
            schema_digest="9" * 64,
        ),
        "unit": unit,
        "previous_selector": None,
        "stage": stage,
        "starting_session": "ava-ops" if stage == "starting" else None,
        "readback": readback if stage in {"observed", "committed"} else None,
    }
    return recovery.NormalReleaseRecoveryJournal.model_validate(payload).model_dump(mode="json")


def _retained_bootstrap(stage: str, *, normal_release_planned: bool = False) -> None:
    handoff.begin(expected_session="ava-updater", generation="bootstrap")
    assert handoff.claim_running("bootstrap", expected_session="ava-updater")
    handoff.write_bootstrap_recovery(
        "bootstrap", _bootstrap_journal(stage, normal_release_planned=normal_release_planned)
    )


def _normal_at(base: dict[str, object], stage: str) -> dict[str, object]:
    payload = json.loads(json.dumps(base))
    payload["stage"] = stage
    payload["starting_session"] = "ava-ops" if stage == "starting" else None
    payload["readback"] = (
        _normal_journal("observed")["readback"] if stage in {"observed", "committed"} else None
    )
    return recovery.NormalReleaseRecoveryJournal.model_validate_json(
        json.dumps(payload)
    ).model_dump(mode="json")


def _write_normal_through(stage: str) -> None:
    sequence = ["waiting", "selected", "bootstrap_stopped", "starting", "observed", "committed"]
    base = _normal_journal("waiting")
    observed_readback: object | None = None
    for item in sequence:
        payload = _normal_at(base, item)
        if observed_readback is not None:
            payload["readback"] = observed_readback
        handoff.write_normal_release_recovery("bootstrap", payload)
        observed_readback = payload["readback"]
        if item == stage:
            return
    raise AssertionError(f"unknown normal stage: {stage}")


def test_unfinished_bootstrap_cannot_be_cleared_or_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _retained_bootstrap("old_stopped")
    before = handoff.bootstrap_state_path().read_bytes()
    assert not handoff.clear("bootstrap")
    assert not handoff.force_clear()
    monkeypatch.setattr(handoff, "owner_is_live", lambda _: False)
    with pytest.raises(handoff.UpdaterHandoffActive):
        handoff.begin(expected_session="another-updater")
    assert handoff.bootstrap_state_path().read_bytes() == before


def test_bootstrap_resume_requires_dead_owner_and_preserves_compensation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _retained_bootstrap("candidate_starting")
    assert not handoff.resume_bootstrap("bootstrap", expected_session="recovery")
    monkeypatch.setattr(handoff, "owner_is_live", lambda _: False)
    assert not handoff.resume_bootstrap("other-generation", expected_session="recovery")
    assert handoff.resume_bootstrap("bootstrap", expected_session="recovery")
    raw = handoff.read_bootstrap_recovery()
    assert raw is not None
    assert raw["journal"] == _bootstrap_journal("candidate_starting")
    snapshot = handoff.read()
    assert snapshot.owner_pid == os.getpid()
    assert snapshot.owner_create_time == psutil.Process().create_time()
    assert snapshot.expected_session == "recovery"


@pytest.mark.parametrize("stage", ["candidate_ready", "recovered"])
def test_only_terminal_bootstrap_can_complete(stage: str) -> None:
    _retained_bootstrap(stage)
    assert handoff.clear("bootstrap")
    assert handoff.read().status == "inactive"


def test_unreadable_ordinary_handoff_is_recoverable_without_bootstrap_evidence() -> None:
    path = handoff.state_path()
    path.write_text("{unfinished recovery record")
    assert handoff.force_clear()
    assert not path.exists()


def test_malformed_versioned_bootstrap_evidence_is_retained() -> None:
    _retained_bootstrap("old_stopped")
    path = handoff.bootstrap_state_path()
    path.write_text('{"version":2,"generation":"bootstrap"}')
    assert not handoff.force_clear()
    assert path.read_text() == '{"version":2,"generation":"bootstrap"}'
    with pytest.raises(handoff.UpdaterHandoffActive):
        handoff.begin(expected_session="another-updater")


def test_bootstrap_evidence_has_a_hard_encoded_budget() -> None:
    handoff.begin(expected_session="ava-updater", generation="bootstrap")
    assert handoff.claim_running("bootstrap", expected_session="ava-updater")
    with pytest.raises(handoff.BootstrapRecoveryInvalidError, match="evidence budget"):
        oversized = _bootstrap_journal("old_stopped")
        oversized["cron"] = "x" * (300 * 1024)
        handoff.write_bootstrap_recovery("bootstrap", oversized)
    assert not handoff.bootstrap_state_path().exists()


def test_bootstrap_takeover_is_exact_dead_predecessor_cas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff.begin(expected_session="old", generation="old")
    assert handoff.claim_running("old", expected_session="old")
    predecessor = handoff.read()
    monkeypatch.setattr(handoff, "owner_is_live", lambda _: False)

    mismatched = replace(predecessor, generation="replacement")
    assert handoff.begin_bootstrap_after_dead_owner(mismatched, expected_session="new") is None
    claimed = handoff.begin_bootstrap_after_dead_owner(predecessor, expected_session="new")
    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.owner_pid == os.getpid()


@pytest.mark.parametrize(
    "stage", ["waiting", "selected", "bootstrap_stopped", "starting", "observed"]
)
def test_normal_release_retains_exact_recovery_record(
    stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _retained_bootstrap("candidate_ready", normal_release_planned=True)
    _write_normal_through(stage)
    path = handoff.bootstrap_state_path()
    before = path.read_bytes()
    assert not handoff.clear("bootstrap")
    assert not handoff.force_clear()
    assert not handoff.allows_generic_recovery(handoff.read())
    monkeypatch.setattr(handoff, "owner_is_live", lambda _: False)
    with pytest.raises(handoff.UpdaterHandoffActive):
        handoff.begin(expected_session="replacement")
    assert path.read_bytes() == before


def test_only_committed_normal_release_can_clear() -> None:
    _retained_bootstrap("candidate_ready", normal_release_planned=True)
    _write_normal_through("committed")
    assert handoff.clear("bootstrap")
    assert not handoff.state_path().exists()
    assert not handoff.bootstrap_state_path().exists()


def test_partial_committed_normal_release_is_retained_as_malformed() -> None:
    _retained_bootstrap("candidate_ready", normal_release_planned=True)
    path = handoff.bootstrap_state_path()
    envelope = json.loads(path.read_text())
    envelope["journal"]["normal_release"] = {"stage": "committed"}
    path.write_text(json.dumps(envelope))
    before = path.read_bytes()
    with pytest.raises(handoff.BootstrapRecoveryInvalidError, match="malformed"):
        handoff.read_bootstrap_recovery()
    assert not handoff.clear("bootstrap")
    assert not handoff.allows_generic_recovery(handoff.read())
    assert path.read_bytes() == before


def test_complete_but_incoherent_terminal_recovery_is_retained() -> None:
    _retained_bootstrap("candidate_ready", normal_release_planned=True)
    _write_normal_through("committed")
    path = handoff.bootstrap_state_path()
    envelope = json.loads(path.read_text())
    envelope["journal"]["stage"] = "recovered"
    envelope["journal"]["normal_release_planned"] = False
    envelope["journal"]["phases"][-1]["stage"] = "recovered"
    path.write_text(json.dumps(envelope))
    before = path.read_bytes()
    with pytest.raises(handoff.BootstrapRecoveryInvalidError, match="malformed"):
        handoff.read_bootstrap_recovery()
    assert not handoff.clear("bootstrap")
    assert not handoff.allows_generic_recovery(handoff.read())
    assert path.read_bytes() == before


def test_normal_release_recovery_requires_candidate_ready_bootstrap() -> None:
    _retained_bootstrap("candidate_started")
    before = handoff.bootstrap_state_path().read_bytes()
    with pytest.raises(handoff.BootstrapRecoveryInvalidError, match="candidate-ready"):
        handoff.write_normal_release_recovery("bootstrap", {"stage": "waiting"})
    assert handoff.bootstrap_state_path().read_bytes() == before


def test_bootstrap_writer_cannot_discard_retained_normal_recovery() -> None:
    _retained_bootstrap("candidate_ready", normal_release_planned=True)
    handoff.write_normal_release_recovery("bootstrap", _normal_journal("waiting"))
    before = handoff.bootstrap_state_path().read_bytes()
    with pytest.raises(handoff.BootstrapRecoveryInvalidError, match="retained normal"):
        handoff.write_bootstrap_recovery(
            "bootstrap", _bootstrap_journal("candidate_ready", normal_release_planned=True)
        )
    assert handoff.bootstrap_state_path().read_bytes() == before


def test_bootstrap_writer_preserves_plan_identity_and_appends_phase() -> None:
    _retained_bootstrap("prepared", normal_release_planned=True)
    changed = _bootstrap_journal("cron_quiesced")
    prepared_phases = _bootstrap_journal("prepared", normal_release_planned=True)["phases"]
    assert isinstance(prepared_phases, list)
    changed["phases"] = [
        *prepared_phases,
        {
            "stage": "cron_quiesced",
            "observed_at": dt.datetime.now(dt.UTC).isoformat(),
            "monotonic_s": 1.0,
            "pid": os.getpid(),
            "elapsed_s": None,
        },
    ]
    with pytest.raises(handoff.BootstrapRecoveryInvalidError, match="identity changed"):
        handoff.write_bootstrap_recovery("bootstrap", changed)
    changed["normal_release_planned"] = True
    handoff.write_bootstrap_recovery("bootstrap", changed)
    with pytest.raises(handoff.BootstrapRecoveryInvalidError, match="append exactly one"):
        handoff.write_bootstrap_recovery("bootstrap", changed)


def test_normal_recovery_rejects_identity_changes_and_phase_rollback() -> None:
    _retained_bootstrap("candidate_ready", normal_release_planned=True)
    base = _normal_journal("waiting")
    handoff.write_normal_release_recovery("bootstrap", base)
    selected = _normal_at(base, "selected")
    changed = json.loads(json.dumps(selected))
    changed["request_path"] = "/unit/run/replacement.json"
    with pytest.raises(handoff.BootstrapRecoveryInvalidError, match="identity changed"):
        handoff.write_normal_release_recovery("bootstrap", changed)
    handoff.write_normal_release_recovery("bootstrap", selected)
    with pytest.raises(handoff.BootstrapRecoveryInvalidError, match="cannot transition"):
        handoff.write_normal_release_recovery("bootstrap", base)


def test_normal_release_recovery_requires_planned_continuation() -> None:
    _retained_bootstrap("candidate_ready")
    before = handoff.bootstrap_state_path().read_bytes()
    with pytest.raises(handoff.BootstrapRecoveryInvalidError, match="did not plan"):
        handoff.write_normal_release_recovery("bootstrap", _normal_journal("waiting"))
    assert handoff.bootstrap_state_path().read_bytes() == before


def test_planned_normal_release_blocks_clear_before_its_first_journal_write() -> None:
    _retained_bootstrap("candidate_ready", normal_release_planned=True)
    before = handoff.bootstrap_state_path().read_bytes()
    snapshot = handoff.read()
    assert not handoff.clear("bootstrap")
    assert not handoff.allows_generic_recovery(snapshot)
    assert handoff.bootstrap_state_path().read_bytes() == before


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handoff, "state_path", lambda: tmp_path / "handoff.json")
    monkeypatch.setattr(
        handoff, "bootstrap_state_path", lambda: tmp_path / "bootstrap-recovery.json"
    )
    monkeypatch.setattr(handoff, "lock_path", lambda: tmp_path / "handoff.lock")


def test_pending_claim_records_the_childs_exact_process_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff.begin(expected_session="ava-updater", generation="g")
    monkeypatch.setattr(
        psutil, "Process", lambda _pid: type("P", (), {"create_time": lambda _self: 42.5})()
    )

    assert handoff.claim_running("g", expected_session="ava-updater", owner_pid=123)
    snapshot = handoff.read()
    assert snapshot.status == "running"
    assert (snapshot.owner_pid, snapshot.owner_create_time) == (123, 42.5)


def test_claim_is_exact_fresh_pending_cas(monkeypatch: pytest.MonkeyPatch) -> None:
    handoff.begin(expected_session="ava-updater", generation="new", ttl_s=60)
    monkeypatch.setattr(
        psutil, "Process", lambda _pid: type("P", (), {"create_time": lambda _self: 1.0})()
    )
    assert not handoff.claim_running("old", expected_session="ava-updater", owner_pid=1)
    assert handoff.read().generation == "new"


def test_expired_pending_can_be_replaced_and_late_child_cannot_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff.begin(expected_session="ava-updater", generation="old", ttl_s=-1)
    replacement = handoff.begin(expected_session="ava-updater", generation="new")
    monkeypatch.setattr(
        psutil, "Process", lambda _pid: type("P", (), {"create_time": lambda _self: 1.0})()
    )
    assert replacement.generation == "new"
    assert not handoff.claim_running("old", expected_session="ava-updater", owner_pid=1)


def test_running_owner_never_expires_while_exact_pid_is_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff.begin(expected_session="ava-updater", generation="g", ttl_s=1)
    monkeypatch.setattr(
        psutil, "Process", lambda _pid: type("P", (), {"create_time": lambda _self: 10.0})()
    )
    assert handoff.claim_running("g", expected_session="ava-updater", owner_pid=7)
    future = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
    snapshot = handoff.read(now=future)
    assert snapshot.status == "running" and snapshot.expired
    assert handoff.owner_is_live(snapshot)
    with pytest.raises(handoff.UpdaterHandoffActive):
        handoff.begin(expected_session="ava-updater", generation="new")


@pytest.mark.parametrize("error", [psutil.AccessDenied(1), OSError("opaque")])
def test_unreadable_running_identity_fails_closed(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    path = handoff.state_path()
    path.write_text(
        json.dumps(
            {
                "phase": "running",
                "generation": "g",
                "expected_session": "ava-updater",
                "created_at": "2026-01-01T00:00:00+00:00",
                "expires_at": "2026-01-01T00:01:00+00:00",
                "owner_pid": 7,
                "owner_create_time": 10.0,
            }
        )
    )

    def _opaque(_pid: int) -> object:
        raise error

    monkeypatch.setattr(psutil, "Process", _opaque)
    assert handoff.owner_is_live(handoff.read())


def test_pid_reuse_is_positive_death_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    handoff.begin(expected_session="ava-updater", generation="g")
    monkeypatch.setattr(
        psutil, "Process", lambda _pid: type("P", (), {"create_time": lambda _self: 10.0})()
    )
    assert handoff.claim_running("g", expected_session="ava-updater", owner_pid=7)
    monkeypatch.setattr(
        psutil, "Process", lambda _pid: type("P", (), {"create_time": lambda _self: 99.0})()
    )
    assert not handoff.owner_is_live(handoff.read())
    assert handoff.begin(expected_session="ava-updater", generation="new").generation == "new"


def test_exact_generation_clear_cannot_remove_a_replacement() -> None:
    handoff.begin(expected_session="ava-updater", generation="old", ttl_s=-1)
    handoff.begin(expected_session="ava-updater", generation="new")
    assert not handoff.clear("old")
    assert handoff.read().generation == "new"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_marker_is_private_to_the_cluster_user() -> None:
    handoff.begin(expected_session="ava-updater", generation="g")
    assert handoff.state_path().stat().st_mode & 0o777 == 0o600
