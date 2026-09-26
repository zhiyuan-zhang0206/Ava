# pyright: reportUnknownArgumentType=warning, reportUnknownLambdaType=warning
from __future__ import annotations

import datetime as dt
import importlib
import json
import logging
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import Mock
from uuid import UUID

import psutil
import pytest

from shared import updater_handoff as handoff
from shared import updater_recovery as recovery
from shared.managed_writer_barrier import RolloutIdentity
from shared.managed_writer_observation import ExpectedUnitWriters, ObservationChallenge
from shared.managed_writer_publication import (
    NormalService,
    NormalServiceReadback,
    PublishedUnit,
    SelectorReadback,
    UnitActivationReadback,
)
from shared.process_evidence import ExpectedProcess


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
        "launchd": [],
        "phases": [
            {
                "stage": stage,
                "observed_at": "2026-09-04T00:00:00Z",
                "monotonic_s": 0.0,
                "pid": 1,
                "elapsed_s": None,
            }
        ],
        "launcher_terminals": [],
        "normal_release": None,
    }


def _spawn_attempt_model(
    session: str = "ava-ops", nonce: UUID | None = None, *, generation: str = "gen-1"
) -> recovery.SpawnAttempt:
    """One journal attempt object; the nonce defaults to UUID(int=7)."""
    value = UUID(int=7) if nonce is None else nonce
    return recovery.SpawnAttempt(
        nonce=value,
        session=session,
        cmd_digest="8" * 64,
        cwd="/unit",
        spawn_lock_path=f"run/updater-spawn/{generation}/{session}.gate",
        receipt_path=f"run/updater-spawn/{generation}/{session}.{value}.receipt.json",
        recorded_at=dt.datetime(2026, 9, 4, tzinfo=dt.UTC),
    )


def _spawn_attempt(
    session: str = "ava-ops", nonce: UUID | None = None, *, generation: str = "gen-1"
) -> dict[str, object]:
    """The JSON (string-typed) form for payloads validated in JSON mode."""
    dumped: dict[str, object] = json.loads(
        _spawn_attempt_model(session, nonce, generation=generation).model_dump_json()
    )
    return dumped


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
        "starting_attempt": _spawn_attempt_model() if stage == "starting" else None,
        "replaces": None,
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
    payload["starting_attempt"] = _spawn_attempt() if stage == "starting" else None
    payload["replaces"] = None
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


def test_legacy_bootstrap_journal_without_terminals_still_reads() -> None:
    _retained_bootstrap("prepared")
    path = handoff.bootstrap_state_path()
    envelope = json.loads(path.read_text())
    del envelope["journal"]["launcher_terminals"]
    path.write_text(json.dumps(envelope))

    raw = handoff.read_bootstrap_recovery()
    assert raw is not None
    assert cast("dict[str, object]", raw["journal"])["launcher_terminals"] == []


def test_bootstrap_writer_carries_launcher_terminals() -> None:
    _retained_bootstrap("prepared")
    quiesced = _bootstrap_journal("cron_quiesced")
    prepared_phases = _bootstrap_journal("prepared")["phases"]
    assert isinstance(prepared_phases, list)
    quiesced["phases"] = [
        *prepared_phases,
        {
            "stage": "cron_quiesced",
            "observed_at": dt.datetime.now(dt.UTC).isoformat(),
            "monotonic_s": 1.0,
            "pid": os.getpid(),
            "elapsed_s": None,
        },
    ]
    quiesced["launcher_terminals"] = [{"label": "e" * 64, "kind": "removed"}]

    handoff.write_bootstrap_recovery("bootstrap", quiesced)

    retained = handoff.read_bootstrap_recovery()
    assert retained is not None
    assert cast("dict[str, object]", retained["journal"])["launcher_terminals"] == [
        {"label": "e" * 64, "kind": "removed", "new_digest": None}
    ]


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


def _write_starting_slot(
    stage_sequence: tuple[str, ...] = ("waiting", "selected", "bootstrap_stopped"),
) -> dict[str, object]:
    """Advance the retained journal through the given stages; return the base."""
    _retained_bootstrap("candidate_ready", normal_release_planned=True)
    base = _normal_journal("waiting")
    for stage in stage_sequence:
        handoff.write_normal_release_recovery("bootstrap", _normal_at(base, stage))
    return base


