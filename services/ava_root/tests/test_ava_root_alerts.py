"""services.ava_root.alerts: the episode store and the fire/hold/resolve router (#4872 B)."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from base.daemon.health import DaemonProbe
from base.native_process.ownership import OwnedProcess
from services.ava_root import alerts as alerts_mod
from services.ava_root.alerts import (
    AlertKind,
    AlertRouter,
    EpisodeRecord,
    UnitAlertFacts,
    UnitAlertView,
    _webhook_payload,
    clear_record,
    derive_kind,
    describe,
    read_record,
    write_record,
)
from services.ava_root.intent_store import RestartFailure, RestartStage


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


class _Clock:
    """The module's `time` seam: one second per call, strictly increasing."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(alerts_mod, "time", fake)
    return fake


class _Notifier:
    """Records every post per unit; flippable acceptance."""

    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.calls: list[tuple[str, EpisodeRecord, bool]] = []

    def notify(self, unit: str, record: EpisodeRecord, *, resolved: bool) -> bool:
        self.calls.append((unit, record, resolved))
        return self.accept


def _view(
    unit: str = "svc",
    *,
    intent_running: bool = True,
    restart_failed: RestartFailure | None = None,
    breaker_open: bool = False,
    custody_held: bool = False,
    detail: str = "",
) -> UnitAlertView:
    return UnitAlertView(
        unit=unit,
        facts=UnitAlertFacts(
            intent_running=intent_running,
            restart_failed=restart_failed,
            custody_held=custody_held,
        ),
        breaker_open=breaker_open,
        detail=detail,
    )


def _failure(stage: RestartStage = RestartStage.UP, detail: str = "boom") -> RestartFailure:
    return RestartFailure(stage=stage, since=12.0, detail=detail)


# -- derivation -----------------------------------------------------------------


def test_derive_kind_priority_and_the_intent_gate() -> None:
    failure = _failure()
    assert derive_kind(_view()) is None
    assert derive_kind(_view(breaker_open=True)) is AlertKind.BREAKER_OPEN
    assert derive_kind(_view(custody_held=True)) is AlertKind.CUSTODY_HELD
    assert derive_kind(_view(restart_failed=failure)) is AlertKind.RESTART_FAILED
    # The recorded replacement failure wins over the breaker, which wins over custody.
    both = _view(restart_failed=failure, breaker_open=True, custody_held=True)
    assert derive_kind(both) is AlertKind.RESTART_FAILED
    assert derive_kind(_view(breaker_open=True, custody_held=True)) is AlertKind.BREAKER_OPEN
    # A stop holds every failure state and still never alerts.
    stopped = _view(
        intent_running=False, restart_failed=failure, breaker_open=True, custody_held=True
    )
    assert derive_kind(stopped) is None


def test_describe_carries_the_kind_evidence() -> None:
    assert describe(
        _view(restart_failed=_failure(RestartStage.DOWN, "stop refused")), AlertKind.RESTART_FAILED
    ) == ("replacement failed at its down half: stop refused")
    assert describe(_view(detail="probe: down (port-taken)"), AlertKind.BREAKER_OPEN) == (
        "probe: down (port-taken)"
    )
    assert describe(_view(), AlertKind.BREAKER_OPEN) == "restart breaker open"
    assert describe(_view(), AlertKind.CUSTODY_HELD) == "native custody requires reconciliation"


# -- the store ------------------------------------------------------------------


def test_store_round_trip_and_clear(tmp_path: Path) -> None:
    record = EpisodeRecord(AlertKind.RESTART_FAILED, 12.5, 13.0, "boom", 14.0)
    write_record(tmp_path, "svc", record)
    assert read_record(tmp_path, "svc") == record
    clear_record(tmp_path, "svc")
    assert read_record(tmp_path, "svc") is None
    clear_record(tmp_path, "svc")  # clearing an absent record is a no-op


def test_store_treats_unreadable_records_as_absent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="services.ava_root.alerts")
    directory = tmp_path / "alerts"
    directory.mkdir()
    (directory / "svc.json").write_text("{not json")
    assert read_record(tmp_path, "svc") is None
    (directory / "svc.json").write_text('{"kind": "restart_failed"}')
    assert read_record(tmp_path, "svc") is None
    (directory / "svc.json").write_text(
        '{"kind": "nonsense", "since": 1, "fired_at": 1, "detail": "x", "delivered_at": null}'
    )
    assert read_record(tmp_path, "svc") is None
    assert caplog.text.count("alert episode") >= 3


