"""`ava start`'s GUI-domain handover (task #3348, R2b).

A start chain outside the macOS GUI login session must not bring services up in
place (they would inherit its launchd domain): an operator-shaped start hands
the bring-up to the cluster's GUI-domain job and waits for readiness with the
same exit contract. These pin the shape gate, the fail-open paths and the
observer's verdict — the domain probe, the job ensure and the readiness wait
are all faked; no test ever kicks a real launchd job.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from cli.commands import _start_gui_handover as ho
from cli.commands._probe import ReadinessWait
from cli.commands._repo import ServiceSpec
from shared.config import settings
from shared.exit_codes import SERVICES_NOT_READY_EXIT_CODE

_AUTOSTART_DETAIL = "com.ava.t.autostart running in gui/501 (pid 123)"


def _spec(session: str) -> ServiceSpec:
    """A ServiceSpec stand-in carrying just the session name the seams read."""
    return cast(ServiceSpec, SimpleNamespace(session=session))


class _FakeTime:
    """`time` stand-in for the module: sleeps advance the clock and are recorded."""

    def __init__(self) -> None:
        self.t = 1_000_000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class _Recorder:
    """Every seam the handover touches, recorded instead of executed."""

    def __init__(self) -> None:
        self.clock = _FakeTime()
        self.ensure_calls = 0
        self.kick_ok = True
        self.kick_detail = _AUTOSTART_DETAIL
        self.waits: list[tuple[tuple[ServiceSpec, ...], float]] = []
        self.wait_result = ReadinessWait((), 0.1, False)
        self.launch_step = True
        self.sessions_alive = False
        self.refuse_rc = 0
        self.waiver: str | None = None
        self.unready_printed = 0
        self.notified: list[tuple[tuple[object, ...], bool]] = []
        self.resolved: list[tuple[tuple[object, ...], bool]] = []
        self.status_printed = 0
        self.skip_calls: list[dict[str, object]] = []


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """All gates pass and every side effect is a recorder."""
    rec = _Recorder()
    monkeypatch.setattr(ho, "time", rec.clock)
    monkeypatch.setattr(
        ho,
        "_rehomeable_domain",
        lambda _roles: "Background",  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(settings.general, "start_gui_handover", True)
    monkeypatch.setattr("shared.os_cron.os_jobs_enabled", lambda: True)

    def _skip(_operator_skip: object, **kwargs: object) -> set[str]:
        rec.skip_calls.append(dict(kwargs))
        return set()

    monkeypatch.setattr("shared.disabled_services.resolve_launch_skip", _skip)
    monkeypatch.setattr(
        "cli.commands._session_lifecycle._launch_roster",
        lambda _roles, _skip: (  # pyright: ignore[reportUnknownArgumentType]
            _spec("t-ops"),
            _spec("t-watchdog"),
        ),
    )
    monkeypatch.setattr(
        "cli.commands.start._refuse_occupied_health_ports",
        lambda _roster: rec.refuse_rc,  # pyright: ignore[reportUnknownArgumentType]
    )

    def _ensure() -> tuple[bool, str]:
        rec.ensure_calls += 1
        return rec.kick_ok, rec.kick_detail

    monkeypatch.setattr("shared.os_autostart.ensure_via_gui_domain", _ensure)

    import cli.commands as ns

    monkeypatch.setattr(
        ho,
        "_job_reached_launch_step",
        lambda _t0: rec.launch_step,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        ns,
        "_has_session",
        lambda _name: rec.sessions_alive,  # pyright: ignore[reportUnknownArgumentType]
    )

    def _wait(specs: tuple[ServiceSpec, ...], timeout_s: float) -> ReadinessWait:
        rec.waits.append((tuple(specs), timeout_s))
        return rec.wait_result

    monkeypatch.setattr(ns, "_wait_for_services_ready", _wait)

    def _print_unready(_wait: ReadinessWait, _timeout_s: float) -> None:
        rec.unready_printed += 1

    monkeypatch.setattr(ns, "_print_unready_services", _print_unready)
    monkeypatch.setattr(
        ns,
        "_print_non_critical_unready_services",
        lambda _unready: None,  # pyright: ignore[reportUnknownArgumentType]
    )

    def _notify(unready: tuple[ServiceSpec, ...], *, im_enabled: bool) -> None:
        rec.notified.append((tuple(unready), im_enabled))

    monkeypatch.setattr(ns, "_notify_non_critical_unready_services", _notify)

    def _recovered(_started: object, _non_critical: object) -> tuple[ServiceSpec, ...]:
        return ()

    monkeypatch.setattr(ns, "_recovered_non_critical_specs", _recovered)

    def _resolve(recovered: tuple[object, ...], *, im_enabled: bool) -> None:
        rec.resolved.append((tuple(recovered), im_enabled))

    monkeypatch.setattr(ns, "_resolve_recovered_non_critical_alerts", _resolve)

    def _status() -> None:
        rec.status_printed += 1

    monkeypatch.setattr("cli.commands.status.cmd_status", _status)

    def _waiver(_roles: object, **_kwargs: object) -> str | None:
        return rec.waiver

    monkeypatch.setattr("cli.commands.start._readiness_waiver", _waiver)
    return rec


def _hand_over(rec: _Recorder, **overrides: object) -> int | None:
    kwargs: dict[str, object] = {
        "disabled_services": (),
        "persist_services": True,
        "updater_telemetry": False,
        "parent_handoff": False,
        "readiness_gate": True,
    }
    kwargs.update(overrides)
    return ho._maybe_handover_start(frozenset({"agent-runner"}), **kwargs)  # type: ignore[arg-type]


def test_hands_over_and_observes_with_the_start_contract(
    env: _Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    """The happy path: kick the GUI-domain job, wait for the job's launch step,
    settle, then judge the probes and return the start exit contract."""
    assert _hand_over(env) == 0
    assert env.ensure_calls == 1
    assert env.skip_calls[0]["persist"] is True
    assert env.clock.slept == [ho._LAUNCH_SETTLE_S]
    assert len(env.waits) == 1
    specs, timeout_s = env.waits[0]
    assert [s.session for s in specs] == ["t-ops", "t-watchdog"]
    assert timeout_s > 0
    assert env.status_printed == 1
    out = capsys.readouterr()
    assert "handed the bring-up to the cluster's GUI-domain job" in out.out
    assert env.kick_detail in out.out


def test_already_serving_skips_the_launch_signal(env: _Recorder) -> None:
    """Every judged session is already alive (an idempotent re-start): no need
    to wait for the job's launch step — judge the probes directly."""
    env.sessions_alive = True
    assert _hand_over(env) == 0
    assert env.clock.slept == []
    assert len(env.waits) == 1


