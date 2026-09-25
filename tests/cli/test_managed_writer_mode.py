"""The managed-writer enable point: one mode decision per rollout (task #4121).

Covers the gate's truth table (off / active / blocked), the fail-closed guard
semantics (absent / not-exactly-True), the read-once cache, the recorded
evidence (rollout log line + telemetry field + `managed_writer_blocked` event),
the read point inside the orchestration (a blocked decision still runs the
legacy flow), the `ava cluster status` bit, the single-read static pin, the
begin / collect+adopt / commit steps the orchestration consumes from the
decision (task #4128 E2-b/E2-c/E2-a), and the completion declaration's
presence in its real module (E2-e).
"""

from __future__ import annotations

import re
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Any

import pytest

from cli.commands import _managed_writer_mode as mode_mod
from cli.commands._managed_writer_hop import (
    CollectorInput,
    HopUnitPlan,
    ManagedWriterPhaseInput,
)
from ops.rpc_prepare_dispatch import ProjectionFile, prepared_hop_name
from shared import rollout_telemetry
from shared.managed_writer_barrier import RolloutIdentity

_CHECKED = "cli.commands._update_normal_release.CHECKED_ACTIVATION_READY"
_WIRING = "cli.commands._update_publication.MANAGED_WRITER_WIRING_COMPLETE"


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    """The decision is cached per process; each test starts from no decision."""
    mode_mod._reset_for_tests()
    rollout_telemetry.deactivate()
    yield
    mode_mod._reset_for_tests()
    rollout_telemetry.deactivate()


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mode_mod, "_config_enabled", lambda: True)


def _disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mode_mod, "_config_enabled", lambda: False)


def _set_guard(monkeypatch: pytest.MonkeyPatch, target: str, value: object = True) -> None:
    monkeypatch.setattr(target, value, raising=False)