def test_starting_stage_requires_its_exact_attempt() -> None:
    payload = _normal_journal("starting")
    payload["starting_attempt"] = None
    with pytest.raises(ValueError):
        recovery.NormalReleaseRecoveryJournal.model_validate_json(json.dumps(payload))
    other = _normal_journal("starting")
    other["starting_attempt"] = _spawn_attempt(session="ava-other")
    with pytest.raises(ValueError):
        recovery.NormalReleaseRecoveryJournal.model_validate_json(json.dumps(other))


def test_spawn_attempt_path_shape_is_pinned() -> None:
    attempt = _spawn_attempt()
    mutations: list[dict[str, object]] = [
        {"spawn_lock_path": "run/updater-spawn/gen-1/other.gate"},
        {"spawn_lock_path": "run/updater-spawn/gen-2/ava-ops.gate"},
        {"receipt_path": f"run/updater-spawn/gen-1/ava-ops.{UUID(int=9)}.receipt.json"},
        {"spawn_lock_path": "/run/updater-spawn/gen-1/ava-ops.gate"},
        {"receipt_path": "../ava-ops.{nonce}.receipt.json"},
    ]
    for mutation in mutations:
        candidate = {**attempt, **mutation}
        payload = _normal_journal("starting") | {"starting_attempt": candidate}
        with pytest.raises(ValueError):
            recovery.NormalReleaseRecoveryJournal.model_validate_json(json.dumps(payload))


def test_replaces_witness_only_exists_while_starting() -> None:
    for stage in ("waiting", "selected", "bootstrap_stopped", "observed", "committed"):
        payload = _normal_journal(stage)
        payload["replaces"] = "spawned_dead"
        with pytest.raises(ValueError):
            recovery.NormalReleaseRecoveryJournal.model_validate_json(json.dumps(payload))


def test_first_start_displaces_no_attempt() -> None:
    base = _write_starting_slot()
    starting = _normal_at(base, "starting")
    starting["replaces"] = "spawned_dead"
    with pytest.raises(handoff.BootstrapRecoveryInvalidError, match="displaces no"):
        handoff.write_normal_release_recovery("bootstrap", starting)
    starting["replaces"] = None
    handoff.write_normal_release_recovery("bootstrap", starting)


def test_starting_replacement_requires_witness_and_fresh_nonce() -> None:
    base = _write_starting_slot(("waiting", "selected", "bootstrap_stopped", "starting"))
    first_nonce = str(UUID(int=7))

    unwitnessed = _normal_at(base, "starting")
    unwitnessed["starting_attempt"] = _spawn_attempt(nonce=UUID(int=8))
    unwitnessed["replaces"] = None
    with pytest.raises(handoff.BootstrapRecoveryInvalidError, match="adjudication witness"):
        handoff.write_normal_release_recovery("bootstrap", unwitnessed)

    same_nonce = _normal_at(base, "starting")
    same_nonce_attempt = cast("dict[str, object]", same_nonce["starting_attempt"])
    assert same_nonce_attempt["nonce"] == first_nonce
    same_nonce["replaces"] = "spawned_alive"
    with pytest.raises(handoff.BootstrapRecoveryInvalidError, match="fresh attempt nonce"):
        handoff.write_normal_release_recovery("bootstrap", same_nonce)

    fresh = _normal_at(base, "starting")
    fresh["starting_attempt"] = _spawn_attempt(nonce=UUID(int=8))
    fresh["replaces"] = "spawned_dead"
    handoff.write_normal_release_recovery("bootstrap", fresh)
    retained = handoff.read_bootstrap_recovery()
    assert retained is not None
    journal = cast("dict[str, object]", retained["journal"])
    retained_normal = cast("dict[str, object]", journal["normal_release"])
    retained_attempt = cast("dict[str, object]", retained_normal["starting_attempt"])
    assert retained_attempt["nonce"] == str(UUID(int=8))
    assert retained_normal["replaces"] == "spawned_dead"


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
    monkeypatch.setattr(psutil, "Process", _process_stub(42.5))

    assert handoff.claim_running("g", expected_session="ava-updater", owner_pid=123)
    snapshot = handoff.read()
    assert snapshot.status == "running"
    assert (snapshot.owner_pid, snapshot.owner_create_time) == (123, 42.5)


def test_claim_is_exact_fresh_pending_cas(monkeypatch: pytest.MonkeyPatch) -> None:
    handoff.begin(expected_session="ava-updater", generation="new", ttl_s=60)
    monkeypatch.setattr(psutil, "Process", _process_stub(1.0))
    assert not handoff.claim_running("old", expected_session="ava-updater", owner_pid=1)
    assert handoff.read().generation == "new"


