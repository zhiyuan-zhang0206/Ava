"""services.ava_root custody reconciliation (task #4872, route C).

The core proves one record against the recorded native births and the unit's
process group — the trio boundaries (a reused PID and a zombie count as gone, a
stopped process still runs), release-or-retain, the refusal text — and the
supervisor side drives it: the cold-start gate, a fresh spawn reusing a unit's
record slot, every health round, and the repeat-report dedupe.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import psutil
import pytest

from base.daemon.health import DaemonProbe
from base.native_process.ownership import OwnedProcess
from services.ava_root.custody import (
    ReconcileOutcome,
    ServiceCustody,
    reconcile_record,
    require_clear,
)
from services.ava_root.failure_state import UnitFailureFacts
from services.ava_root.health import HealthMonitor
from services.ava_root.manifest import RestartPolicy, UnitManifest, UnitRegistry
from services.ava_root.probes import ProbeRegistry
from services.ava_root.supervisor import Supervisor, SupervisorConfig


class _EventRecorder:
    """Stands in for base.log.logger; keeps every structured call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def info(self, message: str, **extra: object) -> None:
        self.calls.append({"message": message, **extra})

    def warning(self, message: str, **extra: object) -> None:
        self.calls.append({"message": message, **extra})

    def events(self, name: str) -> list[dict[str, object]]:
        return [call for call in self.calls if call.get("event") == name]


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> _EventRecorder:
    import base.log as base_log

    recorder = _EventRecorder()
    monkeypatch.setattr(base_log, "logger", recorder)
    return recorder


def _spawn_group_leader(code: str) -> subprocess.Popen[bytes]:
    """A disposable child leading its own process group, like a unit leader."""
    return subprocess.Popen([sys.executable, "-c", code], process_group=0)  # noqa: S603


def _dead_leader_birth() -> tuple[dict[str, object], int]:
    """One leader's captured birth, positively gone, and its now-empty group."""
    leader = _spawn_group_leader("import time; time.sleep(60)")
    birth = OwnedProcess.capture(psutil.Process(leader.pid))
    leader.kill()
    leader.wait(timeout=10)
    return asdict(birth), leader.pid


def _write_record(
    tmp_path: Path,
    unit: str,
    births: list[dict[str, object]],
    group: int | None,
    *,
    version: int = 2,
) -> Path:
    directory = tmp_path / "custody"
    directory.mkdir(parents=True, exist_ok=True)
    body: dict[str, object] = {
        "version": version,
        "unit": unit,
        "stage": "running",
        "processes": births,
    }
    if version >= 2:
        body["group"] = group
    path = directory / f"{unit}.json"
    path.write_text(json.dumps(body))
    return path


def _wait_for_status(pid: int, wanted: str) -> None:
    deadline = time.monotonic() + 10
    while psutil.Process(pid).status() != wanted:
        if time.monotonic() > deadline:
            raise AssertionError(f"pid {pid} never reached {wanted}")
        time.sleep(0.02)


def _wait_sets_own_group(leader: subprocess.Popen[bytes]) -> None:
    """Wait until the leader's setpgid landed, so a joiner can target its group."""
    deadline = time.monotonic() + 5
    while os.getpgid(leader.pid) != leader.pid:
        if time.monotonic() > deadline:
            raise AssertionError("leader did not create its process group")
        time.sleep(0.01)


def _reap(*processes: subprocess.Popen[bytes]) -> None:
    """Kill what still runs, then reap every process so no zombie lingers."""
    for process in processes:
        if process.poll() is None:
            process.kill()
    for process in processes:
        process.wait(timeout=10)


def root(tmp_path: Path, code: str) -> Supervisor:
    unit = UnitManifest(
        "worker", (sys.executable, "-u", "-c", code), RestartPolicy.ALWAYS, "root", ()
    )
    return Supervisor(
        UnitRegistry([unit]), run_dir=tmp_path, config=SupervisorConfig(stop_timeout_s=0.2)
    )


