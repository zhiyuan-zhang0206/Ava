"""`cli.commands._managed_writer_hop` -- the hop plans, fan-out and gate (task #4129, channel C).

Pins the module's import closure too: the eagerly-loaded wiring and verdict
positions import it, so nothing pulling `shared.session_backend` /
`shared.session_record` (the session-kill chain the updater's own import-timing
test guards) may be reachable from here at module scope.

The verdict's hop branch (the phase-input replacement of the Phase-B poll) is
exercised at the bottom: the branch must replace -- never join -- the legacy
poll, wait for the units' candidate-ready journals on a CLEAN gate, and only
then fall into the shared collect/commit window (task #4129 I5).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple
from uuid import UUID

import pytest

from cli.commands._managed_writer_hop import (
    CollectorInput,
    HopUnitPlan,
    ManagedWriterPhaseInput,
    dispatch_bootstrap_hops,
    phase_b_hops,
)
from cli.commands._update_recover import RolloutOutcome
from ops.cluster_rpc import ClusterOpFailed, ClusterOpUnreachable
from ops.rpc_bootstrap_hop import BootstrapHopResult
from ops.rpc_prepare_dispatch import ProjectionFile, prepared_hop_name
from shared.managed_writer_barrier import RolloutIdentity

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The updater's import-timing invariant, re-asserted for this module's own
# closure: `cli.commands` loads on every `ava` invocation, and the wiring /
# verdict imports below put this module in that eager closure.
_MUST_BE_LAZY = ("shared.session_backend", "shared.session_record")

ARTIFACT = "a" * 64
CONTENT = '{"fixture":1}\n'


def _plan(machine: str = "runner-a", home: str = "/ava-a") -> HopUnitPlan:
    name = prepared_hop_name("request", CONTENT)
    return HopUnitPlan(
        machine=machine,
        home=home,
        ops_url=f"http://{machine}",
        artifact_digest=ARTIFACT,
        request_path=f"{home}/run/{name}",
        projections=(ProjectionFile(name=name, content=CONTENT),),
    )


def _ack(plan: HopUnitPlan, **overrides: object) -> dict[str, Any]:
    wire = BootstrapHopResult(
        machine=plan.machine,
        home=plan.home,
        session="ava-updater",
        log=f"{plan.home}/logs/updater-123.log",
    ).model_dump(mode="json")
    return {**wire, **overrides}


def _stub_dispatch(
    monkeypatch: pytest.MonkeyPatch, answers: dict[str, Any]
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def dispatch(
        target_machine: str, kind: str, payload: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        calls.append({"target": target_machine, "kind": kind, "payload": payload, "kwargs": kwargs})
        answer = answers[target_machine]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr("ops.cluster_rpc.dispatch_to_machine", dispatch)
    return calls


# ── the import closure ───────────────────────────────────────────────────────


def test_every_named_module_resolves() -> None:
    """Anti-tautology guard: a rename must fail loudly, not disarm the probe."""
    for name in _MUST_BE_LAZY:
        assert importlib.util.find_spec(name) is not None, (
            f"{name} no longer resolves -- the closure probe has gone vacuous"
        )


def test_the_hop_module_stays_in_the_light_cli_closure() -> None:
    """The wiring and verdict import this module eagerly, so its own closure must
    not reach the session-kill chain (or anything else the eager namespace dodges)."""
    probe = textwrap.dedent(f"""
        import sys

        import cli.commands
        import cli.commands._managed_writer_hop  # noqa: F401

        for name in {list(_MUST_BE_LAZY)!r}:
            if name in sys.modules:
                print(name)
    """)
    proc = subprocess.run(  # noqa: S603 -- fixed argv, sys.executable is trusted
        [sys.executable, "-c", probe],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        env=dict(os.environ),
        check=False,
    )
    assert proc.returncode == 0, f"import probe failed:\n{proc.stdout}\n{proc.stderr}"
    assert proc.stdout == ""


# ── the fan-out ──────────────────────────────────────────────────────────────


def test_dispatch_sends_each_plan_and_acknowledges(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plans = [_plan("runner-a", "/ava-a"), _plan("runner-b", "/ava-b")]
    calls = _stub_dispatch(monkeypatch, {"runner-a": _ack(plans[0]), "runner-b": _ack(plans[1])})

    outcomes = dispatch_bootstrap_hops(plans)

    assert [outcome.ok for outcome in outcomes] == [True, True]
    assert [call["kind"] for call in calls] == ["cluster_bootstrap_hop"] * 2
    assert calls[0]["payload"] == {
        "hop_request": plans[0].request_path,
        "artifact_digest": ARTIFACT,
    }
    assert calls[0]["kwargs"]["ops_url"] == "http://runner-a"
    assert calls[0]["kwargs"]["timeout_s"] is None
    out = capsys.readouterr().out
    assert "runner-a: hop session ava-updater started" in out
    assert "runner-b: hop session ava-updater started" in out


@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        ({"machine": "runner-x"}, "another machine"),
        ({"home": "/ava-x"}, "another unit home"),
        ({"session": "ava-rollout"}, "not the updater session"),
        ({"log": "/elsewhere/logs/updater-1.log"}, "not inside the unit's logs"),
    ],
)
def test_dispatch_refuses_every_ack_drift(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    overrides: dict[str, object],
    detail: str,
) -> None:
    plan = _plan()
    _stub_dispatch(monkeypatch, {"runner-a": _ack(plan, **overrides)})

    outcomes = dispatch_bootstrap_hops([plan])

    assert [outcome.ok for outcome in outcomes] == [False]
    assert detail in outcomes[0].detail
    assert detail in capsys.readouterr().err


def test_dispatch_counts_unreachable_and_failed_ops(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plans = [_plan("runner-a", "/ava-a"), _plan("runner-b", "/ava-b")]
    _stub_dispatch(
        monkeypatch,
        {
            "runner-a": ClusterOpUnreachable("offline"),
            "runner-b": ClusterOpFailed({"error": "refused", "detail": "no image"}),
        },
    )

    outcomes = dispatch_bootstrap_hops(plans)

    assert [outcome.ok for outcome in outcomes] == [False, False]
    err = capsys.readouterr().err
    assert "runner-a: unreachable: offline" in err
    assert "runner-b: op failed:" in err


# ── the gate ─────────────────────────────────────────────────────────────────


def test_gate_opens_on_the_full_acknowledged_roster(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plans = [_plan("runner-a", "/ava-a"), _plan("runner-b", "/ava-b")]
    _stub_dispatch(monkeypatch, {"runner-a": _ack(plans[0]), "runner-b": _ack(plans[1])})

    rc, outcome, hosts, failing = phase_b_hops(plans)

    assert (rc, outcome, hosts, failing) == (0, RolloutOutcome.CLEAN, [], None)
    assert "hop session ava-updater started" in capsys.readouterr().out


def test_gate_refuses_on_any_unacknowledged_unit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plans = [_plan("runner-a", "/ava-a"), _plan("runner-b", "/ava-b")]
    _stub_dispatch(
        monkeypatch,
        {"runner-a": _ack(plans[0]), "runner-b": _ack(plans[1], session="ava-rollout")},
    )

    rc, outcome, hosts, failing = phase_b_hops(plans)

    assert (rc, outcome) == (1, RolloutOutcome.INCOMPLETE)
    assert hosts == []
    assert failing == "the hop gate refused: 1 of 2 units did not acknowledge"
    assert "not the updater session" in capsys.readouterr().err


# ── the verdict branch (`_phase_b_and_commit`) ──────────────────────────────


class _HopStub:
    """Stands in for `phase_b_hops`; records the plans the verdict forwarded."""

    def __init__(
        self, result: tuple[int, RolloutOutcome, list[tuple[str, str | None]], str | None]
    ) -> None:
        self.result = result
        self.calls: list[list[HopUnitPlan]] = []

    def __call__(
        self, plans: tuple[HopUnitPlan, ...]
    ) -> tuple[int, RolloutOutcome, list[tuple[str, str | None]], str | None]:
        self.calls.append(list(plans))
        return self.result


def _seam_targets(runners: list[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
    return list(runners)


def _seam_readiness(
    _targets: list[tuple[str, str | None]], _paused: set[str], _unconverged: list[str] | None
) -> bool:
    return True


def _seam_readiness_refusing(
    _targets: list[tuple[str, str | None]], _paused: set[str], _unconverged: list[str] | None
) -> bool:
    return False


def _never_poll(
    *_args: object, **_kwargs: object
) -> tuple[int, RolloutOutcome, list[tuple[str, str | None]]]:
    raise AssertionError("the legacy Phase-B poll must not run on the hop branch")


def _clean_poll(
    *_args: object, **_kwargs: object
) -> tuple[int, RolloutOutcome, list[tuple[str, str | None]], str | None]:
    return 0, RolloutOutcome.CLEAN, [], None


def _managed_phase_input() -> ManagedWriterPhaseInput:
    """One light phase input for the verdict seams."""
    return ManagedWriterPhaseInput(
        hop_plans=(_plan(),),
        collector=CollectorInput(
            operation=RolloutIdentity(
                holder="gateway:pid1",
                acquired_at=datetime(2026, 9, 20, tzinfo=UTC),
                target_sha="0" * 40,
            ),
            challenge=UUID(int=3),
            valid_until=datetime(2026, 9, 20, 1, tzinfo=UTC),
            candidate_digest="9" * 64,
            units=(),
        ),
    )


class _VerdictRun(NamedTuple):
    verdict: Any
    hop: _HopStub
    phase_input: ManagedWriterPhaseInput | None
    waited: list[CollectorInput]
    collected: list[ManagedWriterPhaseInput | None]
    committed: list[None]


def _verdict_call(
    monkeypatch: pytest.MonkeyPatch,
    *,
    hop_result: tuple[int, RolloutOutcome, list[tuple[str, str | None]], str | None],
    readiness: Any = _seam_readiness,
    wait_result: str | None = None,
    use_phase_input: bool = True,
    poll: Any = _never_poll,
) -> _VerdictRun:
    from cli.commands import _managed_writer_collector as collector_mod
    from cli.commands import _update_verdict as verdict_mod

    stub = _HopStub(hop_result)
    monkeypatch.setattr(verdict_mod, "phase_b_hops", stub)
    container = _managed_phase_input() if use_phase_input else None
    waited: list[CollectorInput] = []

    def wait(collector: CollectorInput) -> str | None:
        waited.append(collector)
        return wait_result

    monkeypatch.setattr(collector_mod, "wait_for_candidate_ready", wait)
    collected: list[ManagedWriterPhaseInput | None] = []

    def collect(window_input: ManagedWriterPhaseInput | None) -> int:
        collected.append(window_input)
        return 0

    committed: list[None] = []

    def commit() -> int:
        committed.append(None)
        return 0

    verdict = verdict_mod._phase_b_and_commit(
        [("host-b", None)],
        paused_names=set[str](),
        unconverged=None,
        target_sha="0" * 40,
        restart_only=False,
        runner_urls={},
        force_reap=False,
        local_launch_failures=list[str](),
        hosts_to_resume=[("host-b", None)],
        targets=_seam_targets,
        readiness=readiness,
        poll_outcome=poll,
        collect=collect,
        commit=commit,
        phase_input=container,
    )
    return _VerdictRun(verdict, stub, container, waited, collected, committed)


def test_verdict_takes_the_hop_branch_and_keeps_the_frozen_resume_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _verdict_call(monkeypatch, hop_result=(0, RolloutOutcome.CLEAN, [], None))

    assert (run.verdict.rc, run.verdict.outcome) == (0, RolloutOutcome.CLEAN)
    assert run.verdict.hosts_to_resume == []
    assert run.verdict.failing_step is None
    assert run.verdict.publication_refused is False
    assert [plan.machine for call in run.hop.calls for plan in call] == ["runner-a"]
    assert run.phase_input is not None
    assert run.waited == [run.phase_input.collector], "a CLEAN gate waits before collecting"
    assert run.collected == [run.phase_input], "the window collects through the phase input"
    assert run.committed == [None], "a collected window commits"


def test_verdict_reports_a_wait_failure_as_a_refused_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The code landed but the activation did not publish: INCOMPLETE + refused."""
    detail = "the hop wait hit the sealed window with units not candidate-ready: runner-a=absent"
    run = _verdict_call(
        monkeypatch, hop_result=(0, RolloutOutcome.CLEAN, [], None), wait_result=detail
    )

    assert (run.verdict.rc, run.verdict.outcome) == (1, RolloutOutcome.INCOMPLETE)
    assert run.verdict.hosts_to_resume == []
    assert run.verdict.failing_step == detail
    assert run.verdict.publication_refused is True
    container = run.phase_input
    assert container is not None
    assert run.waited == [container.collector]
    assert run.collected == [], "a failed wait must not collect"
    assert run.committed == [], "a failed wait must not commit"