def test_expired_pending_can_be_replaced_and_late_child_cannot_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff.begin(expected_session="ava-updater", generation="old", ttl_s=-1)
    replacement = handoff.begin(expected_session="ava-updater", generation="new")
    monkeypatch.setattr(psutil, "Process", _process_stub(1.0))
    assert replacement.generation == "new"
    assert not handoff.claim_running("old", expected_session="ava-updater", owner_pid=1)


def test_running_owner_never_expires_while_exact_pid_is_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff.begin(expected_session="ava-updater", generation="g", ttl_s=1)
    monkeypatch.setattr(psutil, "Process", _process_stub(10.0))
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
    monkeypatch.setattr(psutil, "Process", _process_stub(10.0))
    assert handoff.claim_running("g", expected_session="ava-updater", owner_pid=7)
    monkeypatch.setattr(psutil, "Process", _process_stub(99.0))
    assert not handoff.owner_is_live(handoff.read())
    assert handoff.begin(expected_session="ava-updater", generation="new").generation == "new"


@pytest.mark.skipif(
    sys.platform != "darwin", reason="psutil's macOS wall-clock correction is macOS-only"
)
def test_claimed_owner_spans_import_epochs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A handoff claimed under one clock epoch stays proven-live under another."""
    psosx = importlib.import_module("psutil._psosx")
    base = psosx.INIT_BOOT_TIME
    child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
    try:
        monkeypatch.setattr(psosx, "INIT_BOOT_TIME", base + 3600.0)
        handoff.begin(expected_session="ava-updater", generation="g")
        assert handoff.claim_running("g", expected_session="ava-updater", owner_pid=child.pid)
        monkeypatch.setattr(psosx, "INIT_BOOT_TIME", base)
        assert handoff.owner_is_live(handoff.read())
    finally:
        child.kill()
        child.wait(timeout=5)


def test_exact_generation_clear_cannot_remove_a_replacement() -> None:
    handoff.begin(expected_session="ava-updater", generation="old", ttl_s=-1)
    handoff.begin(expected_session="ava-updater", generation="new")
    assert not handoff.clear("old")
    assert handoff.read().generation == "new"


def test_atomic_write_json_and_parent_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "handoff.json"
    monkeypatch.setattr(handoff, "_fsync_parent", Mock(side_effect=OSError("sync failed")))
    with caplog.at_level(logging.WARNING, logger="shared.updater_handoff"):
        handoff._write_atomic(path, {"z": "café", "a": 1})
    assert path.read_bytes() == b'{"a":1,"z":"caf\\u00e9"}'
    assert "directory fsync failed after commit" in caplog.text
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("target,method", [(os, "fsync"), (os, "replace"), (Path, "replace")])
def test_atomic_write_precommit_failure_preserves_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: object, method: str
) -> None:
    path = tmp_path / "handoff.json"
    path.write_bytes(b"old")
    fail = Mock(side_effect=OSError("write failed"))
    monkeypatch.setattr(target, method, fail)
    with pytest.raises(OSError, match="write failed"):
        handoff._write_atomic(path, {"new": True})
    assert path.read_bytes() == b"old"
    assert sorted(tmp_path.iterdir()) == [path]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_marker_is_private_to_the_cluster_user() -> None:
    handoff.begin(expected_session="ava-updater", generation="g")
    assert handoff.state_path().stat().st_mode & 0o777 == 0o600


def _process_stub(create_time: float, *, pid: int = 123):
    """psutil.Process stand-in: both identity readings return `create_time`."""

    class _PlatformProcess:
        def create_time(self, monotonic: bool = False) -> float:
            return create_time

    def process(_pid: int | None = None) -> object:
        return type(
            "P",
            (),
            {"pid": pid, "create_time": lambda _self: create_time, "_proc": _PlatformProcess()},
        )()

    return process


def test_bootstrap_writer_tolerates_whole_second_owner_create_time_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(psutil, "Process", _process_stub(42.5))
    handoff.begin(expected_session="ava-updater", generation="bootstrap")
    assert handoff.claim_running("bootstrap", expected_session="ava-updater", owner_pid=123)
    # The writer re-reads its own wall-clock-derived birth: 1s of drift is the same process.
    monkeypatch.setattr(psutil, "Process", _process_stub(43.5))
    handoff.write_bootstrap_recovery("bootstrap", _bootstrap_journal("prepared"))
    assert handoff.read_bootstrap_recovery() is not None
    # Beyond the tolerance the writer is no longer the recorded owner.
    monkeypatch.setattr(psutil, "Process", _process_stub(102.5))
    with pytest.raises(handoff.BootstrapRecoveryInvalidError, match="lost exact handoff ownership"):
        handoff.write_bootstrap_recovery("bootstrap", _bootstrap_journal("prepared"))


def test_normal_writer_tolerates_whole_second_owner_create_time_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(psutil, "Process", _process_stub(42.5))
    handoff.begin(expected_session="ava-updater", generation="bootstrap")
    assert handoff.claim_running("bootstrap", expected_session="ava-updater", owner_pid=123)
    handoff.write_bootstrap_recovery(
        "bootstrap", _bootstrap_journal("candidate_ready", normal_release_planned=True)
    )
    monkeypatch.setattr(psutil, "Process", _process_stub(43.5))
    handoff.write_normal_release_recovery("bootstrap", _normal_journal("waiting"))
    monkeypatch.setattr(psutil, "Process", _process_stub(102.5))
    with pytest.raises(
        handoff.BootstrapRecoveryInvalidError, match="normal writer lost exact handoff ownership"
    ):
        handoff.write_normal_release_recovery("bootstrap", _normal_journal("waiting"))


@pytest.fixture(autouse=True)
def _isolated_attempts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the clear-time spawn-attempt GC (I6) inside the test home.

    A second isolated-path fixture (not folded into ``_isolated``) keeps this
    addition additive at the file tail.
    """
    monkeypatch.setattr(
        handoff, "spawn_attempts_dir", lambda generation: tmp_path / "updater-spawn" / generation
    )


