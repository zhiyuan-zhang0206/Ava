"""The caller-side stop-incomplete recovery (task #3942), alone and in the leg.

`cli/commands/_update_stop_recovery.py` attempts exactly ONE bounded internal
start, and only when the episode is provably this leg's own; every other
reading must be a single verdicted decline, and the kill-switch's off state
must stay silent (the pre-#3942 behaviour). The wiring tests drive the leg's
post-stop exit with the helper stubbed to pin the call order (capture BEFORE
the stop, recovery after it), the episode hand-off, and the leg's return value
in both directions.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cli.commands import _update_agent_runner as runner_mod
from cli.commands import _update_stop_recovery as stop_recovery

_EPISODE = ("update:holder", datetime(2026, 9, 21, 5, 0, tzinfo=UTC))
_OTHER_EPISODE = ("someone-else", datetime(2026, 9, 21, 5, 1, tzinfo=UTC))
_GENERATION = "generation-1"


def _hold(phase: str = "stopping", *, episode: tuple[str, datetime] = _EPISODE) -> SimpleNamespace:
    """A pause-owner snapshot carrying `episode` at `phase`."""
    return SimpleNamespace(
        holder=episode[0],
        acquired_at=episode[1],
        maintenance=SimpleNamespace(phase=phase),
        matches=lambda holder, acquired_at: (holder, acquired_at) == episode,
    )


def _handoff(status: str = "running", generation: str = _GENERATION) -> SimpleNamespace:
    return SimpleNamespace(status=status, generation=generation)


def _state(
    *, posture: str = "paused", stranded_since: object = None, stranded_attempts: int = 0
) -> SimpleNamespace:
    return SimpleNamespace(
        posture=posture,
        stranded_hold_since=stranded_since,
        stranded_hold_attempts=stranded_attempts,
    )


def _completed(returncode: int) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode)


class _Runner:
    """Captures `subprocess.run` calls and returns (or raises) a scripted result."""

    def __init__(self, result: object) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self._result = result

    def run(self, argv: list[str], **kwargs: Any) -> object:
        self.calls.append((argv, kwargs))
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


def _stub_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    holds: tuple[object, ...],
    handoffs: tuple[object, ...] = (),
    owner_live: bool = True,
    states: tuple[object, ...] = (_state(), _state(posture="idle")),
    run_result: object = _completed(0),
    enabled: bool = True,
    timeout_s: float = 120.0,
) -> tuple[_Runner, list[tuple[str, str, float]]]:
    """Install the helper's outside world; return (runner stub, telemetry details)."""
    from shared import host_deploy_state, maintenance, updater_handoff
    from shared.config import settings

    hold_it = iter(holds)

    def _snapshot() -> object:
        value = next(hold_it)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(maintenance, "snapshot", _snapshot)

    if handoffs:
        handoff_it = iter(handoffs)

        def _read() -> object:
            return next(handoff_it)

        monkeypatch.setattr(updater_handoff, "read", _read)

        def _owner_is_live(_handoff: object) -> bool:
            return owner_live

        monkeypatch.setattr(updater_handoff, "owner_is_live", _owner_is_live)

    state_it = iter(states)

    def _read_state(machine: object = None, *, conn: object = None) -> object:
        return next(state_it)

    monkeypatch.setattr(host_deploy_state, "read", _read_state)

    details: list[tuple[str, str, float]] = []

    def _record(group: str, name: str, value: float) -> None:
        details.append((group, name, value))

    monkeypatch.setattr(stop_recovery, "record_detail", _record)
    runner = _Runner(run_result)
    monkeypatch.setattr(stop_recovery, "subprocess", runner)
    monkeypatch.setattr(settings.gateway, "stop_incomplete_recovery", enabled)
    monkeypatch.setattr(settings.gateway, "stop_incomplete_recovery_timeout_seconds", timeout_s)
    return runner, details


@pytest.mark.parametrize("phase", ["stopping", "stopped"])
def test_one_bounded_start_recovers_a_half_done_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], phase: str
) -> None:
    """A post-stop phase with the leg's own live episode spends exactly one start."""
    runner, details = _stub_environment(
        monkeypatch,
        holds=(_hold(phase),),
        handoffs=(_handoff(),),
        timeout_s=7.5,
    )
    monkeypatch.setenv("AVA_CONFIG_FETCH", "skip")
    monkeypatch.setenv("AVA_CONFIG_SOURCE", "somewhere")
    repo = tmp_path / "repo"
    ava_bin = tmp_path / "ava"

    assert stop_recovery.recover_incomplete_stop(
        repo, ava_bin, stop_rc=5, episode=_EPISODE, handoff_generation=_GENERATION
    )

    assert len(runner.calls) == 1
    argv, kwargs = runner.calls[0]
    assert argv == [str(ava_bin), "start", "--persist-services", "--updater-telemetry"]
    assert kwargs["cwd"] == repo
    assert kwargs["timeout"] == 7.5
    assert "AVA_CONFIG_FETCH" not in kwargs["env"]
    assert "AVA_CONFIG_SOURCE" not in kwargs["env"]
    assert details == [("updater_stop_recovery", "recovered", 1.0)]
    assert "recovered the half-stopped host" in capsys.readouterr().out


