# pyright: reportUnknownArgumentType=warning, reportUnknownLambdaType=warning
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import psutil
import pytest

from shared import updater_handoff as handoff


def _retained_bootstrap(stage: str) -> None:
    handoff.begin(expected_session="ava-updater", generation="bootstrap")
    assert handoff.claim_running("bootstrap", expected_session="ava-updater")
    path = handoff.state_path()
    raw = json.loads(path.read_text())
    raw["bootstrap_hop"] = {"stage": stage, "private_reference": "retained"}
    path.write_text(json.dumps(raw))


def test_unfinished_bootstrap_cannot_be_cleared_or_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _retained_bootstrap("old_stopped")
    before = handoff.state_path().read_bytes()
    assert not handoff.clear("bootstrap")
    assert not handoff.force_clear()
    monkeypatch.setattr(handoff, "owner_is_live", lambda _: False)
    with pytest.raises(handoff.UpdaterHandoffActive):
        handoff.begin(expected_session="another-updater")
    assert handoff.state_path().read_bytes() == before


def test_bootstrap_resume_requires_dead_owner_and_preserves_compensation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _retained_bootstrap("candidate_starting")
    assert not handoff.resume_bootstrap("bootstrap", expected_session="recovery")
    monkeypatch.setattr(handoff, "owner_is_live", lambda _: False)
    assert not handoff.resume_bootstrap("other-generation", expected_session="recovery")
    assert handoff.resume_bootstrap("bootstrap", expected_session="recovery")
    raw = json.loads(handoff.state_path().read_text())
    assert raw["bootstrap_hop"] == {
        "stage": "candidate_starting",
        "private_reference": "retained",
    }
    assert raw["owner_pid"] == os.getpid()
    assert raw["owner_create_time"] == psutil.Process().create_time()
    assert raw["expected_session"] == "recovery"


@pytest.mark.parametrize("stage", ["candidate_ready", "recovered"])
def test_only_terminal_bootstrap_can_complete(stage: str) -> None:
    _retained_bootstrap(stage)
    assert handoff.clear("bootstrap")
    assert handoff.read().status == "inactive"


def test_unreadable_handoff_is_not_force_discarded() -> None:
    path = handoff.state_path()
    path.write_text("{unfinished recovery record")
    assert not handoff.force_clear()
    assert path.read_text() == "{unfinished recovery record"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handoff, "state_path", lambda: tmp_path / "handoff.json")
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