def _clear_guard(monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    monkeypatch.delattr(target, raising=False)


def _ready_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    _set_guard(monkeypatch, _CHECKED)
    _set_guard(monkeypatch, _WIRING)


def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    def _capture(**kwargs: Any) -> None:
        events.append(kwargs)

    monkeypatch.setattr("shared.audit_events.insert_event_log", _capture)
    return events


# ── truth table ──────────────────────────────────────────────────────────────


def test_off_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable(monkeypatch)
    assert mode_mod.effective_managed_writer_mode() == mode_mod.ManagedWriterMode("off")


def test_active_when_enabled_and_both_guards_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    _ready_guards(monkeypatch)
    mode = mode_mod.effective_managed_writer_mode()
    assert (mode.state, mode.blocked_reasons) == ("active", ())


def test_checked_activation_absent_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    _clear_guard(monkeypatch, _CHECKED)
    _set_guard(monkeypatch, _WIRING)
    mode = mode_mod.effective_managed_writer_mode()
    assert (mode.state, mode.blocked_reasons) == ("blocked", ("checked_activation_not_ready",))


@pytest.mark.parametrize("value", [False, 1, "True", None])
def test_guard_requires_exactly_true(monkeypatch: pytest.MonkeyPatch, value: object) -> None:
    _enable(monkeypatch)
    _set_guard(monkeypatch, _CHECKED, value)
    _set_guard(monkeypatch, _WIRING)
    assert mode_mod.effective_managed_writer_mode().blocked_reasons == (
        "checked_activation_not_ready",
    )


def test_wiring_incomplete_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    _set_guard(monkeypatch, _CHECKED)
    _clear_guard(monkeypatch, _WIRING)
    mode = mode_mod.effective_managed_writer_mode()
    assert (mode.state, mode.blocked_reasons) == ("blocked", ("wiring_incomplete",))


def test_wiring_declaration_is_present_in_the_real_module() -> None:
    """The final wiring slice (#4128 E2-e) declares the guard in the module it proves."""
    assert (
        mode_mod._guard_ready("cli.commands._update_publication", "MANAGED_WRITER_WIRING_COMPLETE")
        is True
    )


def test_the_real_declarations_activate_the_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """With both declarations carried by their real modules, the switch alone activates."""
    _enable(monkeypatch)
    mode = mode_mod.effective_managed_writer_mode()
    assert (mode.state, mode.blocked_reasons) == ("active", ())


def test_both_guards_missing_join_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    _clear_guard(monkeypatch, _CHECKED)
    _clear_guard(monkeypatch, _WIRING)
    assert mode_mod.effective_managed_writer_mode().blocked_reasons == (
        "checked_activation_not_ready",
        "wiring_incomplete",
    )


def test_guard_reader_treats_import_error_as_not_ready() -> None:
    assert mode_mod._guard_ready("no.such.module_anywhere", "X") is False


# ── read-once semantics ──────────────────────────────────────────────────────


def test_decide_reads_the_switch_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list[bool] = []

    def _counting() -> bool:
        reads.append(True)
        return False

    monkeypatch.setattr(mode_mod, "_config_enabled", _counting)
    first = mode_mod.decide_managed_writer_mode()
    second = mode_mod.decide_managed_writer_mode()
    assert first == second == mode_mod.ManagedWriterMode("off")
    assert len(reads) == 1

    mode_mod._reset_for_tests()
    mode_mod.decide_managed_writer_mode()
    assert len(reads) == 2


def test_accessor_is_none_before_the_read_point(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable(monkeypatch)
    assert mode_mod.managed_writer_mode() is None
    assert mode_mod.decide_managed_writer_mode().state == "off"
    assert mode_mod.managed_writer_mode() == mode_mod.ManagedWriterMode("off")


# ── recorded evidence ────────────────────────────────────────────────────────


def test_blocked_decision_emits_one_event_with_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events = _capture_events(monkeypatch)
    _enable(monkeypatch)
    _set_guard(monkeypatch, _CHECKED)
    _clear_guard(monkeypatch, _WIRING)
    mode_mod.decide_managed_writer_mode()
    mode_mod.decide_managed_writer_mode()  # re-entry must not emit a second event
    assert events == [
        {
            "event_type": "managed_writer_blocked",
            "agent_id": None,
            "source": "system",
            "payload": {"reason": "wiring_incomplete"},
        }
    ]
    out = capsys.readouterr().out
    assert "managed-writer mode: blocked \u2014 wiring incomplete; running the legacy flow" in out


def test_off_decision_records_no_event(monkeypatch: pytest.MonkeyPatch) -> None:
    events = _capture_events(monkeypatch)
    _disable(monkeypatch)
    assert mode_mod.decide_managed_writer_mode().state == "off"
    assert events == []


def test_active_decision_records_no_event(monkeypatch: pytest.MonkeyPatch) -> None:
    events = _capture_events(monkeypatch)
    _ready_guards(monkeypatch)
    assert mode_mod.decide_managed_writer_mode().state == "active"
    assert events == []


def test_blocked_decision_lands_in_the_rollout_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    _set_guard(monkeypatch, _CHECKED)
    _clear_guard(monkeypatch, _WIRING)
    collector = rollout_telemetry.activate()
    mode_mod.decide_managed_writer_mode()
    assert collector.summary()["managed_writer"] == {
        "state": "blocked",
        "reasons": ["wiring_incomplete"],
    }


def test_off_decision_lands_in_the_rollout_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable(monkeypatch)
    collector = rollout_telemetry.activate()
    mode_mod.decide_managed_writer_mode()
    assert collector.summary()["managed_writer"] == {"state": "off", "reasons": []}


def test_no_collector_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable(monkeypatch)
    mode_mod.decide_managed_writer_mode()  # must not raise without a collector


def test_blocked_event_is_a_registered_audit_name() -> None:
    from shared.events.contract import EVENTS

    assert EVENTS["managed_writer_blocked"].category == "audit"


# ── the read point inside the orchestration ──────────────────────────────────


def _stub_orchestration(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Reach the end of a real (non-dry-run) rollout without a live cluster."""
    from cli import commands as _cli
    from cli.commands import update as _up

    stopped: list[str] = []
    monkeypatch.setattr(_up, "_rollout_preflight", lambda _repo, **_kw: (None, False, "target-sha"))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "_begin_update_record", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_resolve_fanout_targets", lambda **_kw: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "_run_preflight_fetch", lambda *_a, **_k: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "dry_run_checks", lambda *_a, **_k: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "estimate_maintenance_window", lambda: 130.0)
    monkeypatch.setattr(_up, "_snapshot_known_good", lambda **_kw: ("old", set[str](), None))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _up,
        "_stop_the_world",
        lambda *_a, **_k: stopped.append("stop") or (set[str](), True),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_cli, "_run_gateway_local_update", lambda *_a, **_k: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "refresh_data_plane_settings", lambda: None)
    monkeypatch.setattr(_up, "_persist_cluster_pin", lambda _sha, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_finalize._unpause_local_via_tree", lambda _repo: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "finalize_rollout", lambda *_a, **kwargs: kwargs["outcome"])  # pyright: ignore[reportUnknownArgumentType]
    return stopped


def _run_inner(*, restart_only: bool = False) -> int:
    from cli.commands import update as _up

    return _up._run_gateway_orchestration_inner(
        Path("/unused"),
        restart_only=restart_only,
        origin="test-origin",
        deploy_capability={
            "deploy_holder": "test",
            "deploy_acquired_at": "2026-08-25T00:00:00+00:00",
        },
    )


def test_blocked_still_runs_the_legacy_flow(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Switch on + guard missing -> the legacy flow runs unchanged, blocked marked."""
    events = _capture_events(monkeypatch)
    _enable(monkeypatch)
    _clear_guard(monkeypatch, _CHECKED)
    _clear_guard(monkeypatch, _WIRING)
    stopped = _stub_orchestration(monkeypatch)

    assert _run_inner() == 0
    assert stopped == ["stop"]
    decided = mode_mod.managed_writer_mode()
    assert decided is not None and decided.state == "blocked"
    assert [event["event_type"] for event in events] == ["managed_writer_blocked"]
    assert "managed-writer mode: blocked" in capsys.readouterr().out


def test_decision_precedes_prepare(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable(monkeypatch)
    _stub_orchestration(monkeypatch)
    from cli.commands import update as _up

    original = _up._build_prepare_gate
    observed: list[object] = []

    def _probe(*args: Any, **kwargs: Any) -> Any:
        observed.append(mode_mod.managed_writer_mode())
        return original(*args, **kwargs)  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(_up, "_build_prepare_gate", _probe)
    assert _run_inner() == 0
    assert observed == [mode_mod.ManagedWriterMode("off")]


def test_active_rollout_runs_the_flow_and_keeps_the_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.commands import update as _up

    _ready_guards(monkeypatch)
    stopped = _stub_orchestration(monkeypatch)
    # This test is about the decision's lifetime; the begin position is stubbed
    # (its chain needs a sealed release context and a live lease, and it has its
    # own tests below).
    monkeypatch.setattr(_up, "_begin_managed_writer_publication", lambda _sha: (0, None))  # pyright: ignore[reportUnknownArgumentType]
    assert _run_inner() == 0
    assert stopped == ["stop"]
    assert mode_mod.managed_writer_mode() == mode_mod.ManagedWriterMode("active")


def test_off_rollout_records_no_event_and_no_banner(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events = _capture_events(monkeypatch)
    _disable(monkeypatch)
    stopped = _stub_orchestration(monkeypatch)
    assert _run_inner() == 0
    assert stopped == ["stop"]
    assert events == []
    assert "managed-writer mode: off" in capsys.readouterr().out


# ── the P1 begin wiring (task #4128, E2-b) ───────────────────────────────────


@pytest.mark.parametrize("state", ["off", "blocked", "none"])
def test_begin_step_skips_without_an_active_decision(
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

    assert wiring._begin_managed_writer_publication("0" * 40) == (0, None)
    captured = capsys.readouterr()
    assert "managed-writer begin" not in captured.out
    if state == "none":
        # A call outside the rollout's read point is beaconed, never silent.
        assert "no mode decision recorded" in captured.err
    else:
        assert captured.err == ""


def test_begin_step_refuses_under_active_when_the_context_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    set_machine_identity,
) -> None:
    """The wiring translates the chain's refusal into the step's exit code and a
    named stderr line; the connected chain refuses with no sealed context yet.

    The machine identity is injected at the source: the lease's holder is this
    process's own `self_holder()`, and resolving that name for real would walk
    into the bare tmp home this test points `ava_home` at. It previously only
    passed because another test had already primed the process-global identity
    cache in the same xdist worker -- a worker-placement flake that turned red
    on the Trunk runner (2026-09-20)."""
    from datetime import UTC, datetime

    from cli.commands import _managed_writer_collector as collector_mod
    from cli.commands import _managed_writer_dispatch as dispatch_mod
    from cli.commands import _managed_writer_wiring as wiring
    from shared.cluster_lock import DeployLease

    set_machine_identity(role="agent-runner", name="test-runner")
    _ready_guards(monkeypatch)
    mode_mod.decide_managed_writer_mode()
    monkeypatch.setattr(
        dispatch_mod,
        "read_update_lease",
        lambda: DeployLease(
            holder=dispatch_mod.self_holder(),
            held_for_s=0,
            expires_in_s=60,
            kind="rollout",
            acquired_at=datetime(2026, 9, 20, tzinfo=UTC),
        ),
    )
    monkeypatch.setattr(dispatch_mod.settings.general, "ava_home", tmp_path.resolve())

    # The begin's N3 registration read (task #4129 I5) is a database touch; the
    # test's subject is the missing sealed context, so the read stands in empty.
    def read_registration(_operation: object) -> collector_mod.JournaledRegistration:
        return collector_mod.JournaledRegistration(valid_until=None, plan_digest=None)

    monkeypatch.setattr(collector_mod, "read_journaled_registration", read_registration)

    assert wiring._begin_managed_writer_publication("0" * 40) == (1, None)
    err = capsys.readouterr().err
    assert "managed-writer begin refused" in err
    assert "no sealed release context" in err


def test_active_rollout_refuses_at_the_begin_position(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-closed: an `active` rollout must not reach the stop with no journal."""
    from cli.commands import _managed_writer_dispatch as dispatch_mod
    from cli.commands import update as _up

    _ready_guards(monkeypatch)
    stopped = _stub_orchestration(monkeypatch)
    # No live rollout lease: the begin chain refuses before it stops anything.
    monkeypatch.setattr(dispatch_mod, "read_update_lease", lambda: None)
    recorded: dict[str, object] = {}

    def _capture(_hosts: object, _fan_out: object, _timeout: object, **kwargs: object) -> None:
        recorded.update(kwargs)

    monkeypatch.setattr(_up, "finalize_rollout", _capture)

    assert _run_inner() == 1
    assert stopped == [], "nothing may be stopped without the journal open"
    assert recorded["outcome"] is _up.RolloutOutcome.ABORTED
    assert "managed-writer begin refused" in str(recorded["failing_step"])
    assert "nothing was stopped" in str(recorded["failing_step"])


def test_restart_only_rollout_skips_the_begin_position(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bounce publishes nothing: restart-only rollouts never enter the begin position by design."""
    _ready_guards(monkeypatch)
    stopped = _stub_orchestration(monkeypatch)

    assert _run_inner(restart_only=True) == 0
    assert stopped == ["stop"]


def _hop_plan() -> HopUnitPlan:
    content = '{"fixture":1}\n'
    name = prepared_hop_name("request", content)
    return HopUnitPlan(
        machine="host-b",
        home="/ava-b",
        ops_url=None,
        artifact_digest="a" * 64,
        request_path=f"/ava-b/run/{name}",
        projections=(ProjectionFile(name=name, content=content),),
    )


def _phase_input() -> ManagedWriterPhaseInput:
    """One light phase input: the fixture plan plus an empty collector roster."""
    from datetime import UTC, datetime
    from uuid import UUID

    return ManagedWriterPhaseInput(
        hop_plans=(_hop_plan(),),
        collector=CollectorInput(
            operation=RolloutIdentity(
                holder="gateway:pid77",
                acquired_at=datetime(2026, 9, 20, tzinfo=UTC),
                target_sha="0" * 40,
            ),
            challenge=UUID(int=5),
            valid_until=datetime(2026, 9, 20, 1, tzinfo=UTC),
            candidate_digest="e" * 64,
            units=(),
        ),
        continue_units=(),
    )


def test_begin_step_returns_the_phase_input_under_active(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands import _managed_writer_dispatch as dispatch_mod
    from cli.commands import _managed_writer_wiring as wiring

    _ready_guards(monkeypatch)
    mode_mod.decide_managed_writer_mode()
    phase_input = _phase_input()
    monkeypatch.setattr(dispatch_mod, "begin_managed_writer_publication", lambda _sha: phase_input)  # pyright: ignore[reportUnknownArgumentType]

    assert wiring._begin_managed_writer_publication("0" * 40) == (0, phase_input)


# ── the P2 collect wiring (task #4128, E2-c) ─────────────────────────────────


@pytest.mark.parametrize("state", ["off", "blocked", "none"])
def test_collect_step_skips_without_an_active_decision(
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

    assert wiring._collect_managed_writer_publication(None) == 0
    captured = capsys.readouterr()
    assert "managed-writer collect" not in captured.out
    if state == "none":
        # A call outside the rollout's read point is beaconed, never silent.
        assert "no mode decision recorded" in captured.err
    else:
        assert captured.err == ""


def test_collect_step_refuses_without_the_phase_input_under_active(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An `active` decision with no phase input is an invariant breach, refused
    fail-closed: only the begin position's chain may feed this step."""
    from cli.commands import _managed_writer_wiring as wiring

    _ready_guards(monkeypatch)
    mode_mod.decide_managed_writer_mode()

    assert wiring._collect_managed_writer_publication(None) == 1
    err = capsys.readouterr().err
    assert "managed-writer collect refused" in err
    assert "without the begin position's phase input" in err
    assert "assembled outside its orchestration" in err


def test_active_rollout_refuses_at_the_collect_position(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-closed: a completed rollout must not publish an uncollected set."""
    from cli.commands import update as _up

    _ready_guards(monkeypatch)
    _stub_orchestration_to_phase_b(monkeypatch, stub_collect=False)
    commit_calls: list[None] = []
    monkeypatch.setattr(
        _up, "_commit_managed_writer_publication", lambda: commit_calls.append(None) or 0
    )  # pyright: ignore[reportUnknownArgumentType]
    recorded: dict[str, object] = {}

    def _capture(_hosts: object, _fan_out: object, _timeout: object, **kwargs: object) -> None:
        recorded.update(kwargs)

    monkeypatch.setattr(_up, "finalize_rollout", _capture)

    assert _run_inner() == 1
    assert commit_calls == [], "the commit must not run when the collection refused"
    assert recorded["outcome"] is _up.RolloutOutcome.INCOMPLETE
    assert "the managed-writer collection refused" in str(recorded["failing_step"])
    assert recorded["publication_refused"] is True


def test_restart_only_rollout_skips_the_collect_position(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bounce publishes nothing: restart-only rollouts never enter the collect position by design."""
    from cli.commands import update as _up

    _ready_guards(monkeypatch)
    _stub_orchestration_to_phase_b(monkeypatch)
    calls: list[None] = []
    monkeypatch.setattr(
        _up,
        "_collect_managed_writer_publication",
        lambda _phase_input: calls.append(None) or 0,  # pyright: ignore[reportUnknownArgumentType]
    )
    commit_calls: list[None] = []
    monkeypatch.setattr(
        _up, "_commit_managed_writer_publication", lambda: commit_calls.append(None) or 0
    )  # pyright: ignore[reportUnknownArgumentType]

    assert _run_inner(restart_only=True) == 0
    assert calls == [] and commit_calls == []


def test_collect_never_runs_on_a_non_clean_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands import update as _up

    _ready_guards(monkeypatch)
    _stub_orchestration_to_phase_b(monkeypatch)
    monkeypatch.setattr(
        _up,
        "_phase_b_outcome",
        lambda *_a, **_k: (1, _up.RolloutOutcome.INCOMPLETE, [], None),  # pyright: ignore[reportUnknownArgumentType]
    )
    calls: list[None] = []
    monkeypatch.setattr(
        _up,
        "_collect_managed_writer_publication",
        lambda _phase_input: calls.append(None) or 0,  # pyright: ignore[reportUnknownArgumentType]
    )
    commit_calls: list[None] = []
    monkeypatch.setattr(
        _up, "_commit_managed_writer_publication", lambda: commit_calls.append(None) or 0
    )  # pyright: ignore[reportUnknownArgumentType]

    assert _run_inner() == 1
    assert calls == [] and commit_calls == []


# ── the P5 commit wiring (task #4128, E2-a) ──────────────────────────────────


def _fake_write_transaction(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Stand in for the commit step's transaction; no database is dialed."""
    import contextlib

    borrowed: list[object] = []

    @contextlib.contextmanager
    def _txn() -> Generator[object, None, None]:
        borrowed.append(object())
        yield borrowed[-1]

    monkeypatch.setattr("shared.db_transaction.write_transaction", _txn)
    return borrowed


@pytest.mark.parametrize("state", ["off", "blocked", "none"])
def test_commit_step_skips_without_an_active_decision(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], state: str
) -> None:
    from cli.commands import _managed_writer_wiring as wiring
    from cli.commands import _update_publication as seats

    _capture_events(monkeypatch)
    calls: list[object] = []
    monkeypatch.setattr(seats, "commit_pending_publication", calls.append)
    borrowed = _fake_write_transaction(monkeypatch)
    if state == "off":
        _disable(monkeypatch)
        mode_mod.decide_managed_writer_mode()
    elif state == "blocked":
        _enable(monkeypatch)
        _clear_guard(monkeypatch, _CHECKED)
        _clear_guard(monkeypatch, _WIRING)
        mode_mod.decide_managed_writer_mode()

    assert wiring._commit_managed_writer_publication() == 0
    assert calls == []
    assert borrowed == []
    captured = capsys.readouterr()
    assert "managed_writer_commit" not in captured.out
    if state == "none":
        # A call outside the rollout's read point is beaconed, never silent.
        assert "no mode decision recorded" in captured.err
    else:
        assert captured.err == ""


def test_commit_step_publishes_on_an_active_decision(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from uuid import uuid4

    from cli.commands import _managed_writer_wiring as wiring
    from cli.commands import _update_publication as seats

    publication_id = uuid4()
    seen: list[object] = []

    def _commit(conn: object) -> object:
        seen.append(conn)
        return publication_id

    monkeypatch.setattr(seats, "commit_pending_publication", _commit)
    borrowed = _fake_write_transaction(monkeypatch)
    _ready_guards(monkeypatch)
    mode_mod.decide_managed_writer_mode()

    assert wiring._commit_managed_writer_publication() == 0
    assert seen == borrowed and len(borrowed) == 1
    out = capsys.readouterr().out
    assert str(publication_id) in out
    assert "stage=managed_writer_commit" in out


def test_commit_step_is_a_clean_skip_without_a_pending(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.commands import _managed_writer_wiring as wiring
    from cli.commands import _update_publication as seats

    monkeypatch.setattr(seats, "commit_pending_publication", lambda _conn: None)  # pyright: ignore[reportUnknownArgumentType]
    _fake_write_transaction(monkeypatch)
    _ready_guards(monkeypatch)
    mode_mod.decide_managed_writer_mode()

    assert wiring._commit_managed_writer_publication() == 0
    assert "committed" not in capsys.readouterr().out


def test_commit_step_refusal_names_checked_recovery(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.commands import _managed_writer_wiring as wiring
    from cli.commands import _update_publication as seats
    from shared.managed_writer_barrier import ManagedWriterBarrierError

    def _refuse(conn: object) -> object:
        del conn
        raise ManagedWriterBarrierError("pending publication is not ready to commit")

    monkeypatch.setattr(seats, "commit_pending_publication", _refuse)
    _fake_write_transaction(monkeypatch)
    _ready_guards(monkeypatch)
    mode_mod.decide_managed_writer_mode()

    assert wiring._commit_managed_writer_publication() == 1
    err = capsys.readouterr().err
    assert "managed-writer commit refused" in err
    assert "ava cluster recover-pending" in err


def _stub_orchestration_to_phase_b(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stub_collect: bool = True,
    phase_input: ManagedWriterPhaseInput | None = None,
) -> None:
    """Extend `_stub_orchestration` to reach the post-Phase-B publication window."""
    from cli import commands as _cli
    from cli.commands import update as _up

    _stub_orchestration(monkeypatch)
    # The publication window lies downstream of the E2-b begin (whose chain
    # needs a sealed release context and a live lease) and the E2-c collect
    # (whose channel is the collector), so these tests stub both -- the
    # positions under test are elsewhere. `stub_collect=False` leaves the real
    # collect step in place for its own refusal test, and `phase_input` makes
    # the stubbed begin hand the closing section a managed-writer phase input
    # (task #4129 I4/I5).
    monkeypatch.setattr(_up, "_begin_managed_writer_publication", lambda _sha: (0, phase_input))  # pyright: ignore[reportUnknownArgumentType]
    if stub_collect:
        monkeypatch.setattr(_up, "_collect_managed_writer_publication", lambda _phase_input: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_resolve_fanout_targets", lambda **_kw: [("host-b", None)])  # pyright: ignore[reportUnknownArgumentType]
    # The stale-marker reconcile dials the live roster; only the commit step
    # under test may borrow the fake transaction.
    monkeypatch.setattr(_up, "_clear_stale_stop_marker", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "_phase_b_targets", list)
    monkeypatch.setattr(_up, "_gateway_ready_or_incomplete", lambda *_a, **_k: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _up,
        "_phase_b_outcome",
        lambda *_a, **_k: (0, _up.RolloutOutcome.CLEAN, [], None),  # pyright: ignore[reportUnknownArgumentType]
    )


def _stub_commit_landing(monkeypatch: pytest.MonkeyPatch) -> tuple[list[object], list[object]]:
    """Point the real commit step's seat at a recorded landing, not a database."""
    from cli.commands import _update_publication as seats

    committed: list[object] = []

    def _commit(conn: object) -> object:
        committed.append(conn)
        return None

    monkeypatch.setattr(seats, "commit_pending_publication", _commit)
    borrowed = _fake_write_transaction(monkeypatch)
    return borrowed, committed


def test_active_rollout_commits_through_the_seat(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real step + seat landing: one transaction, one commit, one stage."""
    _ready_guards(monkeypatch)
    _stub_orchestration_to_phase_b(monkeypatch)
    borrowed, committed = _stub_commit_landing(monkeypatch)

    assert _run_inner() == 0
    assert committed == borrowed and len(committed) == 1
    assert "stage=managed_writer_commit" in capsys.readouterr().out


@pytest.mark.parametrize("state", ["off", "blocked"])
def test_inactive_rollout_leaves_the_commit_landing_untouched(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], state: str
) -> None:
    """A rollout outside the active decision never borrows or commits."""
    _capture_events(monkeypatch)
    if state == "off":
        _disable(monkeypatch)
    else:
        _enable(monkeypatch)
        _clear_guard(monkeypatch, _CHECKED)
        _clear_guard(monkeypatch, _WIRING)
    _stub_orchestration_to_phase_b(monkeypatch)
    borrowed, committed = _stub_commit_landing(monkeypatch)

    assert _run_inner() == 0
    assert committed == [] and borrowed == []
    assert "managed_writer_commit" not in capsys.readouterr().out


def test_commit_step_never_runs_on_a_non_clean_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands import update as _up

    _ready_guards(monkeypatch)
    _stub_orchestration_to_phase_b(monkeypatch)
    monkeypatch.setattr(
        _up,
        "_phase_b_outcome",
        lambda *_a, **_k: (1, _up.RolloutOutcome.INCOMPLETE, [], None),  # pyright: ignore[reportUnknownArgumentType]
    )
    calls: list[None] = []
    monkeypatch.setattr(_up, "_commit_managed_writer_publication", lambda: calls.append(None) or 0)  # pyright: ignore[reportUnknownArgumentType]

    assert _run_inner() == 1
    assert calls == []


def test_commit_refusal_fails_the_rollout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal must reach the record face as INCOMPLETE + the retained-journal
    flag: an rc-only assertion would let the record still claim CLEAN."""
    from cli.commands import update as _up

    _ready_guards(monkeypatch)
    _stub_orchestration_to_phase_b(monkeypatch)
    monkeypatch.setattr(_up, "_commit_managed_writer_publication", lambda: 1)  # pyright: ignore[reportUnknownArgumentType]
    recorded: dict[str, object] = {}

    def _capture(_hosts: object, _fan_out: object, _timeout: object, **kwargs: object) -> None:
        recorded.update(kwargs)

    monkeypatch.setattr(_up, "finalize_rollout", _capture)

    assert _run_inner() == 1
    assert recorded["outcome"] is _up.RolloutOutcome.INCOMPLETE
    assert "managed-writer publication commit refused" in str(recorded["failing_step"])
    assert recorded["publication_refused"] is True


def test_restart_only_rollout_skips_the_commit_step(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands import update as _up

    _ready_guards(monkeypatch)
    _stub_orchestration_to_phase_b(monkeypatch)
    calls: list[None] = []
    monkeypatch.setattr(_up, "_commit_managed_writer_publication", lambda: calls.append(None) or 0)  # pyright: ignore[reportUnknownArgumentType]

    assert _run_inner(restart_only=True) == 0
    assert calls == []


# ── the status bit ───────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload: dict[str, Any] | list[Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError("fake error response")

    def json(self) -> dict[str, Any] | list[Any]:
        return self._payload


def _patch_roster(monkeypatch: pytest.MonkeyPatch, roster: list[dict[str, Any]]) -> None:
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")

    def _fake_get(url: str, **_kw: Any) -> _FakeResponse:
        return _FakeResponse(roster)

    monkeypatch.setattr("httpx.get", _fake_get)


def _machine_row() -> dict[str, Any]:
    from datetime import UTC, datetime

    from gateway.schemas import MachineStatus

    base = MachineStatus(
        name="test-host",
        serve_gateway=True,
        serve_agent_runner=True,
        gateway_url="http://gw:8000",
        up_since_at=datetime(2026, 6, 1, 7, 0, tzinfo=UTC),
        online=True,
        paused=False,
    )
    return base.model_dump(mode="json")  # pyright: ignore[reportUnknownMemberType]


def test_status_off_is_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli import commands as _cli

    _disable(monkeypatch)
    _patch_roster(monkeypatch, [_machine_row()])
    assert _cli.cmd_cluster_status() == 0
    assert "managed-writer" not in capsys.readouterr().out


def test_status_shows_effective_blocked_not_just_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Switch on + guard missing renders blocked -- never a plain 'on'."""
    from cli import commands as _cli

    _enable(monkeypatch)
    _clear_guard(monkeypatch, _CHECKED)
    _set_guard(monkeypatch, _WIRING)
    _patch_roster(monkeypatch, [_machine_row()])
    assert _cli.cmd_cluster_status() == 0
    out = capsys.readouterr().out
    assert (
        "managed-writer: blocked \u2014 checked activation not ready; running the legacy flow"
        in out
    )


def test_status_shows_active(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli import commands as _cli

    _ready_guards(monkeypatch)
    _patch_roster(monkeypatch, [_machine_row()])
    assert _cli.cmd_cluster_status() == 0
    assert "managed-writer: active" in capsys.readouterr().out


# ── the single-read static pin ───────────────────────────────────────────────


_SOURCE_ROOTS = ("cli", "shared", "agent", "gateway", "ops", "ava", "ava_builtins", "services")
_CANONICAL_READERS = {
    "cli/commands/_managed_writer_mode.py",
    "shared/config/gateway.py",
}
# The word boundary pins the switch itself: the activation-window field
# (`update_managed_writer_window_seconds`, task #4129) shares the prefix but is
# a different knob with its own consumers, and a bare substring match would
# read its every mention as a switch read.
_SWITCH_READ = re.compile(r"\bupdate_managed_writer\b")


def test_switch_is_read_only_in_the_gate_module() -> None:
    """Exactly one production read site; any other reader is a read-point bypass."""
    assert _SWITCH_READ.search("settings.gateway.update_managed_writer") is not None
    assert _SWITCH_READ.search("gateway.update_managed_writer_window_seconds") is None
    root = Path(__file__).resolve().parents[2]
    offenders: dict[str, list[int]] = {}
    for source_root in _SOURCE_ROOTS:
        for path in sorted((root / source_root).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if rel in _CANONICAL_READERS:
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                if _SWITCH_READ.search(line):
                    offenders.setdefault(rel, []).append(lineno)
    assert offenders == {}, (
        f"update_managed_writer is read outside the enable point: {offenders} -- "
        "consumers take the recorded decision, never the switch."
    )