def test_the_arm_never_spends_the_os_recovery_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Q1': the caller arm only READS host_deploy_state (design v0.2)."""
    from shared import host_deploy_state

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the caller arm must not write host_deploy_state")

    for name in (
        "clear_stranded_hold",
        "finish_stranded_recovery",
        "mark_stranded_hold",
        "reserve_stranded_recovery",
        "set_posture",
    ):
        monkeypatch.setattr(host_deploy_state, name, _forbidden)

    runner, _details = _stub_environment(monkeypatch, holds=(_hold(),), handoffs=(_handoff(),))
    assert stop_recovery.recover_incomplete_stop(
        tmp_path, tmp_path / "ava", stop_rc=1, episode=_EPISODE, handoff_generation=_GENERATION
    )
    assert len(runner.calls) == 1


def test_switch_off_is_silent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`AVA_STOP_INCOMPLETE_RECOVERY=false` is the pre-#3942 behaviour: no line, no event."""
    runner, details = _stub_environment(monkeypatch, holds=(), enabled=False)

    assert not stop_recovery.recover_incomplete_stop(
        tmp_path, tmp_path / "ava", stop_rc=5, episode=_EPISODE, handoff_generation=_GENERATION
    )
    assert runner.calls == []
    assert details == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize(
    ("label", "holds", "handoffs", "slug"),
    [
        ("hold-unreadable", (RuntimeError("journal torn"),), (), "hold-unreadable"),
        ("hold-vanished", (None,), (), "no-hold"),
        ("foreign-generation", (_hold(episode=_OTHER_EPISODE),), (_handoff(),), "foreign-hold"),
        ("phase-starting", (_hold("starting"),), (_handoff(),), "phase-starting"),
        ("phase-drained", (_hold("drained"),), (_handoff(),), "phase-drained"),
        ("handoff-pending", (_hold(),), (_handoff(status="pending"),), "handoff-absent"),
        (
            "handoff-other-generation",
            (_hold(),),
            (_handoff(generation="not-ours"),),
            "handoff-absent",
        ),
    ],
)
def test_declines_without_touching_the_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    label: str,
    holds: tuple[object, ...],
    handoffs: tuple[object, ...],
    slug: str,
) -> None:
    """Every gate refusal: one verdicted decline, zero starts."""
    runner, details = _stub_environment(monkeypatch, holds=holds, handoffs=handoffs)

    assert not stop_recovery.recover_incomplete_stop(
        tmp_path, tmp_path / "ava", stop_rc=5, episode=_EPISODE, handoff_generation=_GENERATION
    )
    assert runner.calls == [], label
    assert details == [("updater_stop_recovery", f"skipped-{slug}", 1.0)]
    assert "skipped" in capsys.readouterr().err


def test_declines_when_no_episode_was_captured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner, details = _stub_environment(monkeypatch, holds=())

    assert not stop_recovery.recover_incomplete_stop(
        tmp_path, tmp_path / "ava", stop_rc=5, episode=None, handoff_generation=_GENERATION
    )
    assert runner.calls == []
    assert details == [("updater_stop_recovery", "skipped-no-hold", 1.0)]
    assert "skipped" in capsys.readouterr().err


