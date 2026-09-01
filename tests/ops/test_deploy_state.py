"""Shared deploy-state read layer — the one copy of the lease/orchestration
classification the pin/code/schema heal controllers used to inline three times.

Locks the classification contract every controller now builds on: the four lease
kinds, the narrow/pass settle-hold modes, and the conservative unreadable-defer
directions. The per-controller guard ORDER is locked by their own suites (the
settle+* cases); this file locks the reading layer itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ops import cluster_session
from ops.controllers._deploy_state import (
    Defer,
    Guard,
    GuardCtx,
    GuardVerdict,
    Proceed,
    ProceedWarn,
    read_lease_state,
    read_orchestration,
    run_guards,
)
from shared.cluster_lock import DeployLease, settle_note
from shared.host_deploy_state import HostDeployState

_THIS_HOST = "laptop-host"


def _lease(*, note: str | None) -> DeployLease:
    """A live lease as `read_update_lease` returns it. `note=None` is a rollout
    executing right now; a settle note is a stated waiting period with nobody
    executing under it."""
    return DeployLease(
        holder="gateway-host:pid65237", held_for_s=120.0, expires_in_s=900.0, note=note
    )


def _host_state(*, live: bool) -> HostDeployState:
    now = datetime.now(UTC)
    return HostDeployState(
        machine=_THIS_HOST,
        posture="converging",
        updated_at=now,
        updater_lease_expires_at=now + timedelta(minutes=5) if live else None,
    )


@pytest.fixture(autouse=True)
def _machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The narrow mode asks "does this hold name THIS host" — a stable name makes
    the settle cases deterministic."""
    monkeypatch.setattr("shared.machine.machine_name", lambda: _THIS_HOST)


class TestReadLeaseState:
    """The four lease kinds and both settle-hold modes."""

    def test_free_when_no_lease(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: None)
        verdict = read_lease_state(settle_hold_mode="narrow")
        assert verdict.kind == "free"
        assert verdict.holder is None and verdict.waits_for_this_host is None

    def test_unreadable_lease_defers_in_both_modes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A lease that cannot be read is not evidence of absence — every mode
        classifies it unreadable, and every controller defers on that."""

        def _boom() -> DeployLease | None:
            raise RuntimeError("db down")

        monkeypatch.setattr("shared.cluster_lock.read_update_lease", _boom)
        for mode in ("narrow", "pass"):
            verdict = read_lease_state(settle_hold_mode=mode)  # type: ignore[arg-type]
            assert verdict.kind == "unreadable"

    def test_executing_lease_in_both_modes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An executing rollout defers in every mode — a settle hold is the ONLY
        kind of lease a healer may pass."""
        monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: _lease(note=None))
        for mode in ("narrow", "pass"):
            verdict = read_lease_state(settle_hold_mode=mode)  # type: ignore[arg-type]
            assert verdict.kind == "executing"
            assert verdict.holder == "gateway-host:pid65237"

    def test_settle_hold_naming_this_host_passes_narrow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #1020's exception, in reading terms: a hold that exists BECAUSE
        this host has not converged is passed by the narrow mode."""
        monkeypatch.setattr(
            "shared.cluster_lock.read_update_lease",
            lambda: _lease(note=settle_note([_THIS_HOST, "other-box"])),
        )
        verdict = read_lease_state(settle_hold_mode="narrow")
        assert verdict.kind == "settle_hold"
        assert verdict.waits_for_this_host is True
        assert verdict.describe is not None and "settling, waiting for" in verdict.describe

    def test_settle_hold_naming_another_host_defers_narrow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The permission is scoped to the named host; a hold waiting for someone
        else says nothing about this host's right to heal itself."""
        monkeypatch.setattr(
            "shared.cluster_lock.read_update_lease", lambda: _lease(note=settle_note(["other-box"]))
        )
        verdict = read_lease_state(settle_hold_mode="narrow")
        assert verdict.kind == "settle_hold"
        assert verdict.waits_for_this_host is False

    def test_settle_hold_unparseable_note_defers_narrow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unreadable note yields an empty host set and therefore a deferral —
        a note we cannot parse must never be read as permission."""
        monkeypatch.setattr(
            "shared.cluster_lock.read_update_lease", lambda: _lease(note="paused for maintenance")
        )
        verdict = read_lease_state(settle_hold_mode="narrow")
        assert verdict.kind == "settle_hold"
        assert verdict.waits_for_this_host is False

    def test_settle_hold_passes_in_pass_mode_even_for_another_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The schema controller's reading: its heal answers a cluster-shared
        condition, so no settle hold defers it — narrowing to ``awaits`` would
        import the mutual wait #1020 exists to remove."""
        monkeypatch.setattr(
            "shared.cluster_lock.read_update_lease", lambda: _lease(note=settle_note(["other-box"]))
        )
        verdict = read_lease_state(settle_hold_mode="pass")
        assert verdict.kind == "settle_hold"
        assert verdict.waits_for_this_host is None  # the question is not asked