def test_clear_gcs_the_generation_spawn_attempts() -> None:
    """I6: a successful clear removes this generation's spawn-attempt evidence."""
    _retained_bootstrap("candidate_ready", normal_release_planned=True)
    _write_normal_through("committed")
    attempts = handoff.spawn_attempts_dir("bootstrap")
    attempts.mkdir(parents=True, exist_ok=True)
    (attempts / "ava-ops.gate").write_text("held", encoding="utf-8")
    (attempts / "ava-ops.7.receipt.json").write_text("{}", encoding="utf-8")
    assert handoff.clear("bootstrap")
    assert not attempts.exists()


def test_refused_clear_keeps_the_generation_spawn_attempts() -> None:
    """I6: a refused clear never touches the attempt evidence (non-terminal)."""
    _retained_bootstrap("candidate_started")
    attempts = handoff.spawn_attempts_dir("bootstrap")
    attempts.mkdir(parents=True, exist_ok=True)
    (attempts / "ava-ops.gate").write_text("held", encoding="utf-8")
    (attempts / "ava-ops.7.receipt.json").write_text("{}", encoding="utf-8")
    assert not handoff.clear("bootstrap")
    assert (attempts / "ava-ops.gate").read_text(encoding="utf-8") == "held"
    assert (attempts / "ava-ops.7.receipt.json").read_text(encoding="utf-8") == "{}"
    assert handoff.state_path().exists()


@pytest.mark.parametrize(
    "exc", [OSError("device busy"), ValueError("embedded null byte")], ids=["oserror", "valueerror"]
)
def test_failed_gc_keeps_the_generation_spawn_attempts(
    exc: OSError | ValueError,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """I6 failure branch: a failed removal is logged and the evidence is kept.

    Clear still returns its verdict (about the handoff state, not the
    directory): the warning is the record, the retained files are the
    conservative side.
    """
    _retained_bootstrap("candidate_ready", normal_release_planned=True)
    _write_normal_through("committed")
    attempts = handoff.spawn_attempts_dir("bootstrap")
    attempts.mkdir(parents=True, exist_ok=True)
    (attempts / "ava-ops.gate").write_text("held", encoding="utf-8")
    (attempts / "ava-ops.7.receipt.json").write_text("{}", encoding="utf-8")

    def _refuse_removal(*args: object, **kwargs: object) -> None:
        raise exc

    monkeypatch.setattr(handoff.shutil, "rmtree", _refuse_removal)
    with caplog.at_level(logging.WARNING, logger="shared.updater_handoff"):
        assert handoff.clear("bootstrap")
    assert "spawn-attempt GC left evidence in place" in caplog.text
    assert (attempts / "ava-ops.gate").read_text(encoding="utf-8") == "held"
    assert (attempts / "ava-ops.7.receipt.json").read_text(encoding="utf-8") == "{}"
    assert not handoff.state_path().exists()
    assert not handoff.bootstrap_state_path().exists()