def test_declines_when_the_handoff_owner_is_dead(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner, details = _stub_environment(
        monkeypatch, holds=(_hold(),), handoffs=(_handoff(),), owner_live=False
    )

    assert not stop_recovery.recover_incomplete_stop(
        tmp_path, tmp_path / "ava", stop_rc=5, episode=_EPISODE, handoff_generation=_GENERATION
    )
    assert runner.calls == []
    assert details == [("updater_stop_recovery", "skipped-handoff-dead", 1.0)]


def test_declines_on_a_declared_stranded_hold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The OS arm declared (or already spent) this episode's attempt: defer."""
    stranded = _state(stranded_since=datetime(2026, 9, 21, 4, 30, tzinfo=UTC))
    runner, details = _stub_environment(
        monkeypatch, holds=(_hold(),), handoffs=(_handoff(),), states=(stranded,)
    )

    assert not stop_recovery.recover_incomplete_stop(
        tmp_path, tmp_path / "ava", stop_rc=5, episode=_EPISODE, handoff_generation=_GENERATION
    )
    assert runner.calls == []
    assert details == [("updater_stop_recovery", "skipped-stranded", 1.0)]


def test_a_failed_attempt_defers_without_retrying(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner, details = _stub_environment(
        monkeypatch, holds=(_hold(),), handoffs=(_handoff(),), run_result=_completed(4)
    )

    assert not stop_recovery.recover_incomplete_stop(
        tmp_path, tmp_path / "ava", stop_rc=5, episode=_EPISODE, handoff_generation=_GENERATION
    )
    assert len(runner.calls) == 1
    assert details == [("updater_stop_recovery", "failed-start-rc-4", 1.0)]


def test_a_timed_out_attempt_defers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner, details = _stub_environment(
        monkeypatch,
        holds=(_hold(),),
        handoffs=(_handoff(),),
        run_result=subprocess.TimeoutExpired(cmd="ava start", timeout=120.0),
    )

    assert not stop_recovery.recover_incomplete_stop(
        tmp_path, tmp_path / "ava", stop_rc=5, episode=_EPISODE, handoff_generation=_GENERATION
    )
    assert len(runner.calls) == 1
    assert details == [("updater_stop_recovery", "failed-timeout", 1.0)]


def test_a_start_that_leaves_the_host_paused_defers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """rc==0 is not enough: the posture row must read back idle (win precedent)."""
    runner, details = _stub_environment(
        monkeypatch,
        holds=(_hold(),),
        handoffs=(_handoff(),),
        states=(_state(), _state(posture="paused")),
    )

    assert not stop_recovery.recover_incomplete_stop(
        tmp_path, tmp_path / "ava", stop_rc=5, episode=_EPISODE, handoff_generation=_GENERATION
    )
    assert len(runner.calls) == 1
    assert details == [("updater_stop_recovery", "failed-not-serving", 1.0)]


# ── the leg's wiring: capture before the stop, one recovery at the exit ────────


def _install_leg_stubs(
    monkeypatch: pytest.MonkeyPatch, *, stop_rc: int, recovered: bool, events: list[str]
) -> None:
    import cli.commands as _cli

    class _FakeBackend:
        def venv_launcher(self, _name: str, root: Path) -> Path:
            launcher = root / "ava"
            launcher.touch()
            return launcher

    class _FakeSubprocess:
        def run(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(returncode=0)

    def _probes() -> int:
        return 0

    def _readiness(*_args: object, **_kwargs: object) -> int:
        return 0

    def _quiesce(_mode: str) -> bool:
        return True

    monkeypatch.setattr(runner_mod, "platform_backend", _FakeBackend)
    monkeypatch.setattr(runner_mod, "subprocess", _FakeSubprocess())
    monkeypatch.setattr(_cli, "_preflight_probes", _probes)
    monkeypatch.setattr(_cli, "_preflight_start_readiness", _readiness)
    monkeypatch.setattr(_cli, "_quiesce_local_agents", _quiesce)

    def _do_stop(*_args: object, **_kwargs: object) -> int:
        events.append("stop")
        return stop_rc

    monkeypatch.setattr(_cli, "_do_stop", _do_stop)

    def _capture() -> tuple[str, datetime]:
        events.append("capture")
        return _EPISODE

    def _recover(*_args: object, **kwargs: object) -> bool:
        events.append("recover")
        assert kwargs["stop_rc"] == stop_rc
        assert kwargs["episode"] == _EPISODE
        assert kwargs["handoff_generation"] == _GENERATION
        return recovered

    monkeypatch.setattr(stop_recovery, "capture_stop_episode", _capture)
    monkeypatch.setattr(stop_recovery, "recover_incomplete_stop", _recover)


@pytest.mark.parametrize(("recovered", "expected"), [(True, 0), (False, 1)])
def test_the_leg_continues_as_success_only_when_recovery_reports_serving(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, recovered: bool, expected: int
) -> None:
    """The episode is captured pre-stop; recovery runs at the stop's failure exit."""
    events: list[str] = []
    _install_leg_stubs(monkeypatch, stop_rc=1, recovered=recovered, events=events)

    rc = runner_mod._run_agent_runner_self_update_inner(
        tmp_path, restart_only=True, mode="none", handoff_generation=_GENERATION
    )
    assert rc == expected
    assert events == ["capture", "stop", "recover"]


def test_the_leg_leaves_a_successful_stop_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    _install_leg_stubs(monkeypatch, stop_rc=0, recovered=True, events=events)

    rc = runner_mod._run_agent_runner_self_update_inner(
        tmp_path, restart_only=True, mode="none", handoff_generation=_GENERATION
    )
    assert rc == 0
    assert events == ["capture", "stop"]