async def row(owner: Supervisor) -> dict[str, Any]:
    return cast("list[dict[str, Any]]", (await owner.status())["units"])[0]


async def exited(owner: Supervisor) -> None:
    """Wait until root's watch task has processed the unit's own exit."""
    for _ in range(100):
        if (await row(owner))["state"] == "stopped":
            return
        await asyncio.sleep(0.02)
    raise AssertionError("unit did not exit on its own")


def exits_on(trigger: Path, before: str = "pass") -> str:
    """Unit code that runs `before`, then exits once the test creates `trigger`."""
    return (
        f"import pathlib,sys,time\n{before}\n"
        "deadline=time.monotonic()+30\n"
        f"while not pathlib.Path({str(trigger)!r}).exists():\n"
        "    if time.monotonic()>deadline: sys.exit(1)\n"
        "    time.sleep(0.01)\n"
    )


def _stale_record(tmp_path: Path, unit: str) -> Path:
    """A record of one leader every fact of which is gone, its group empty."""
    dead = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], process_group=0)
    birth = OwnedProcess.capture(psutil.Process(dead.pid))
    dead.kill()
    dead.wait(timeout=10)
    record = tmp_path / "custody" / f"{unit}.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        json.dumps(
            {
                "version": 2,
                "unit": unit,
                "stage": "running",
                "processes": [asdict(birth)],
                "group": dead.pid,
            }
        )
    )
    return record


# ── core: release or retain ──────────────────────────────────────────────────


def test_reconcile_releases_when_every_birth_is_gone_and_group_empty(
    tmp_path: Path, events: _EventRecorder
) -> None:
    birth, group = _dead_leader_birth()
    path = _write_record(tmp_path, "worker", [birth], group)

    outcome = reconcile_record(tmp_path, "worker")

    assert outcome is not None
    assert outcome.decision == "released"
    assert (outcome.checked, outcome.found) == (1, 0)
    assert "gone" in outcome.evidence and "empty" in outcome.evidence
    assert not path.exists()
    [event] = events.events("custody_reconcile")
    assert event["unit"] == "worker"
    assert event["decision"] == "released"
    assert event["checked"] == 1
    assert event["found"] == 0
    assert "evidence" in event


def test_reconcile_retains_while_a_recorded_birth_runs(
    tmp_path: Path, events: _EventRecorder
) -> None:
    leader = _spawn_group_leader("import time; time.sleep(60)")
    try:
        birth = OwnedProcess.capture(psutil.Process(leader.pid))
        path = _write_record(tmp_path, "worker", [asdict(birth)], leader.pid)

        outcome = reconcile_record(tmp_path, "worker")

        assert outcome is not None
        assert outcome.decision == "retained"
        assert (outcome.checked, outcome.found) == (1, 1)
        assert "still run" in outcome.evidence
        assert path.exists()
        [event] = events.events("custody_reconcile")
        assert event["decision"] == "retained"
    finally:
        leader.kill()
        leader.wait(timeout=10)


def test_reconcile_treats_a_stopped_birth_as_live(tmp_path: Path) -> None:
    """Boundary b: SIGSTOP/T is not gone — the record stays."""
    leader = _spawn_group_leader("import time; time.sleep(60)")
    try:
        birth = OwnedProcess.capture(psutil.Process(leader.pid))
        _write_record(tmp_path, "worker", [asdict(birth)], leader.pid)
        leader.send_signal(signal.SIGSTOP)
        _wait_for_status(leader.pid, psutil.STATUS_STOPPED)

        outcome = reconcile_record(tmp_path, "worker")

        assert outcome is not None
        assert outcome.decision == "retained"
        assert outcome.found == 1
        assert "live" in outcome.evidence
    finally:
        leader.kill()
        leader.wait(timeout=10)