def test_launch_signal_timeout_still_judges_the_probes(
    env: _Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    """A job that never reaches its launch step (a failed converge, a dead job)
    must not be reported as a readiness timeout: fall through to the probes and
    say where the job's own log is."""
    env.launch_step = False
    assert _hand_over(env) == 0
    assert ho._LAUNCH_SETTLE_S not in env.clock.slept
    assert len(env.waits) == 1
    assert "has not reached its launch step" in capsys.readouterr().err


def test_unready_verdict_returns_the_not_ready_exit(env: _Recorder) -> None:
    env.wait_result = ReadinessWait((_spec("t-ops"),), 180.0, False)
    assert _hand_over(env) == SERVICES_NOT_READY_EXIT_CODE
    assert env.unready_printed == 1


def test_readiness_waiver_exits_zero(env: _Recorder, capsys: pytest.CaptureFixture[str]) -> None:
    env.wait_result = ReadinessWait((_spec("t-ops"),), 180.0, False)
    env.waiver = "a live update lease"
    assert _hand_over(env) == 0
    assert "exiting 0 anyway" in capsys.readouterr().err


def test_non_critical_unready_is_reported_and_notified(env: _Recorder) -> None:
    env.wait_result = ReadinessWait((), 45.0, False, non_critical_unready=(_spec("t-pitr"),))
    assert _hand_over(env) == 0
    assert len(env.notified) == 1
    assert env.notified[0][1] is True  # im_enabled=readiness_gate


@pytest.mark.parametrize(
    "overrides",
    [
        {"parent_handoff": True},
        {"updater_telemetry": True},
        {"persist_services": False},
        {"disabled_services": ("t-frontend",)},
    ],
)
def test_internal_shapes_never_hand_over(env: _Recorder, overrides: dict[str, object]) -> None:
    """Credential-marker and internal-flag shapes are not equivalent to the
    canonical GUI job, so they keep the old (warn + in-place) behavior."""
    assert _hand_over(env, **overrides) is None
    assert env.ensure_calls == 0


def test_knob_off_falls_back(env: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.general, "start_gui_handover", False)
    assert _hand_over(env) is None
    assert env.ensure_calls == 0


def test_without_os_jobs_there_is_nothing_to_hand_over(
    env: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shared.os_cron.os_jobs_enabled", lambda: False)
    assert _hand_over(env) is None
    assert env.ensure_calls == 0


def test_not_rehomeable_skips_quietly(env: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ho,
        "_rehomeable_domain",
        lambda _roles: None,  # pyright: ignore[reportUnknownArgumentType]
    )
    assert _hand_over(env) is None
    assert env.ensure_calls == 0


def test_occupied_health_port_refuses_without_kicking(env: _Recorder) -> None:
    """The pre-bind gate runs before the handover: a unit that cannot bind its
    health ports refuses here, exactly like a normal start, and no job is
    kicked into the same refusal."""
    env.refuse_rc = 1
    assert _hand_over(env) == 1
    assert env.ensure_calls == 0


def test_failed_ensure_falls_open(env: _Recorder, capsys: pytest.CaptureFixture[str]) -> None:
    env.kick_ok = False
    env.kick_detail = "no autostart plist at /x.plist"
    assert _hand_over(env) is None
    assert "handover unavailable" in capsys.readouterr().err
