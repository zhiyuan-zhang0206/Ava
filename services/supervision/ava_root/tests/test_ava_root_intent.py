"""Unit intent records: explicit policy, explicit failure states, boot merge (#4872).

The record is what a restarted root re-derives: a failed self-rescue must never
read back as an operator stop, and a recorded failure must survive a root
restart until a fresh generation proves it gone.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from services.supervision.ava_root import intent_store
from services.supervision.ava_root.inputs import InputSeal
from services.supervision.ava_root.intent_store import (
    IntentRecord,
    IntentSource,
    RestartFailure,
    RestartStage,
    UnitIntent,
    merge_record_for_boot,
)
from services.supervision.ava_root.manifest import RestartPolicy, UnitManifest, UnitRegistry
from services.supervision.ava_root.supervisor import Supervisor, SupervisorConfig

_SLEEP_FOREVER = "import time; time.sleep(60)"


class _Recorder:
    """Stands in for base.log.logger; records every structured event call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def info(self, message: str, **extra: object) -> None:
        self.calls.append({"message": message, **extra})

    def warning(self, message: str, **extra: object) -> None:
        self.calls.append({"message": message, **extra})

    def events(self, name: str) -> list[dict[str, object]]:
        return [call for call in self.calls if call.get("event") == name]


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    import base.log as base_log

    rec = _Recorder()
    monkeypatch.setattr(base_log, "logger", rec)
    return rec


def _unit(
    code: str,
    *,
    unit_id: str = "svc",
    inputs: tuple[InputSeal, ...] = (),
) -> UnitManifest:
    return UnitManifest(
        unit_id,
        (sys.executable, "-u", "-c", code),
        RestartPolicy.ALWAYS,
        "root",
        inputs=inputs,
    )


def _supervisor(run_dir: Path, units: list[UnitManifest]) -> Supervisor:
    return Supervisor(
        UnitRegistry(units), run_dir=run_dir, config=SupervisorConfig(stop_timeout_s=0.2)
    )


def _sealed_input(tmp_path: Path, unit_id: str = "svc") -> tuple[Path, InputSeal]:
    path = tmp_path / f"{unit_id}-input"
    path.write_text("v1")
    path = path.resolve()
    return path, InputSeal.capture(path)


async def _entry(owner: Supervisor) -> dict[str, Any]:
    return cast("list[dict[str, Any]]", (await owner.status())["units"])[0]


async def _wait_state(owner: Supervisor, want: str) -> None:
    for _ in range(250):
        if (await _entry(owner))["state"] == want:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"unit did not reach state {want!r}")


async def _wait_file(path: Path) -> None:
    for _ in range(250):
        if path.exists():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"file {path} never appeared")


# -- the boot merge table ------------------------------------------------------


def test_boot_merge_rules() -> None:
    # No record: the admitted start is the only word.
    merged = merge_record_for_boot(None)
    assert (merged.intent, merged.source, merged.restart_failed, merged.start) == (
        UnitIntent.RUNNING,
        IntentSource.SELECTION,
        None,
        True,
    )
    # An explicit operator stop holds; boot never overrides it.
    failure = RestartFailure(RestartStage.UP, 1234.5, "boom")
    merged = merge_record_for_boot(IntentRecord(UnitIntent.STOPPED, IntentSource.OPERATOR, None))
    assert (merged.intent, merged.source, merged.start) == (
        UnitIntent.STOPPED,
        IntentSource.OPERATOR,
        False,
    )
    assert merged.note is not None
    # A selection stop holds the same way (never re-run implicitly).
    merged = merge_record_for_boot(IntentRecord(UnitIntent.STOPPED, IntentSource.SELECTION, None))
    assert (merged.intent, merged.start) == (UnitIntent.STOPPED, False)
    # A stop the root gave itself (shutdown) is mechanical: the start supersedes it.
    merged = merge_record_for_boot(IntentRecord(UnitIntent.STOPPED, IntentSource.SELF, None))
    assert (merged.intent, merged.source, merged.start) == (
        UnitIntent.RUNNING,
        IntentSource.SELECTION,
        True,
    )
    # A running record keeps its source; an unresolved failure is carried.
    merged = merge_record_for_boot(IntentRecord(UnitIntent.RUNNING, IntentSource.OPERATOR, failure))
    assert (merged.intent, merged.source, merged.restart_failed, merged.start) == (
        UnitIntent.RUNNING,
        IntentSource.OPERATOR,
        failure,
        True,
    )


