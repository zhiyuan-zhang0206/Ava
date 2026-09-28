# pyright: reportUnknownArgumentType=warning, reportUnknownLambdaType=warning
"""Retained updater handoff evidence: the readers and exact-generation clear.

No current code writes the handoff or its recovery envelope, so every state
here is a raw on-disk fixture shaped like what the retired updater left behind.
"""

from __future__ import annotations

import datetime as dt
import importlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
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
from shared.native_process.ownership import stable_create_time
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


# Captured before the autouse fixture swaps it for a tmp-path stand-in.
_real_spawn_attempts_dir = handoff.spawn_attempts_dir


def _write_handoff(
    generation: str = "g",
    *,
    phase: str = "running",
    expires_in_s: float = 900.0,
    owner_pid: int | None = None,
    owner_create_time: float | None = None,
) -> None:
    """Write the marker the retired updater published, exactly as it laid it out.

    A running marker defaults to this test process as its owner, so its
    identity is genuinely live unless a test swaps it.
    """
    now = dt.datetime.now(dt.UTC)
    payload: dict[str, object] = {
        "phase": phase,
        "generation": generation,
        "expected_session": "ava-updater",
        "created_at": now.isoformat(),
        "expires_at": (now + dt.timedelta(seconds=expires_in_s)).isoformat(),
    }
    if phase == "running":
        payload["owner_pid"] = os.getpid() if owner_pid is None else owner_pid
        payload["owner_create_time"] = (
            stable_create_time(psutil.Process()) if owner_create_time is None else owner_create_time
        )
    handoff.state_path().write_text(json.dumps(payload))


def _write_bootstrap(generation: str, journal: dict[str, object], *, version: int = 1) -> None:
    handoff.bootstrap_state_path().write_text(
        json.dumps({"version": version, "generation": generation, "journal": journal})
    )


def _retained_bootstrap(
    stage: str,
    *,
    normal_release_planned: bool = False,
    normal: dict[str, object] | None = None,
) -> None:
    """A running bootstrap handoff plus its retained recovery envelope."""
    _write_handoff("bootstrap")
    journal = _bootstrap_journal(stage, normal_release_planned=normal_release_planned)
    journal["normal_release"] = normal
    _write_bootstrap("bootstrap", journal)


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handoff, "state_path", lambda: tmp_path / "handoff.json")
    monkeypatch.setattr(
        handoff, "bootstrap_state_path", lambda: tmp_path / "bootstrap-recovery.json"
    )
    monkeypatch.setattr(handoff, "lock_path", lambda: tmp_path / "handoff.lock")


@pytest.fixture(autouse=True)
def _isolated_attempts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the clear-time spawn-attempt GC (I6) inside the test home."""
    monkeypatch.setattr(
        handoff, "spawn_attempts_dir", lambda generation: tmp_path / "updater-spawn" / generation
    )


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


# --- the retained recovery envelope gates clear and generic recovery ---------


def test_unfinished_bootstrap_cannot_be_cleared() -> None:
    _retained_bootstrap("old_stopped")
    before = handoff.bootstrap_state_path().read_bytes()
    assert not handoff.clear("bootstrap")
    assert not handoff.allows_generic_recovery(handoff.read())
    assert handoff.bootstrap_state_path().read_bytes() == before


@pytest.mark.parametrize("stage", ["candidate_ready", "recovered"])
def test_only_terminal_bootstrap_can_complete(stage: str) -> None:
    _retained_bootstrap(stage)
    assert handoff.allows_generic_recovery(handoff.read())
    assert handoff.clear("bootstrap")
    assert handoff.read().status == "inactive"
    assert not handoff.bootstrap_state_path().exists()


def test_malformed_versioned_bootstrap_evidence_is_retained() -> None:
    _write_handoff("bootstrap")
    path = handoff.bootstrap_state_path()
    path.write_text('{"version":2,"generation":"bootstrap"}')
    assert not handoff.clear("bootstrap")
    assert not handoff.allows_generic_recovery(handoff.read())
    assert path.read_text() == '{"version":2,"generation":"bootstrap"}'


def test_oversized_bootstrap_evidence_is_retained() -> None:
    """The reader's byte budget: an over-budget envelope is unreadable, never absent."""
    _write_handoff("bootstrap")
    oversized = _bootstrap_journal("candidate_ready")
    oversized["cron"] = "x" * (300 * 1024)
    _write_bootstrap("bootstrap", oversized)
    before = handoff.bootstrap_state_path().read_bytes()
    assert not handoff.clear("bootstrap")
    assert not handoff.allows_generic_recovery(handoff.read())
    assert handoff.bootstrap_state_path().read_bytes() == before