# -- the router -----------------------------------------------------------------


def test_fire_once_then_hold(tmp_path: Path, recorder: _Recorder, clock: _Clock) -> None:
    notifier = _Notifier()
    router = AlertRouter(tmp_path, notifier=notifier)
    view = _view(restart_failed=_failure())
    router.observe([view])
    router.observe([view])  # a backoff round stays silent
    router.observe([view])
    fired = recorder.events("root_unit_alert_fired")
    assert len(fired) == 1
    assert fired[0]["unit"] == "svc"
    assert fired[0]["kind"] == "restart_failed"
    assert fired[0]["detail"] == "replacement failed at its up half: boom"
    assert fired[0]["delivery"] == "posted"
    assert recorder.events("root_unit_alert_resolved") == []
    assert len(notifier.calls) == 1
    unit, record, resolved = notifier.calls[0]
    assert (unit, resolved) == ("svc", False)
    assert record.kind is AlertKind.RESTART_FAILED
    stored = read_record(tmp_path, "svc")
    assert stored is not None and stored.delivered_at is not None


def test_resolve_replays_the_identity_and_clears(
    tmp_path: Path, recorder: _Recorder, clock: _Clock
) -> None:
    notifier = _Notifier()
    router = AlertRouter(tmp_path, notifier=notifier)
    router.observe([_view(restart_failed=_failure())])
    fired = recorder.events("root_unit_alert_fired")[0]
    since = fired["since_timestamp_seconds"]
    clock.now += 100.0
    router.observe([_view()])  # the condition is gone
    resolved = recorder.events("root_unit_alert_resolved")
    assert len(resolved) == 1
    assert resolved[0]["since_timestamp_seconds"] == since
    assert resolved[0]["kind"] == "restart_failed"
    assert resolved[0]["delivery"] == "posted"
    assert cast("float", resolved[0]["failed_for_s"]) >= 100.0
    assert read_record(tmp_path, "svc") is None
    unit, record, was_resolved = notifier.calls[-1]
    assert (unit, was_resolved) == ("svc", True)
    assert record.since == since


def test_a_returning_condition_is_a_new_episode(
    tmp_path: Path, recorder: _Recorder, clock: _Clock
) -> None:
    router = AlertRouter(tmp_path)
    view = _view(breaker_open=True)
    router.observe([view])
    router.observe([_view()])  # observed, condition absent: resolves
    router.observe([view])  # a fresh episode
    fired = recorder.events("root_unit_alert_fired")
    assert len(fired) == 2
    first = cast("float", fired[0]["since_timestamp_seconds"])
    second = cast("float", fired[1]["since_timestamp_seconds"])
    assert first < second


def test_kind_change_keeps_the_open_episode(
    tmp_path: Path, recorder: _Recorder, clock: _Clock
) -> None:
    notifier = _Notifier()
    router = AlertRouter(tmp_path, notifier=notifier)
    router.observe([_view(restart_failed=_failure())])
    router.observe([_view(breaker_open=True)])  # the failure cleared, the breaker holds
    assert len(recorder.events("root_unit_alert_fired")) == 1
    assert len(notifier.calls) == 1, "a kind change must not re-notify"
    stored = read_record(tmp_path, "svc")
    assert stored is not None and stored.kind is AlertKind.BREAKER_OPEN
    assert stored.since == recorder.events("root_unit_alert_fired")[0]["since_timestamp_seconds"]


def test_root_restart_carries_the_episode(
    tmp_path: Path, recorder: _Recorder, clock: _Clock
) -> None:
    view = _view(restart_failed=_failure())
    AlertRouter(tmp_path).observe([view])
    # A restarted root derives its rounds from the same store: still failing, no re-fire.
    restarted = AlertRouter(tmp_path)
    restarted.observe([view])
    assert len(recorder.events("root_unit_alert_fired")) == 1
    restarted.observe([_view()])  # the condition disappeared meanwhile and still resolves
    assert len(recorder.events("root_unit_alert_resolved")) == 1


