# pyright: reportUnknownArgumentType=warning, reportUnknownLambdaType=warning
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import replace
from pathlib import Path

import psutil
import pytest

from shared import updater_handoff as handoff


def _retained_bootstrap(stage: str) -> None:
    handoff.begin(expected_session="ava-updater", generation="bootstrap")
    assert handoff.claim_running("bootstrap", expected_session="ava-updater")
    handoff.write_bootstrap_recovery("bootstrap", {"stage": stage, "private_reference": "retained"})


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
    assert raw["journal"] == {
        "stage": "candidate_starting",
        "private_reference": "retained",
    }
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
        handoff.write_bootstrap_recovery(
            "bootstrap", {"stage": "old_stopped", "padding": "x" * (300 * 1024)}
        )
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
    _retained_bootstrap("candidate_ready")
    handoff.write_normal_release_recovery(
        "bootstrap", {"stage": stage, "previous_selector": "exact original bytes"}
    )
    path = handoff.bootstrap_state_path()
    before = path.read_bytes()
    assert not handoff.clear("bootstrap")
    assert not handoff.force_clear()
    monkeypatch.setattr(handoff, "owner_is_live", lambda _: False)
    with pytest.raises(handoff.UpdaterHandoffActive):
        handoff.begin(expected_session="replacement")
    assert path.read_bytes() == before


def test_only_committed_normal_release_can_clear() -> None:
    _retained_bootstrap("candidate_ready")
    handoff.write_normal_release_recovery("bootstrap", {"stage": "committed"})
    assert handoff.clear("bootstrap")
    assert not handoff.state_path().exists()
    assert not handoff.bootstrap_state_path().exists()


def test_normal_release_recovery_requires_candidate_ready_bootstrap() -> None:
    _retained_bootstrap("candidate_started")
    before = handoff.bootstrap_state_path().read_bytes()
    with pytest.raises(handoff.BootstrapRecoveryInvalidError, match="candidate-ready"):
        handoff.write_normal_release_recovery("bootstrap", {"stage": "waiting"})
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