def test_orphaned_bootstrap_evidence_blocks_recovery_of_an_unreadable_marker() -> None:
    """With no readable generation, only an absent envelope permits recovery."""
    handoff.state_path().write_text("{unfinished recovery record")
    snapshot = handoff.read()
    assert snapshot.status == "invalid"
    assert handoff.allows_generic_recovery(snapshot)
    _write_bootstrap("bootstrap", _bootstrap_journal("candidate_ready"))
    assert not handoff.allows_generic_recovery(snapshot)


@pytest.mark.parametrize(
    "stage", ["waiting", "selected", "bootstrap_stopped", "starting", "observed"]
)
def test_normal_release_retains_exact_recovery_record(stage: str) -> None:
    _retained_bootstrap(
        "candidate_ready", normal_release_planned=True, normal=_normal_journal(stage)
    )
    path = handoff.bootstrap_state_path()
    before = path.read_bytes()
    assert not handoff.clear("bootstrap")
    assert not handoff.allows_generic_recovery(handoff.read())
    assert path.read_bytes() == before


def test_only_committed_normal_release_can_clear() -> None:
    _retained_bootstrap(
        "candidate_ready", normal_release_planned=True, normal=_normal_journal("committed")
    )
    assert handoff.clear("bootstrap")
    assert not handoff.state_path().exists()
    assert not handoff.bootstrap_state_path().exists()


def test_partial_committed_normal_release_is_retained_as_malformed() -> None:
    _retained_bootstrap(
        "candidate_ready", normal_release_planned=True, normal={"stage": "committed"}
    )
    path = handoff.bootstrap_state_path()
    before = path.read_bytes()
    assert not handoff.clear("bootstrap")
    assert not handoff.allows_generic_recovery(handoff.read())
    assert path.read_bytes() == before


def test_complete_but_incoherent_terminal_recovery_is_retained() -> None:
    journal = _bootstrap_journal("recovered")
    journal["normal_release"] = _normal_journal("committed")
    _write_handoff("bootstrap")
    _write_bootstrap("bootstrap", journal)
    path = handoff.bootstrap_state_path()
    before = path.read_bytes()
    assert not handoff.clear("bootstrap")
    assert not handoff.allows_generic_recovery(handoff.read())
    assert path.read_bytes() == before


def test_planned_normal_release_blocks_clear_before_its_first_journal_write() -> None:
    _retained_bootstrap("candidate_ready", normal_release_planned=True)
    before = handoff.bootstrap_state_path().read_bytes()
    snapshot = handoff.read()
    assert not handoff.clear("bootstrap")
    assert not handoff.allows_generic_recovery(snapshot)
    assert handoff.bootstrap_state_path().read_bytes() == before


def test_legacy_bootstrap_journal_without_terminals_still_reads() -> None:
    """Journals written before `launcher_terminals` existed parse as terminal evidence."""
    journal = _bootstrap_journal("candidate_ready")
    del journal["launcher_terminals"]
    _write_handoff("bootstrap")
    _write_bootstrap("bootstrap", journal)
    assert handoff.clear("bootstrap")
    assert not handoff.bootstrap_state_path().exists()


def test_generic_recovery_requires_the_exact_inspected_snapshot() -> None:
    _write_handoff("old")
    inspected = handoff.read()
    _write_handoff("new")
    assert not handoff.allows_generic_recovery(inspected)
    assert handoff.allows_generic_recovery(handoff.read())


# --- the recovery evidence schemas the reader validates against --------------


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


# --- the handoff marker: parse, expiry and exact owner identity --------------


def test_pending_marker_expiry_is_read_against_the_given_clock() -> None:
    _write_handoff("g", phase="pending", expires_in_s=60)
    snapshot = handoff.read()
    assert (snapshot.status, snapshot.generation, snapshot.expired) == ("pending", "g", False)
    assert snapshot.owner_pid is None and snapshot.owner_create_time is None
    later = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)
    assert handoff.read(now=later).expired


@pytest.mark.parametrize(
    "mutation",
    [
        {"phase": "pending", "owner_pid": 7, "owner_create_time": 1.0},
        {"phase": "running", "owner_pid": None, "owner_create_time": 1.0},
        {"phase": "running", "owner_pid": 7, "owner_create_time": "soon"},
        {"phase": "finished"},
        {"generation": ""},
        {"expires_at": "2026-01-01T00:00:00"},
    ],
)
def test_malformed_marker_is_conservatively_invalid(
    mutation: dict[str, object], caplog: pytest.LogCaptureFixture
) -> None:
    _write_handoff("g", owner_pid=7, owner_create_time=1.0)
    payload = json.loads(handoff.state_path().read_text())
    handoff.state_path().write_text(json.dumps(payload | mutation))
    with caplog.at_level(logging.WARNING, logger="shared.updater_handoff"):
        assert handoff.read().status == "invalid"
    assert "invalid" in caplog.text