def test_reconcile_treats_a_zombie_birth_as_gone(tmp_path: Path) -> None:
    """Boundary c: dead-not-reaped counts as dead; the record's own group here
    is a different, empty one so this isolates the per-birth classification."""
    child = _spawn_group_leader("import time; time.sleep(0.5)")
    try:
        birth = OwnedProcess.capture(psutil.Process(child.pid))
        _wait_for_status(child.pid, psutil.STATUS_ZOMBIE)
        _birth, empty_group = _dead_leader_birth()
        path = _write_record(tmp_path, "worker", [asdict(birth)], empty_group)

        outcome = reconcile_record(tmp_path, "worker")

        assert outcome is not None
        assert outcome.decision == "released"
        assert "zombie" in outcome.evidence
        assert not path.exists()
    finally:
        child.wait(timeout=10)


def test_reconcile_treats_a_reused_pid_as_the_recorded_birth_gone(tmp_path: Path) -> None:
    """Boundary a: a PID now naming another birth is gone, not live."""
    keeper = _spawn_group_leader("import time; time.sleep(60)")
    try:
        real = OwnedProcess.capture(psutil.Process(keeper.pid))
        impostor = OwnedProcess(
            real.pid,
            real.birth + 1.0,
            None if real.starttime is None else real.starttime + 1,
        )
        _birth, empty_group = _dead_leader_birth()
        path = _write_record(tmp_path, "worker", [asdict(impostor)], empty_group)

        outcome = reconcile_record(tmp_path, "worker")

        assert outcome is not None
        assert outcome.decision == "released"
        assert "reused" in outcome.evidence
        assert not path.exists()
    finally:
        keeper.kill()
        keeper.wait(timeout=10)


def test_reconcile_retains_while_the_recorded_group_holds_members(tmp_path: Path) -> None:
    leader = _spawn_group_leader(
        "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])"
    )
    birth = OwnedProcess.capture(psutil.Process(leader.pid))
    leader.wait(timeout=10)  # the leader exits; its member keeps the group
    path = _write_record(tmp_path, "worker", [asdict(birth)], leader.pid)
    try:
        outcome = reconcile_record(tmp_path, "worker")

        assert outcome is not None
        assert outcome.decision == "retained"
        assert "still holds" in outcome.evidence
        assert path.exists()
    finally:
        from base.native_process.group_closure import group_members

        for pid in group_members(leader.pid):
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                continue


def test_reconcile_retains_when_a_birth_cannot_be_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keeper = _spawn_group_leader("import time; time.sleep(60)")
    try:
        birth = OwnedProcess.capture(psutil.Process(keeper.pid))
        path = _write_record(tmp_path, "worker", [asdict(birth)], keeper.pid)

        def unverifiable(self: OwnedProcess, _process: psutil.Process) -> bool:
            raise RuntimeError(f"cannot verify process identity for PID {self.pid}")

        monkeypatch.setattr(OwnedProcess, "birth_matches", unverifiable)

        outcome = reconcile_record(tmp_path, "worker")

        assert outcome is not None
        assert outcome.decision == "retained"
        assert "unverifiable" in outcome.evidence
        assert path.exists()
    finally:
        keeper.kill()
        keeper.wait(timeout=10)


def test_reconcile_retains_a_record_without_a_group_number(tmp_path: Path) -> None:
    """A pre-upgrade record keeps its births' verification but not closure."""
    birth, _group = _dead_leader_birth()
    path = _write_record(tmp_path, "worker", [birth], None, version=1)

    outcome = reconcile_record(tmp_path, "worker")

    assert outcome is not None
    assert outcome.decision == "retained"
    assert "no process group" in outcome.evidence
    assert path.exists()