class TestReadOrchestration:
    """The three orchestration readings; unreadable is conservative everywhere."""

    def test_none_when_no_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ops.cluster.current_orchestration", lambda: None)
        state = read_orchestration()
        assert state.kind == "none" and state.name is None

    def test_in_flight_names_the_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ops.cluster.current_orchestration", lambda: "update")
        state = read_orchestration()
        assert state.kind == "in_flight" and state.name == "update"

    def test_unreadable_defers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The pin controller's safer semantics, now uniform: an orchestration
        read that fails is not evidence of absence. (The code controller used to
        let this exception propagate — the drift this module closes.)"""

        def _boom() -> str | None:
            raise RuntimeError("session spawn not available")

        monkeypatch.setattr("ops.cluster.current_orchestration", _boom)
        state = read_orchestration()
        assert state.kind == "unreadable" and state.name is None


class TestCurrentOrchestrationPreRead:
    """A status snapshot can reuse its two DB rows without changing the judgment."""

    def test_executing_lease_answers_before_host_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _unexpected_read(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("current_orchestration performed another DB read")

        monkeypatch.setattr("shared.cluster_lock.read_update_lease", _unexpected_read)
        monkeypatch.setattr("shared.host_deploy_state.read", _unexpected_read)
        lease = DeployLease(
            holder="gateway:pid1",
            held_for_s=10.0,
            expires_in_s=100.0,
            note=None,
            kind="rollout",
        )

        assert cluster_session.current_orchestration(_host_state(live=True), lease) == "rollout"

    def test_host_updater_is_the_fallback_when_the_lease_does_not_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _unexpected_read(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("current_orchestration performed another DB read")

        monkeypatch.setattr("shared.cluster_lock.read_update_lease", _unexpected_read)
        monkeypatch.setattr("shared.host_deploy_state.read", _unexpected_read)

        assert cluster_session.current_orchestration(_host_state(live=True), None) == "update"
        assert cluster_session.current_orchestration(_host_state(live=False), None) is None
        assert cluster_session.current_orchestration(None, None) is None


class TestGuardPipeline:
    """run_guards: short-circuit on the first Defer, warn-and-continue on
    ProceedWarn, Proceed only when every guard passed."""

    @staticmethod
    def _verdict(kind: str) -> Guard:
        def _v(_ctx: GuardCtx) -> GuardVerdict:
            return {"defer": Defer, "warn": ProceedWarn, "proceed": Proceed}[kind]  # type: ignore[return-value]

        return Guard(kind, _v)

    def test_all_proceed_returns_proceed(self) -> None:
        guards = [self._verdict("proceed"), self._verdict("proceed")]
        assert run_guards(guards, GuardCtx("a", "b")) is Proceed

    def test_first_defer_short_circuits(self) -> None:
        """The pipeline stops at the first Defer — later guards are not consulted
        (their side effects are how the settle+* order tests observe this)."""
        seen: list[str] = []

        def _defer(_ctx: GuardCtx) -> GuardVerdict:
            seen.append("defer")
            return Defer

        def _late(_ctx: GuardCtx) -> GuardVerdict:
            seen.append("late")
            return Defer

        guards = [Guard("defer", _defer), Guard("late", _late)]
        assert run_guards(guards, GuardCtx("a", "b")) is Defer
        assert seen == ["defer"]

    def test_proceed_warn_continues(self) -> None:
        """A settle hold naming this host passes the lease guard but the rest of
        the table still applies — the #1020 exception is scoped to the lock half."""
        seen: list[str] = []

        def _warn(_ctx: GuardCtx) -> GuardVerdict:
            seen.append("warn")
            return ProceedWarn

        def _defer(_ctx: GuardCtx) -> GuardVerdict:
            seen.append("defer")
            return Defer

        guards = [Guard("warn", _warn), Guard("defer", _defer)]
        assert run_guards(guards, GuardCtx("a", "b")) is Defer
        assert seen == ["warn", "defer"]