def test_record_round_trip_and_corruption(tmp_path: Path) -> None:
    assert intent_store.read(tmp_path, "svc") is None  # absent
    record = IntentRecord(
        UnitIntent.RUNNING,
        IntentSource.OPERATOR,
        RestartFailure(RestartStage.DOWN, 99.5, "stop refused"),
    )
    intent_store.write(tmp_path, "svc", record)
    assert intent_store.read(tmp_path, "svc") == record
    (tmp_path / "intent" / "svc.json").write_text("{not json")
    assert intent_store.read(tmp_path, "svc") is None
    (tmp_path / "intent" / "svc.json").write_text(
        '{"intent": "sideways", "source": "operator", "restart_failed": null, "updated_at": 1}'
    )
    assert intent_store.read(tmp_path, "svc") is None


# -- boot behavior -------------------------------------------------------------


async def test_boot_holds_an_explicit_stored_stop(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="services.supervision.ava_root.supervisor")
    intent_store.write(
        tmp_path, "svc", IntentRecord(UnitIntent.STOPPED, IntentSource.OPERATOR, None)
    )
    owner = _supervisor(tmp_path, [_unit(_SLEEP_FOREVER)])
    await owner.start()
    try:
        entry = await _entry(owner)
        assert entry["intent"] == "stopped"
        assert entry["intent_source"] == "operator"
        assert entry["state"] == "stopped" and entry["pid"] is None
        assert owner.revival_deferral("svc") == "held down"
        assert "stored operator stop held" in caplog.text
        # The operator's own up supersedes the stored stop.
        await owner.up("svc")
        entry = await _entry(owner)
        assert entry["intent"] == "running" and entry["state"] == "running"
    finally:
        await owner.shutdown()


async def test_boot_holds_a_stored_selection_stop(tmp_path: Path) -> None:
    intent_store.write(
        tmp_path, "svc", IntentRecord(UnitIntent.STOPPED, IntentSource.SELECTION, None)
    )
    owner = _supervisor(tmp_path, [_unit(_SLEEP_FOREVER)])
    await owner.start()
    try:
        entry = await _entry(owner)
        assert (entry["intent"], entry["intent_source"], entry["state"]) == (
            "stopped",
            "selection",
            "stopped",
        )
        assert owner.revival_deferral("svc") == "held down"
    finally:
        await owner.shutdown()


async def test_boot_supersedes_a_stored_root_stop(tmp_path: Path) -> None:
    first = _supervisor(tmp_path, [_unit(_SLEEP_FOREVER)])
    await first.start()
    await first.shutdown()  # writes stopped/self for the unit
    stored = intent_store.read(tmp_path, "svc")
    assert stored is not None and stored.source is IntentSource.SELF

    second = _supervisor(tmp_path, [_unit(_SLEEP_FOREVER)])
    await second.start()
    try:
        entry = await _entry(second)
        assert (entry["intent"], entry["state"]) == ("running", "running")
        assert intent_store.read(tmp_path, "svc") == IntentRecord(
            UnitIntent.RUNNING, IntentSource.SELECTION, None
        )
    finally:
        await second.shutdown()


# -- restart failure atomicity -------------------------------------------------


async def test_restart_up_half_failure_records_explicit_state(
    tmp_path: Path, recorder: _Recorder
) -> None:
    input_path, seal = _sealed_input(tmp_path)
    owner = _supervisor(tmp_path, [_unit(_SLEEP_FOREVER, inputs=(seal,))])
    await owner.start()
    try:
        input_path.write_text("v2")  # the seal no longer matches: the up half must fail
        result = await owner.restart("svc")
        entry = cast("list[dict[str, Any]]", result["units"])[0]
        assert entry["action"] == "failed"
        status = await _entry(owner)
        assert status["intent"] == "running" and status["state"] == "stopped"
        assert status["restart_failed"]["stage"] == "up"
        assert "input" in status["restart_failed"]["detail"]
        assert owner.revival_deferral("svc") is None, "a failed restart is not an operator stop"
        stored = intent_store.read(tmp_path, "svc")
        assert stored is not None and stored.intent is UnitIntent.RUNNING
        assert stored.restart_failed is not None and stored.restart_failed.stage is RestartStage.UP
        assert len(recorder.events("root_restart_failed")) == 1
        assert recorder.events("root_restart_cleared") == []
    finally:
        await owner.shutdown()


