"""The continuation phase, folded into `cli.commands._managed_writer_hop` (task #4129, channel E).

Pins both coordinator steps: the drive fan-out + readback wait, and the
commit-tail fan-out + committed-slot wait. The unit answers are stubbed at the
RPC boundary (`ops.cluster_rpc.dispatch_to_machine`) and at the database read
(`shared.db.connect` / the activation module), so every branch of the wait
ladders -- acknowledgement drift, the one retryable in-flight refusal,
tolerance of the journal's not-ready refusal, the early recovered-predecessor
failure, and both deadline details -- is exercised without a live fleet.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from cli.commands import _managed_writer_hop as continue_mod
from cli.commands import _managed_writer_mode as mode_mod
from cli.commands._managed_writer_dispatch import assemble_phase_inputs
from cli.commands._managed_writer_hop import (
    CollectorInput,
    ContinueUnitInput,
    ManagedWriterPhaseInput,
)
from cli.commands._update_bootstrap import BootstrapHopRequest
from cli.commands._update_normal_release import NormalReleaseRequest
from ops.cluster_rpc import ClusterOpFailed, ClusterOpUnreachable
from ops.ops_normal_continue import NormalContinueResult
from ops.rpc_prepare_dispatch import prepared_hop_name
from shared import rollout_telemetry
from shared.managed_writer_barrier import ManagedWriterBarrierError, RolloutIdentity
from tests.cli.test_managed_writer_dispatch import (
    CANDIDATE_DIGEST,
    SCHEMA,
    VALID_UNTIL,
    _hop_world,
    _published,
)
from tests.cli.test_managed_writer_mode import (
    _CHECKED,
    _WIRING,
    _capture_events,
    _clear_guard,
    _disable,
    _enable,
    _ready_guards,
)
from tests.cli.test_managed_writer_mode import (
    _phase_input as _mode_phase_input,
)

ARTIFACT = "a" * 64
OPERATION = RolloutIdentity(
    holder="gateway:pid1", acquired_at=datetime(2026, 9, 20, tzinfo=UTC), target_sha="0" * 40
)
CHALLENGE = UUID(int=3)


@pytest.fixture(autouse=True)
def _fresh_mode_state() -> Iterator[None]:
    """The mode decision is cached per process; each moved wiring pin starts fresh."""
    mode_mod._reset_for_tests()
    rollout_telemetry.deactivate()
    yield
    mode_mod._reset_for_tests()
    rollout_telemetry.deactivate()


def _unit(machine: str = "runner-a", home: str = "/ava-a") -> ContinueUnitInput:
    return ContinueUnitInput(
        machine=machine,
        home=home,
        ops_url=f"http://{machine}",
        artifact_digest=ARTIFACT,
        request_path=f"{home}/run/normal-request-{'c' * 32}.json",
    )


def _phase_input(
    units: tuple[ContinueUnitInput, ...], *, valid_until: datetime | None = None
) -> ManagedWriterPhaseInput:
    return ManagedWriterPhaseInput(
        hop_plans=(),
        collector=CollectorInput(
            operation=OPERATION,
            challenge=CHALLENGE,
            valid_until=valid_until or datetime.now(UTC) + timedelta(minutes=5),
            candidate_digest="9" * 64,
            units=(),
        ),
        continue_units=units,
    )


def _ack(unit: ContinueUnitInput, **overrides: object) -> dict[str, Any]:
    wire = NormalContinueResult(
        machine=unit.machine,
        home=unit.home,
        session="ava-updater",
        log=f"{unit.home}/logs/updater-123.log",
    ).model_dump(mode="json")
    return {**wire, **overrides}


class _Answers:
    """A per-machine answer script; the last answer repeats once drained."""

    def __init__(self, answer: Any) -> None:
        if isinstance(answer, list):
            self._queue: list[Any] = answer
        else:
            self._queue = [answer]

    def next(self) -> Any:
        if len(self._queue) > 1:
            return self._queue.pop(0)
        return self._queue[0]


def _stub_dispatch(
    monkeypatch: pytest.MonkeyPatch, answers: dict[str, Any]
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    script = {machine: _Answers(answer) for machine, answer in answers.items()}

    async def dispatch(
        target_machine: str, kind: str, payload: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        calls.append({"target": target_machine, "kind": kind, "payload": payload, "kwargs": kwargs})
        answer = script[target_machine].next()
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr("ops.cluster_rpc.dispatch_to_machine", dispatch)
    return calls


def _in_flight() -> ClusterOpFailed:
    return ClusterOpFailed(
        {"error": "ClusterUpdateInProgress: orchestration session 'ava-updater' already exists"}
    )


class _ReadbackScript:
    """The stubbed `read_pending_unit_readbacks`; the last step repeats."""

    def __init__(self, *steps: Any) -> None:
        self.steps = list(steps)
        self.calls = 0

    def __call__(self, _conn: object, _operation: object, _challenge: object) -> Any:
        self.calls += 1
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, Exception):
            raise step
        return step


def _readback(machine: str, home: str) -> Any:
    return SimpleNamespace(
        selector=SimpleNamespace(unit=SimpleNamespace(machine=machine, home=home))
    )


def _stub_readbacks(monkeypatch: pytest.MonkeyPatch, script: _ReadbackScript) -> _ReadbackScript:
    class _Conn:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_exc: object) -> bool:
            return False

    def connect(**_kwargs: object) -> _Conn:
        return _Conn()

    monkeypatch.setattr("shared.db.connect", connect)
    monkeypatch.setattr("shared.managed_writer_activation.read_pending_unit_readbacks", script)
    return script


def _fast_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.config import settings

    monkeypatch.setattr(settings.gateway, "update_managed_writer_hop_poll_seconds", 0.001)


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    def sleeper(_seconds: float) -> None:
        return None

    monkeypatch.setattr(continue_mod.time, "sleep", sleeper)


# ── the drive: fan-out + acknowledgements ────────────────────────────────────


def test_drive_sends_each_unit_and_acknowledges(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    units = (_unit("runner-a", "/ava-a"), _unit("runner-b", "/ava-b"))
    _fast_polls(monkeypatch)
    calls = _stub_dispatch(monkeypatch, {"runner-a": _ack(units[0]), "runner-b": _ack(units[1])})
    script = _stub_readbacks(
        monkeypatch,
        _ReadbackScript([_readback("runner-a", "/ava-a"), _readback("runner-b", "/ava-b")]),
    )

    rc = continue_mod.drive_continuation(_phase_input(units))

    assert rc == 0
    assert [call["kind"] for call in calls] == ["cluster_normal_continue"] * 2
    assert calls[0]["payload"] == {
        "continue_request": units[0].request_path,
        "step": "drive",
        "artifact_digest": ARTIFACT,
    }
    assert calls[1]["payload"]["step"] == "drive"
    assert calls[0]["kwargs"]["ops_url"] == "http://runner-a"
    assert calls[0]["kwargs"]["timeout_s"] is None
    assert script.calls >= 1
    out = capsys.readouterr().out
    assert "runner-a: continuation session ava-updater started" in out
    assert "runner-b: continuation session ava-updater started" in out
    assert "\u2713 managed-writer continuation: 2 units read back" in out


@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        ({"machine": "runner-x"}, "another machine"),
        ({"home": "/ava-x"}, "another unit home"),
        ({"session": "ava-rollout"}, "not the updater session"),
        ({"log": "/elsewhere/logs/updater-1.log"}, "not inside the unit's logs"),
    ],
)
def test_drive_refuses_every_ack_drift(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    overrides: dict[str, object],
    detail: str,
) -> None:
    unit = _unit()
    _fast_polls(monkeypatch)
    _stub_dispatch(monkeypatch, {"runner-a": _ack(unit, **overrides)})
    script = _stub_readbacks(monkeypatch, _ReadbackScript(ManagedWriterBarrierError("nope")))

    rc = continue_mod.drive_continuation(_phase_input((unit,)))

    assert rc == 1
    assert detail in capsys.readouterr().err
    assert script.calls == 0, "a refused ack must not reach the readback wait"


def test_drive_counts_unreachable_and_failed_ops(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    units = (_unit("runner-a", "/ava-a"), _unit("runner-b", "/ava-b"))
    _fast_polls(monkeypatch)
    calls = _stub_dispatch(
        monkeypatch,
        {
            "runner-a": ClusterOpUnreachable("offline"),
            "runner-b": ClusterOpFailed({"error": "ReleaseRejectedError: refused", "detail": "x"}),
        },
    )

    rc = continue_mod.drive_continuation(_phase_input(units))

    assert rc == 1
    assert len(calls) == 2, "a deterministic refusal fails at once, never retried"
    err = capsys.readouterr().err
    assert "runner-a: unreachable: offline" in err
    assert "runner-b: op failed:" in err


def test_drive_retries_the_in_flight_refusal_until_the_session_clears(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unit = _unit()
    _fast_polls(monkeypatch)
    calls = _stub_dispatch(monkeypatch, {"runner-a": [_in_flight(), _ack(unit)]})
    _stub_readbacks(monkeypatch, _ReadbackScript([_readback("runner-a", "/ava-a")]))

    rc = continue_mod.drive_continuation(_phase_input((unit,)))

    assert rc == 0
    assert [call["payload"]["step"] for call in calls] == ["drive", "drive"]
    out = capsys.readouterr().out
    assert "an updater session is still in flight; retrying until the sealed window closes" in out


def test_drive_fails_when_the_window_closes_with_a_unit_still_in_flight(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unit = _unit()
    _fast_polls(monkeypatch)
    _no_sleep(monkeypatch)
    calls = _stub_dispatch(monkeypatch, {"runner-a": _in_flight()})

    rc = continue_mod.drive_continuation(
        _phase_input((unit,), valid_until=datetime.now(UTC) + timedelta(seconds=0.05))
    )

    assert rc == 1
    assert len(calls) >= 2
    err = capsys.readouterr().err
    assert (
        "the continuation dispatch hit the sealed window with units not dispatched: runner-a" in err
    )


def test_drive_refuses_before_dispatching_when_the_window_is_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unit = _unit()
    _fast_polls(monkeypatch)
    calls = _stub_dispatch(monkeypatch, {"runner-a": _ack(unit)})

    rc = continue_mod.drive_continuation(
        _phase_input((unit,), valid_until=datetime.now(UTC) - timedelta(minutes=1))
    )

    assert rc == 1
    assert calls == []
    assert "hit the sealed window" in capsys.readouterr().err


# ── the readback wait ────────────────────────────────────────────────────────


def test_readback_wait_tolerates_the_journal_not_ready_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    units = (_unit("runner-a", "/ava-a"), _unit("runner-b", "/ava-b"))
    _fast_polls(monkeypatch)
    _stub_dispatch(monkeypatch, {"runner-a": _ack(units[0]), "runner-b": _ack(units[1])})
    script = _stub_readbacks(
        monkeypatch,
        _ReadbackScript(
            ManagedWriterBarrierError("pending normal startup authority is missing"),
            [_readback("runner-a", "/ava-a"), _readback("runner-b", "/ava-b")],
        ),
    )

    rc = continue_mod.drive_continuation(_phase_input(units))

    assert rc == 0
    assert script.calls == 2
    assert "\u2713 managed-writer continuation: 2 units read back" in capsys.readouterr().out


def test_readback_wait_prints_the_count_when_it_changes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    units = (_unit("runner-a", "/ava-a"), _unit("runner-b", "/ava-b"))
    _fast_polls(monkeypatch)
    _stub_dispatch(monkeypatch, {"runner-a": _ack(units[0]), "runner-b": _ack(units[1])})
    _stub_readbacks(
        monkeypatch,
        _ReadbackScript(
            [_readback("runner-a", "/ava-a")],
            [_readback("runner-a", "/ava-a"), _readback("runner-b", "/ava-b")],
        ),
    )

    rc = continue_mod.drive_continuation(_phase_input(units))

    assert rc == 0
    out = capsys.readouterr().out
    assert "managed-writer continuation: 1 of 2 units read back" in out


@pytest.mark.parametrize(
    ("step", "recorded"),
    [
        (ManagedWriterBarrierError("not ready"), "0"),
        ([_readback("runner-a", "/ava-a")], "1"),
    ],
)
def test_readback_wait_times_out_with_the_recorded_count(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    step: Any,
    recorded: str,
) -> None:
    units = (_unit("runner-a", "/ava-a"), _unit("runner-b", "/ava-b"))
    _fast_polls(monkeypatch)
    _no_sleep(monkeypatch)
    _stub_dispatch(monkeypatch, {"runner-a": _ack(units[0]), "runner-b": _ack(units[1])})
    _stub_readbacks(monkeypatch, _ReadbackScript(step))

    rc = continue_mod.drive_continuation(
        _phase_input(units, valid_until=datetime.now(UTC) + timedelta(seconds=0.05))
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert (
        f"the continuation wait hit the sealed window with units not read back: {recorded} of 2 recorded"
        in err
    )


# ── the commit tails ─────────────────────────────────────────────────────────


def _tail_read(
    unit: ContinueUnitInput,
    *,
    journal_present: bool = True,
    journal_stage: str | None = "candidate_ready",
    normal_release_stage: str | None = "committed",
) -> dict[str, Any]:
    return {
        "machine": unit.machine,
        "home": unit.home,
        "journal_present": journal_present,
        "journal_stage": journal_stage,
        "normal_release_stage": normal_release_stage,
    }


def test_commit_tails_dispatch_then_wait_for_the_committed_slot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unit = _unit()
    _fast_polls(monkeypatch)
    calls = _stub_dispatch(monkeypatch, {"runner-a": [_ack(unit), _tail_read(unit)]})

    rc = continue_mod.commit_tails(_phase_input((unit,)))

    assert rc == 0
    assert calls[0]["payload"]["step"] == "commit"
    assert calls[0]["kwargs"]["ops_url"] == "http://runner-a"
    assert calls[1]["kind"] == "cluster_bootstrap_recovery_read"
    assert calls[1]["payload"] == {}
    assert calls[1]["kwargs"]["ops_url"] == "http://runner-a"
    out = capsys.readouterr().out
    assert "runner-a: commit-tail session ava-updater started" in out
    assert "runner-a: normal release committed" in out
    assert "\u2713 managed-writer tails: all 1 units committed" in out


def test_commit_tails_accepts_a_committed_and_disposed_slot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unit = _unit()
    _fast_polls(monkeypatch)
    _stub_dispatch(
        monkeypatch,
        {
            "runner-a": [
                _ack(unit),
                _tail_read(
                    unit, journal_present=False, journal_stage=None, normal_release_stage=None
                ),
            ]
        },
    )

    rc = continue_mod.commit_tails(_phase_input((unit,)))

    assert rc == 0
    out = capsys.readouterr().out
    assert "journal absent; the committed tail has been disposed" in out
    assert "\u2713 managed-writer tails: all 1 units committed" in out


def test_commit_tails_fails_early_on_a_recovered_bootstrap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unit = _unit()
    _fast_polls(monkeypatch)
    _stub_dispatch(
        monkeypatch,
        {
            "runner-a": [
                _ack(unit),
                _tail_read(unit, journal_stage="recovered", normal_release_stage=None),
            ]
        },
    )

    rc = continue_mod.commit_tails(_phase_input((unit,)))

    assert rc == 1
    assert (
        "the commit-tail wait: runner-a recovered its predecessor instead of committing its normal release"
        in capsys.readouterr().err
    )


def test_commit_tails_fails_early_on_a_foreign_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unit = _unit()
    _fast_polls(monkeypatch)
    _stub_dispatch(
        monkeypatch,
        {"runner-a": [_ack(unit), {**_tail_read(unit), "machine": "runner-x"}]},
    )

    rc = continue_mod.commit_tails(_phase_input((unit,)))

    assert rc == 1
    assert "the recovery read answered for another machine" in capsys.readouterr().err


def test_commit_tails_waits_through_observed_then_committed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unit = _unit()
    _fast_polls(monkeypatch)
    calls = _stub_dispatch(
        monkeypatch,
        {
            "runner-a": [
                _ack(unit),
                _tail_read(unit, normal_release_stage="observed"),
                _tail_read(unit),
            ]
        },
    )

    rc = continue_mod.commit_tails(_phase_input((unit,)))

    assert rc == 0
    assert len(calls) == 3
    out = capsys.readouterr().out
    assert "managed-writer tails: runner-a normal release stage=observed" in out


def test_commit_tails_times_out_with_the_last_states(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unit = _unit()
    _fast_polls(monkeypatch)
    _no_sleep(monkeypatch)
    _stub_dispatch(
        monkeypatch,
        {"runner-a": [_ack(unit), _tail_read(unit, normal_release_stage="observed")]},
    )

    rc = continue_mod.commit_tails(
        _phase_input((unit,), valid_until=datetime.now(UTC) + timedelta(seconds=0.05))
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert (
        "the commit-tail wait hit the sealed window with units not committed: "
        "runner-a=normal release stage=observed" in err
    )


# ── the silent empty roster ──────────────────────────────────────────────────


def test_an_empty_roster_is_a_silent_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rollout's begin may seal no continuation units; both steps must then
    return 0 without dialing anything (the wiring tests rely on this shape)."""
    calls: list[object] = []

    async def dispatch(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        calls.append(object())
        return {}

    def no_dial(**_kwargs: object) -> object:
        pytest.fail("no database dial on an empty roster")

    monkeypatch.setattr("ops.cluster_rpc.dispatch_to_machine", dispatch)
    monkeypatch.setattr("shared.db.connect", no_dial)
    phase_input = _phase_input(())

    assert continue_mod.drive_continuation(phase_input) == 0
    assert continue_mod.commit_tails(phase_input) == 0
    assert calls == []
    assert capsys.readouterr() == ("", "")


# ── the phase-input assembly's normal projection + continuation roster ───────


def test_assemble_phase_inputs_binds_the_normal_projection_and_continuation_roster() -> None:
    """The sealed request names the normal projection, and the continuation
    roster carries the same content-named entry per unit in plan order."""
    facts, targets, operation = _hop_world(now=datetime(2026, 9, 20, tzinfo=UTC))
    facts = [replace(facts[0], previous_selector='{"selector":1}\n'), facts[1]]
    challenge = UUID(int=11)

    phase_input = assemble_phase_inputs(
        facts,
        targets,
        operation=operation,
        valid_until=VALID_UNTIL,
        challenge=challenge,
        schema_digest=SCHEMA,
        candidate_digest=CANDIDATE_DIGEST,
    )

    plan = next(iter(phase_input.hop_plans))
    candidate_projection, request_projection, normal_projection = plan.projections
    assert plan.request_path == f"/ava-a/run/{request_projection.name}"
    assert normal_projection.name == prepared_hop_name("normal-request", normal_projection.content)
    request = BootstrapHopRequest.model_validate_json(request_projection.content)
    assert request.normal_release_path == f"/ava-a/run/{normal_projection.name}"
    normal = NormalReleaseRequest.model_validate_json(normal_projection.content)
    assert normal.context_path == f"/ava-a/run/{candidate_projection.name}"
    assert normal.unit == _published("runner-a", "/ava-a")
    assert normal.previous_selector == facts[0].previous_selector
    assert normal.predecessor == facts[0].hop_material.predecessor

    unit = phase_input.collector.units[0]
    assert unit.normal_request == normal_projection.content.encode("ascii")

    continuation = phase_input.continue_units
    assert [item.machine for item in continuation] == ["runner-a", "runner-b"]
    assert continuation[0].request_path == f"/ava-a/run/{normal_projection.name}"
    assert (continuation[0].home, continuation[0].ops_url, continuation[0].artifact_digest) == (
        "/ava-a",
        None,
        ARTIFACT,
    )


# ── the channel-E continuation wiring (task #4129 I6) ────────────────────────


@pytest.mark.parametrize("state", ["off", "blocked", "none"])
def test_continue_steps_skip_without_an_active_decision(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], state: str
) -> None:
    from cli.commands import _managed_writer_wiring as wiring

    _capture_events(monkeypatch)
    if state == "off":
        _disable(monkeypatch)
        mode_mod.decide_managed_writer_mode()
    elif state == "blocked":
        _enable(monkeypatch)
        _clear_guard(monkeypatch, _CHECKED)
        _clear_guard(monkeypatch, _WIRING)
        mode_mod.decide_managed_writer_mode()

    assert wiring._drive_managed_writer_continuation(None) == 0
    assert wiring._commit_managed_writer_tails(None) == 0
    captured = capsys.readouterr()
    assert "managed_writer_continue" not in captured.out
    assert "managed_writer_tails" not in captured.out
    if state == "none":
        # A call outside the rollout's read point is beaconed, never silent.
        assert "no mode decision recorded" in captured.err
    else:
        assert captured.err == ""