def test_resolve_is_skipped_when_the_firing_never_landed(
    tmp_path: Path, recorder: _Recorder, clock: _Clock
) -> None:
    notifier = _Notifier(accept=False)
    router = AlertRouter(tmp_path, notifier=notifier)
    router.observe([_view(breaker_open=True)])
    assert recorder.events("root_unit_alert_fired")[0]["delivery"] == "failed"
    router.observe([_view()])
    resolved = recorder.events("root_unit_alert_resolved")[0]
    assert resolved["delivery"] == "skipped"
    assert len(notifier.calls) == 1, "a never-delivered firing has no open row to close"


def test_pending_delivery_is_repaired_on_a_later_observation(
    tmp_path: Path, recorder: _Recorder, clock: _Clock
) -> None:
    notifier = _Notifier(accept=False)
    router = AlertRouter(tmp_path, notifier=notifier)
    view = _view(breaker_open=True)
    router.observe([view])  # the channel rejects the firing
    assert recorder.events("root_unit_alert_fired")[0]["delivery"] == "failed"
    notifier.accept = True
    router.observe([view])  # a later observation re-posts the pending firing
    assert len(recorder.events("root_unit_alert_fired")) == 1, "no re-fire, only re-delivery"
    stored = read_record(tmp_path, "svc")
    assert stored is not None and stored.delivered_at is not None
    router.observe([_view()])
    resolved = recorder.events("root_unit_alert_resolved")[0]
    assert resolved["delivery"] == "posted"
    assert notifier.calls[-1][2] is True


def test_no_notifier_keeps_store_and_events(
    tmp_path: Path, recorder: _Recorder, clock: _Clock
) -> None:
    router = AlertRouter(tmp_path)
    router.observe([_view(custody_held=True)])
    assert recorder.events("root_unit_alert_fired")[0]["delivery"] == "skipped"
    assert read_record(tmp_path, "svc") is not None
    router.observe([_view()])
    assert recorder.events("root_unit_alert_resolved")[0]["delivery"] == "skipped"


def test_a_raising_notifier_is_a_failed_delivery(
    tmp_path: Path, recorder: _Recorder, clock: _Clock
) -> None:
    class _Boom:
        def notify(self, unit: str, record: EpisodeRecord, *, resolved: bool) -> bool:
            raise RuntimeError("boom")

    router = AlertRouter(tmp_path, notifier=_Boom())
    router.observe([_view(breaker_open=True)])
    assert recorder.events("root_unit_alert_fired")[0]["delivery"] == "failed"


