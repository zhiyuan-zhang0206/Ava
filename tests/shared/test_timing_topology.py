"""Verify all remaining hosted-runtime and maintenance timing relationships.

Tests assert defaults, exercise each constraint kind with invalid live values,
and ensure the family modules register every clock they expose.
"""

from __future__ import annotations

import pytest

import shared.deploy_timing as deploy
from shared.timing import CLOCKS, CONSTRAINTS, assert_clock_lattice, validate_clock_lattice


def test_default_lattice_holds() -> None:
    """The full declared lattice must hold for the settings defaults.

    This is the topology pin: every constraint in `shared.timing.CONSTRAINTS`
    (deploy / schedule-supervision / agent-lease / updater / wedged families) is asserted against the live default values. A change to
    any default that inverts a load-bearing ordering fails here, with the
    constraint's intent in the failure message.
    """
    failures = validate_clock_lattice()
    assert failures == [], "lattice violated:\n  " + "\n  ".join(failures)


def test_dispatch_client_outlives_owner_wait_and_release_preflight() -> None:
    """A true start must not surface as a client timeout and invite a retry."""
    release_preflight_max_s = 3 * 15.0
    assert (
        deploy.ORCHESTRATION_OWNER_WAIT_S + release_preflight_max_s
    ) < deploy.CLUSTER_DISPATCH_TIMEOUT_S


# --- the checker must catch every kind of violation it declares ---------------


def test_checker_catches_lt_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deploy progress deadline must fit inside its ownership lease."""
    monkeypatch.setattr(deploy, "NO_PROGRESS_TIMEOUT_S", CLOCKS["LOCK_TTL_S"].get() + 10)
    failures = validate_clock_lattice()
    assert any("NO_PROGRESS_TIMEOUT_S < LOCK_TTL_S" in f for f in failures)


def test_checker_catches_eq_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A drifted updater lease TTL (no longer equal to NO_PROGRESS) must be
    reported — two clocks disagreeing about "stopped making progress"."""
    monkeypatch.setattr(
        "shared.timing.UPDATER_LEASE_TTL_S",
        deploy.NO_PROGRESS_TIMEOUT_S + 100,
    )
    failures = validate_clock_lattice()
    assert any("UPDATER_LEASE_TTL_S == NO_PROGRESS_TIMEOUT_S" in f for f in failures)


def test_checker_catches_derived_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wedged threshold shrunk below its derivation (exec node + LLM retry
    budget) must be reported — an operator override that tightens wedged
    detection below a healthy agent's longest legitimate stall."""
    monkeypatch.setattr("shared.config.settings.daemon.wedged_agent_inbound_age_seconds", 500.0)
    failures = validate_clock_lattice()
    assert any("WEDGED_AGE_SEC >= EXEC_NODE_TIMEOUT_S" in f for f in failures)


def test_checker_catches_scaled_eq_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lease TTL that drifts off its 10x renewal ratio must be reported."""
    monkeypatch.setattr(deploy, "AGENT_LEASE_TTL_S", 700.0)  # not 10 x 60
    failures = validate_clock_lattice()
    assert any("AGENT_LEASE_TTL_S == 10 * AGENT_LEASE_RENEW_INTERVAL_S" in f for f in failures)


def test_assert_clock_lattice_raises_on_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fail-fast entry point raises, never returns, on a violation."""
    from shared.timing import ClockLatticeError

    monkeypatch.setattr(deploy, "NO_PROGRESS_TIMEOUT_S", CLOCKS["LOCK_TTL_S"].get() + 10)
    with pytest.raises(ClockLatticeError):
        assert_clock_lattice()


# --- registration completeness: no family-module clock may sit outside CLOCKS --


def test_every_deploy_family_clock_is_registered() -> None:
    """Same completeness pin for the deploy family module."""
    registered = set(CLOCKS)
    for name in dir(deploy):
        if name.startswith("_"):
            continue
        value = getattr(deploy, name)
        if isinstance(value, (int, float)):
            assert name in registered, (
                f"{name} defined in deploy_timing but not registered in CLOCKS"
            )


def test_every_constraint_references_registered_clocks() -> None:
    """A constraint naming a clock that is not in CLOCKS is a typo the lattice
    cannot detect at runtime — fail here instead."""
    registered = set(CLOCKS)
    for c in CONSTRAINTS:
        for expr in (c.lhs, c.rhs):
            for token in expr.replace(" + ", " ").replace(" * ", " ").split():
                if token.isdigit():  # scalar multiplier, not a clock
                    continue
                assert token in registered, (
                    f"constraint references unknown clock {token!r} in {c.lhs} {c.kind} {c.rhs}"
                )


def test_constraint_kinds_are_known() -> None:
    """Only the kinds the checker implements may be declared."""
    for c in CONSTRAINTS:
        assert c.kind in {"<", "<=", "==", ">="}, f"unknown constraint kind {c.kind!r}"