def test_continue_steps_beacon_without_the_phase_input_under_active(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reaching the channel-E positions without the begin chain's phase input is
    the shape the collection position refuses first; if one is reached anyway it
    skips with a visible beacon rather than refuse, and the commit seat's own
    all-unit gate still fail-closes."""
    from cli.commands import _managed_writer_wiring as wiring

    _ready_guards(monkeypatch)
    mode_mod.decide_managed_writer_mode()

    assert wiring._drive_managed_writer_continuation(None) == 0
    err = capsys.readouterr().err
    assert "managed-writer continue" in err
    assert "without the begin position's phase input" in err

    assert wiring._commit_managed_writer_tails(None) == 0
    err = capsys.readouterr().err
    assert "managed-writer tails" in err
    assert "without the begin position's phase input" in err


def test_continue_step_passes_the_refusal_through(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.commands import _managed_writer_hop as continue_mod
    from cli.commands import _managed_writer_wiring as wiring

    _ready_guards(monkeypatch)
    mode_mod.decide_managed_writer_mode()
    phase_input = _mode_phase_input()
    seen: list[ManagedWriterPhaseInput | None] = []

    def drive(window_input: ManagedWriterPhaseInput | None) -> int:
        seen.append(window_input)
        return 1

    monkeypatch.setattr(continue_mod, "drive_continuation", drive)

    assert wiring._drive_managed_writer_continuation(phase_input) == 1
    assert seen == [phase_input]
    assert "stage=managed_writer_continue" in capsys.readouterr().out


def test_tails_step_passes_the_refusal_through(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.commands import _managed_writer_hop as continue_mod
    from cli.commands import _managed_writer_wiring as wiring

    _ready_guards(monkeypatch)
    mode_mod.decide_managed_writer_mode()
    phase_input = _mode_phase_input()
    seen: list[ManagedWriterPhaseInput | None] = []

    def tails(window_input: ManagedWriterPhaseInput | None) -> int:
        seen.append(window_input)
        return 1

    monkeypatch.setattr(continue_mod, "commit_tails", tails)

    assert wiring._commit_managed_writer_tails(phase_input) == 1
    assert seen == [phase_input]
    assert "stage=managed_writer_tails" in capsys.readouterr().out