async def test_restart_up_half_failure_retry_replaces_and_clears(
    tmp_path: Path, recorder: _Recorder
) -> None:
    input_path, seal = _sealed_input(tmp_path)
    owner = _supervisor(tmp_path, [_unit(_SLEEP_FOREVER, inputs=(seal,))])
    await owner.start()
    try:
        input_path.write_text("v2")
        await owner.restart("svc")
        input_path.write_text("v1")  # the retry can activate again
        result = await owner.restart("svc")
        assert cast("list[dict[str, Any]]", result["units"])[0]["action"] == "restarted"
        status = await _entry(owner)
        assert status["restart_failed"] is None
        assert status["state"] == "running"
        assert owner._units["svc"].restart_count == 1
        assert len(recorder.events("root_restart_failed")) == 1
        assert len(recorder.events("root_restart_cleared")) == 1
    finally:
        await owner.shutdown()


async def test_restart_down_refusal_records_stage_down_and_retry_settles(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    template = (
        "import pathlib, signal, sys, time\n"
        "if pathlib.Path(RELEASE).exists():\n"
        "    time.sleep(60)\n"
        "else:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    pathlib.Path(READY).touch()\n"
        "    while not pathlib.Path(RELEASE).exists(): time.sleep(0.05)\n"
        "    sys.exit(0)\n"
    )
    code = template.replace("RELEASE", repr(str(release))).replace("READY", repr(str(ready)))
    owner = _supervisor(tmp_path, [_unit(code)])
    await owner.start()
    try:
        await _wait_file(ready)  # the child now ignores TERM for real
        active = await _entry(owner)
        assert active["state"] == "running"
        with pytest.raises(RuntimeError, match=r"did not stop within its 0\.2s window"):
            await owner.restart("svc")
        status = await _entry(owner)
        assert status["intent"] == "running"
        assert status["state"] == "running", "a refused stop keeps the old generation"
        assert status["pid"] == active["pid"]
        assert status["restart_failed"]["stage"] == "down"
        assert owner.revival_deferral("svc") is None, "retryable, not an operator stop"
        release.touch()  # the unit finishes on its own; the retry is safe
        await _wait_state(owner, "stopped")
        result = await owner.restart("svc")
        assert cast("list[dict[str, Any]]", result["units"])[0]["action"] == "restarted"
        status = await _entry(owner)
        assert (status["restart_failed"], status["state"]) == (None, "running")
    finally:
        await owner.shutdown()


async def test_boot_carries_recorded_failure_until_a_generation_proves_it_gone(
    tmp_path: Path, recorder: _Recorder
) -> None:
    input_path, seal = _sealed_input(tmp_path)
    unit = _unit(_SLEEP_FOREVER, inputs=(seal,))
    first = _supervisor(tmp_path, [unit])
    await first.start()
    input_path.write_text("v2")
    await first.restart("svc")
    stored = intent_store.read(tmp_path, "svc")
    assert stored is not None and stored.restart_failed is not None
    since = stored.restart_failed.since
    # Simulated root restart: a fresh supervisor over the same run directory.
    second = _supervisor(tmp_path, [unit])
    await second.start()  # the carried failure blocks nothing; the start attempt fails
    try:
        entry = await _entry(second)
        assert entry["intent"] == "running" and entry["state"] == "stopped"
        assert entry["restart_failed"]["stage"] == "up"
        assert entry["restart_failed"]["since"] == since, "the episode keeps its start time"
        assert "spawn failed" in (entry["last_error"] or "")
        assert len(recorder.events("root_restart_failed")) == 1, "no duplicate enter on carry"
        assert recorder.events("root_restart_cleared") == []
        input_path.write_text("v1")
        await second.restart("svc")
        entry = await _entry(second)
        assert entry["restart_failed"] is None and entry["state"] == "running"
        assert len(recorder.events("root_restart_cleared")) == 1
        assert intent_store.read(tmp_path, "svc") == IntentRecord(
            UnitIntent.RUNNING, IntentSource.SELECTION, None
        )
    finally:
        await second.shutdown()