def test_running_owner_never_expires_while_exact_pid_is_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_handoff("g", expires_in_s=1, owner_pid=7, owner_create_time=10.0)
    monkeypatch.setattr(psutil, "Process", _process_stub(10.0))
    future = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
    snapshot = handoff.read(now=future)
    assert snapshot.status == "running" and snapshot.expired
    assert handoff.owner_is_live(snapshot)


@pytest.mark.parametrize("error", [psutil.AccessDenied(1), OSError("opaque")])
def test_unreadable_running_identity_fails_closed(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    _write_handoff("g", owner_pid=7, owner_create_time=10.0)

    def _opaque(_pid: int) -> object:
        raise error

    monkeypatch.setattr(psutil, "Process", _opaque)
    assert handoff.owner_is_live(handoff.read())


def test_pid_reuse_is_positive_death_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    _write_handoff("g", owner_pid=7, owner_create_time=10.0)
    monkeypatch.setattr(psutil, "Process", _process_stub(99.0))
    assert not handoff.owner_is_live(handoff.read())


def test_absent_pid_is_positive_death_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    _write_handoff("g", owner_pid=7, owner_create_time=10.0)

    def _gone(pid: int) -> object:
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(psutil, "Process", _gone)
    assert not handoff.owner_is_live(handoff.read())


def test_owner_liveness_is_undefined_for_a_pending_marker() -> None:
    _write_handoff("g", phase="pending")
    with pytest.raises(ValueError, match="running handoff"):
        handoff.owner_is_live(handoff.read())


@pytest.mark.skipif(
    sys.platform != "darwin", reason="psutil's macOS wall-clock correction is macOS-only"
)
def test_recorded_owner_spans_import_epochs(monkeypatch: pytest.MonkeyPatch) -> None:
    """An owner recorded under one clock epoch stays proven-live under another."""
    psosx = importlib.import_module("psutil._psosx")
    base = psosx.INIT_BOOT_TIME
    child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
    try:
        monkeypatch.setattr(psosx, "INIT_BOOT_TIME", base + 3600.0)
        recorded = stable_create_time(psutil.Process(child.pid))
        _write_handoff("g", owner_pid=child.pid, owner_create_time=recorded)
        monkeypatch.setattr(psosx, "INIT_BOOT_TIME", base)
        assert handoff.owner_is_live(handoff.read())
    finally:
        child.kill()
        child.wait(timeout=5)


def test_exact_generation_clear_cannot_remove_a_replacement() -> None:
    _write_handoff("new")
    assert not handoff.clear("old")
    assert handoff.read().generation == "new"


# --- clear-time spawn-attempt GC (I6) -----------------------------------------


def _seed_attempts() -> Path:
    attempts = handoff.spawn_attempts_dir("bootstrap")
    attempts.mkdir(parents=True, exist_ok=True)
    (attempts / "ava-ops.gate").write_text("held", encoding="utf-8")
    (attempts / "ava-ops.7.receipt.json").write_text("{}", encoding="utf-8")
    return attempts


def test_clear_gcs_the_generation_spawn_attempts() -> None:
    """I6: a successful clear removes this generation's spawn-attempt evidence."""
    _retained_bootstrap(
        "candidate_ready", normal_release_planned=True, normal=_normal_journal("committed")
    )
    attempts = _seed_attempts()
    assert handoff.clear("bootstrap")
    assert not attempts.exists()


def test_refused_clear_keeps_the_generation_spawn_attempts() -> None:
    """I6: a refused clear never touches the attempt evidence (non-terminal)."""
    _retained_bootstrap("candidate_started")
    attempts = _seed_attempts()
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
    _retained_bootstrap(
        "candidate_ready", normal_release_planned=True, normal=_normal_journal("committed")
    )
    attempts = _seed_attempts()

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


def test_spawn_attempt_dir_is_bound_to_the_unit_run_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
    assert _real_spawn_attempts_dir("gen-1") == tmp_path / "run" / "updater-spawn" / "gen-1"


@pytest.mark.parametrize("generation", ["", "../escape", "gen/../escape", ".hidden", "a b"])
def test_spawn_attempt_dir_refuses_a_name_that_leaves_its_directory(
    generation: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tampered marker generation must never steer the clear-time GC elsewhere."""
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
    with pytest.raises(ValueError, match="spawn-attempt directory"):
        _real_spawn_attempts_dir(generation)