def test_reconcile_rejects_a_bool_version_record(tmp_path: Path, events: _EventRecorder) -> None:
    """JSON `true` would satisfy `in (1, 2)` as `True == 1` and be read as v1;
    the parser rejects bools like every sibling field instead (C-1)."""
    birth, group = _dead_leader_birth()
    path = _write_record(tmp_path, "worker", [birth], group)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["version"] = True
    path.write_text(json.dumps(body), encoding="utf-8")

    outcome = reconcile_record(tmp_path, "worker")

    assert outcome is not None
    assert outcome.decision == "retained"
    assert "version True" in outcome.evidence
    assert path.exists()
    [event] = events.events("custody_reconcile")
    assert event["decision"] == "retained"


def test_require_clear_reconciles_and_names_the_refusal(tmp_path: Path) -> None:
    """The gate clears what it proves and refuses the rest with steps, force path
    and evidence path (task #4872, C-2); a spawning record is unprovable."""
    record = ServiceCustody(tmp_path, "worker")
    with pytest.raises(RuntimeError, match="custody requires reconciliation") as refused:
        require_clear(tmp_path)
    message = str(refused.value)
    assert "no acknowledged generation" in message
    assert "Reconcile steps" in message
    assert "Force path" in message
    assert "Evidence path" in message
    assert str(record.path) in message
    assert record.path.exists()


def test_require_clear_clears_a_proven_record_without_refusing(tmp_path: Path) -> None:
    birth, group = _dead_leader_birth()
    path = _write_record(tmp_path, "worker", [birth], group)
    require_clear(tmp_path)
    assert not path.exists()

    (tmp_path / "custody" / "notes.txt").write_text("an operator's note")
    with pytest.raises(RuntimeError, match="unrecognized entries"):
        require_clear(tmp_path)


# ── supervisor flows ─────────────────────────────────────────────────────────


async def test_reaped_unexpected_exit_reconciles_before_a_cold_duplicate(tmp_path: Path) -> None:
    """A record whose every birth is gone (reaped) and whose group is empty
    releases on reconcile, so a cold start needs no operator force (task #4872,
    C-6); the retained dead generation still settles through the stop family.
    """
    trigger = tmp_path / "go"
    owner = root(tmp_path, exits_on(trigger))
    await owner.start()
    trigger.touch()
    await exited(owner)
    assert "custody" in (await row(owner))["last_error"]
    record = tmp_path / "custody/worker.json"
    assert record.exists()
    other = root(tmp_path, "import time; time.sleep(60)")
    await other.start()
    assert (await row(other))["state"] == "running"
    assert record.exists(), "the fresh generation's own record must be here"
    await other.shutdown()
    await owner.shutdown()
    assert not list((tmp_path / "custody").iterdir())


async def test_running_birth_refuses_cold_duplicate(tmp_path: Path) -> None:
    """While a recorded birth still runs, reconcile retains the record and a
    second root's cold start stays refused — no duplicate is authorized."""
    owner = root(tmp_path, "import time; time.sleep(60)")
    await owner.start()
    record = tmp_path / "custody/worker.json"
    assert record.exists()
    other = root(tmp_path, "raise AssertionError('must not spawn')")
    try:
        with pytest.raises(RuntimeError, match="custody requires reconciliation"):
            await other.start()
        assert record.exists()
    finally:
        await owner.shutdown()
    assert not list((tmp_path / "custody").iterdir())


async def test_spawn_reconciles_a_stale_record_once_then_retries(tmp_path: Path) -> None:
    """A stale record does not block a fresh generation: the spawn reconciles
    it once, then retries the record slot (task #4872, C-3)."""
    owner = root(tmp_path, "import time; time.sleep(60)")
    await owner.start()
    await owner.down("worker")
    _stale_record(tmp_path, "worker")
    result = cast("dict[str, Any]", await owner.up("worker"))
    assert cast("list[dict[str, Any]]", result["units"])[0]["action"] == "started"
    assert (await row(owner))["state"] == "running"
    await owner.shutdown()
    assert not list((tmp_path / "custody").iterdir())


