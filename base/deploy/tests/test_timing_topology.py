"""Verify all remaining hosted-runtime and maintenance timing relationships.

Tests assert defaults, exercise each constraint kind with invalid live values,
and ensure the family modules register every clock they expose.
"""

from __future__ import annotations

import pytest

import base.deploy.progress_timeout as deploy
from base.deploy.timing import CLOCKS, CONSTRAINTS, assert_clock_lattice, validate_clock_lattice


def test_default_lattice_holds() -> None:
    """The full declared lattice must hold for the settings defaults.

    This is the topology pin: every constraint in `base.deploy.timing.CONSTRAINTS`
    (deploy / schedule-supervision / agent-lease / wedged / stop families) is asserted against the live default values. A change to
    any default that inverts a load-bearing ordering fails here, with the
    constraint's intent in the failure message.
    """
    failures = validate_clock_lattice()
    assert failures == [], "lattice violated:\n  " + "\n  ".join(failures)


# --- the checker must catch every kind of violation it declares ---------------


def test_checker_catches_lt_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """One gateway preflight dial must fit inside the no-progress judgment."""
    monkeypatch.setattr(deploy, "GATEWAY_PREFLIGHT_BUDGET_S", deploy.NO_PROGRESS_TIMEOUT_S + 10)
    failures = validate_clock_lattice()
    assert any("GATEWAY_PREFLIGHT_BUDGET_S < NO_PROGRESS_TIMEOUT_S" in f for f in failures)


def test_checker_catches_derived_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wedged threshold shrunk below its derivation (exec node + LLM retry
    budget) must be reported — an operator override that tightens wedged
    detection below a healthy agent's longest legitimate stall."""
    monkeypatch.setattr("base.config.settings.daemon.wedged_agent_inbound_age_seconds", 500.0)
    failures = validate_clock_lattice()
    assert any("WEDGED_AGE_SEC >= EXEC_NODE_TIMEOUT_S" in f for f in failures)


def test_checker_catches_scaled_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lease TTL shorter than ten renewal beats must be reported."""
    monkeypatch.setattr(deploy, "AGENT_LEASE_TTL_S", 9 * deploy.AGENT_LEASE_RENEW_INTERVAL_S)
    failures = validate_clock_lattice()
    assert any("AGENT_LEASE_TTL_S >= 10 * AGENT_LEASE_RENEW_INTERVAL_S" in f for f in failures)


def test_legacy_adoption_silence_is_floored_by_the_renewal_beat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slower beat that would let a live predecessor read as silent is reported."""
    monkeypatch.setattr(deploy, "AGENT_LEASE_RENEW_INTERVAL_S", 20.0)
    failures = validate_clock_lattice()
    assert any(
        "LEGACY_HOST_ADOPTION_SILENCE_S >= 4 * AGENT_LEASE_RENEW_INTERVAL_S" in f for f in failures
    )


def test_a_default_bundle_lifetime_past_the_cap_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`issue-unit` without `--ttl-hours` must issue a bundle it accepts."""
    monkeypatch.setattr(deploy, "UNIT_BUNDLE_TTL_S", deploy.UNIT_BUNDLE_MAX_TTL_S + 1)
    failures = validate_clock_lattice()
    assert any("UNIT_BUNDLE_TTL_S <= UNIT_BUNDLE_MAX_TTL_S" in f for f in failures)


def test_assert_clock_lattice_raises_on_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fail-fast entry point raises, never returns, on a violation."""
    from base.deploy.timing import ClockLatticeError

    monkeypatch.setattr(deploy, "GATEWAY_PREFLIGHT_BUDGET_S", deploy.NO_PROGRESS_TIMEOUT_S + 10)
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
                f"{name} defined in progress_timeout but not registered in CLOCKS"
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