def test_one_units_failure_never_aborts_the_round(
    tmp_path: Path, recorder: _Recorder, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    router = AlertRouter(tmp_path)
    real_read = alerts_mod.read_record

    def flaky(run_dir: Path, unit: str) -> EpisodeRecord | None:
        if unit == "bad":
            raise RuntimeError("store exploded")
        return real_read(run_dir, unit)

    monkeypatch.setattr(alerts_mod, "read_record", flaky)
    router.observe([_view(unit="bad", breaker_open=True), _view(unit="good", breaker_open=True)])
    fired = recorder.events("root_unit_alert_fired")
    assert len(fired) == 1 and fired[0]["unit"] == "good"


# -- the wire payload -----------------------------------------------------------


def _only_alert(payload: dict[str, object]) -> dict[str, object]:
    alerts = cast("list[dict[str, object]]", payload["alerts"])
    return alerts[0]


def _labels(payload: dict[str, object]) -> dict[str, str]:
    return cast("dict[str, str]", _only_alert(payload)["labels"])


def test_webhook_payload_wire_shape_and_stable_fingerprint(clock: _Clock) -> None:
    record = EpisodeRecord(AlertKind.RESTART_FAILED, 1000.0, 1000.0, "boom", None)
    fired = _webhook_payload("svc", record, resolved=False)
    assert fired["source"] == "ava-root"
    alert = _only_alert(fired)
    assert set(alert) == {"status", "labels", "annotations", "startsAt", "endsAt", "fingerprint"}
    assert alert["status"] == "firing"
    assert alert["startsAt"] == "1970-01-01T00:16:40+00:00"
    assert alert["endsAt"] == ""
    assert _labels(fired) == {
        "alertname": "root-unit-alert",
        "severity": "error",
        "unit": "svc",
        "kind": "restart_failed",
    }
    resolved = _only_alert(_webhook_payload("svc", record, resolved=True))
    assert resolved["status"] == "resolved"
    assert resolved["endsAt"] != ""
    assert resolved["fingerprint"] == alert["fingerprint"]
    # The (fingerprint, startsAt) dedup key must not move when the kind changes.
    flipped = _only_alert(
        _webhook_payload("svc", replace(record, kind=AlertKind.BREAKER_OPEN), resolved=True)
    )
    assert flipped["fingerprint"] == alert["fingerprint"]


def test_webhook_payload_severity_ladder(clock: _Clock) -> None:
    severities = {
        kind: _labels(
            _webhook_payload("svc", EpisodeRecord(kind, 1.0, 1.0, "d", None), resolved=False)
        )["severity"]
        for kind in AlertKind
    }
    assert severities == {
        AlertKind.RESTART_FAILED: "error",
        AlertKind.BREAKER_OPEN: "critical",
        AlertKind.CUSTODY_HELD: "warning",
    }


def test_payload_validates_against_the_ingest_schema(clock: _Clock) -> None:
    from gateway.alerts.schemas import AlertWebhookPayload

    record = EpisodeRecord(AlertKind.BREAKER_OPEN, 1000.0, 1000.0, "down", None)
    for resolved in (False, True):
        parsed = AlertWebhookPayload.model_validate(
            _webhook_payload("svc", record, resolved=resolved)
        )
        assert parsed.source == "ava-root"
        alert = parsed.alerts[0]
        assert alert.starts_at, (
            "camelCase startsAt must survive: a snake_case key is dropped silently"
        )


# -- the health monitor integration ---------------------------------------------


class _StubSupervisor:
    """RevivalHost stand-in for one unit: an active generation, no deferrals."""

    def __init__(self) -> None:
        self.restart_calls = 0
        self.generation = (OwnedProcess(42, 100.0, None), 0.0)

    async def restart(self, unit_id: str) -> dict[str, object]:
        self.restart_calls += 1
        return {"verb": "restart", "units": []}

    def health_generation(self, unit_id: str) -> tuple[OwnedProcess, float]:
        return self.generation

    def revival_deferral(self, unit_id: str) -> str | None:
        return None

    def unit_alert_facts(self, unit_id: str) -> UnitAlertFacts:
        return UnitAlertFacts(intent_running=True, restart_failed=None, custody_held=False)


async def test_health_rounds_fire_and_resolve_one_episode(
    tmp_path: Path, recorder: _Recorder
) -> None:
    """The monitor's tail pass drives the router: breaker open -> fire, alive -> resolve."""
    from services.ava_root.health import HealthConfig, HealthMonitor
    from services.ava_root.probes import ProbeRegistry

    verdict = [DaemonProbe.down("no")]
    registry = ProbeRegistry()
    registry.register("svc", lambda: verdict[0])
    posts: list[bool] = []

    class _Notifier:
        def notify(self, unit: str, record: EpisodeRecord, *, resolved: bool) -> bool:
            posts.append(resolved)
            return True

    config = HealthConfig(
        interval_s=0.01,
        startup_grace_s=0.0,
        verify_deadline_s=0.05,
        verify_interval_s=0.001,
        failures_before_restart=1,
        backoff_base_s=600.0,
        backoff_cap_s=600.0,
        breaker_rounds=3,
    )
    monitor = HealthMonitor(
        _StubSupervisor(),
        registry,
        config=config,
        alerts=AlertRouter(tmp_path, notifier=_Notifier()),
    )
    await monitor.run_round()  # 1: restart attempt, not verified
    await monitor.run_round()  # 2: backing off
    await monitor.run_round()  # 3: the third non-alive round opens the breaker
    fired = recorder.events("root_unit_alert_fired")
    assert len(fired) == 1
    assert fired[0]["kind"] == "breaker_open"
    assert fired[0]["delivery"] == "posted"
    assert posts == [False]

    verdict[0] = DaemonProbe.up("ok")
    await monitor.run_round()  # alive: the breaker closes and the episode resolves
    resolved = recorder.events("root_unit_alert_resolved")
    assert len(resolved) == 1
    assert resolved[0]["kind"] == "breaker_open"
    assert posts[-1] is True