async def test_spawn_keeps_an_unproven_record_and_reports_failure(tmp_path: Path) -> None:
    """A record keeping an unproven fact stays: the retry is refused and the
    unit reports the spawn failure (task #4872, C-3)."""
    owner = root(tmp_path, "import time; time.sleep(60)")
    await owner.start()
    await owner.down("worker")
    record = tmp_path / "custody/worker.json"
    record.write_text(json.dumps({"version": 2, "unit": "worker", "stage": "spawning"}))
    result = cast("dict[str, Any]", await owner.up("worker"))
    unit = cast("list[dict[str, Any]]", result["units"])[0]
    assert unit["action"] == "failed"
    assert "spawn failed" in str(unit["error"])
    assert record.exists()
    await owner.shutdown()


# ── repeat pass dedupe ───────────────────────────────────────────────────────


async def test_repeat_pass_reports_a_retained_record_once_until_evidence_changes(
    tmp_path: Path, events: _EventRecorder
) -> None:
    """A repeated pass reports releases always and a retained record on first
    sight and evidence change only — the stream carries the decision, not a
    per-round heartbeat (task #4872, C-1)."""
    owner = root(tmp_path, "import time; time.sleep(60)")
    leader = _spawn_group_leader("import time; time.sleep(60)")
    _wait_sets_own_group(leader)
    member = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], process_group=leader.pid
    )
    try:
        birth = OwnedProcess.capture(psutil.Process(leader.pid))
        path = _write_record(tmp_path, "worker", [asdict(birth)], leader.pid)

        first = await owner.reconcile_custody()
        assert [outcome.decision for outcome in first] == ["retained"]
        assert len(events.events("custody_reconcile")) == 1

        repeat = await owner.reconcile_custody()
        assert [outcome.decision for outcome in repeat] == ["retained"]
        assert len(events.events("custody_reconcile")) == 1, "an unchanged retain repeats silently"

        leader.kill()
        leader.wait(timeout=10)  # the birth is gone; its member keeps the group
        changed = await owner.reconcile_custody()
        assert [outcome.decision for outcome in changed] == ["retained"]
        reported = events.events("custody_reconcile")
        assert len(reported) == 2, "an evidence change reports again"
        assert "still holds" in str(reported[-1]["evidence"])

        member.kill()
        member.wait(timeout=10)
        released = await owner.reconcile_custody()
        assert [outcome.decision for outcome in released] == ["released"]
        assert len(events.events("custody_reconcile")) == 3
        assert not path.exists()
        assert await owner.reconcile_custody() == []
    finally:
        _reap(leader, member)


# ── health wiring ────────────────────────────────────────────────────────────


class _RevivalStub:
    """A protocol-complete revival host recording what one round asks of it."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.reconcile_calls = 0

    async def restart(self, unit_id: str) -> dict[str, object]:
        self.asked.append(unit_id)
        return {"verb": "restart", "units": []}

    def health_generation(self, unit_id: str) -> tuple[OwnedProcess, float] | None:
        self.asked.append(unit_id)
        return (OwnedProcess(42, 100.0, None), 0.0)

    def revival_deferral(self, unit_id: str) -> str | None:
        self.asked.append(unit_id)
        return None

    def unit_failure_facts(self, unit_id: str) -> UnitFailureFacts:
        self.asked.append(unit_id)
        return UnitFailureFacts(intent_running=True, restart_failed=None, custody_held=False)

    async def reconcile_custody(self) -> list[ReconcileOutcome]:
        self.reconcile_calls += 1
        return []


async def test_health_round_reconciles_custody_before_probing() -> None:
    """Every round reconciles custody first (task #4872, C-4)."""
    stub = _RevivalStub()
    registry = ProbeRegistry()
    registry.register("worker", lambda: DaemonProbe.up("ok"))
    monitor = HealthMonitor(stub, registry)
    await monitor.run_round()
    assert stub.reconcile_calls == 1