def test_verdict_reports_the_hop_gate_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    failing = "the hop gate refused: 1 of 2 units did not acknowledge"
    run = _verdict_call(monkeypatch, hop_result=(1, RolloutOutcome.INCOMPLETE, [], failing))

    assert (run.verdict.rc, run.verdict.outcome) == (1, RolloutOutcome.INCOMPLETE)
    assert run.verdict.hosts_to_resume == []
    assert run.verdict.failing_step == failing
    assert run.verdict.publication_refused is False
    assert run.waited == [], "a refused gate must not wait"
    assert run.collected == [] and run.committed == []


def test_verdict_keeps_the_readiness_gate_before_the_hop_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _verdict_call(
        monkeypatch,
        hop_result=(0, RolloutOutcome.CLEAN, [], None),
        readiness=_seam_readiness_refusing,
    )

    assert (run.verdict.rc, run.verdict.outcome) == (1, RolloutOutcome.INCOMPLETE)
    assert run.verdict.failing_step == "the gateway was not serving, so Phase B never fanned out"
    assert run.hop.calls == [], "the hop phase must not start when the gateway is not serving"
    assert run.waited == [] and run.collected == []


def test_verdict_poll_path_hands_the_collection_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a phase input the legacy poll drives, and the window's collect call
    still happens -- with None, so the wiring's active check can refuse a rollout
    assembled outside its orchestration (fail-closed, never a silent publish)."""
    run = _verdict_call(
        monkeypatch,
        hop_result=(0, RolloutOutcome.CLEAN, [], None),
        use_phase_input=False,
        poll=_clean_poll,
    )

    assert (run.verdict.rc, run.verdict.outcome) == (0, RolloutOutcome.CLEAN)
    assert run.hop.calls == []
    assert run.waited == []
    assert run.collected == [None]
    assert run.committed == [None]
