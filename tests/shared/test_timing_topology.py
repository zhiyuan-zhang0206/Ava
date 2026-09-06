"""The clock lattice — every declared ordering between timing constants holds.

`shared/timing.py` is the single authority for the load-bearing orderings between
timeouts / graces / poll intervals (boot family, deploy family, agent lease,
updater lease, wedged derivation, controller scan cadence). These tests are the
default-value pin: they assert every declared constraint against the settings
defaults, prove the checker itself catches each kind of violation, and prove
every clock defined in the family modules is registered in the lattice (so a new
clock cannot silently join the lattice without declaring its neighbours).

The three boot-family ordering tests migrated here from `tests/shared/test_config.py`
carry their incident history in their docstrings — the 2026-07-30 spawn incident
is why they exist, and why the ordering now lives in one place.
"""

from __future__ import annotations

import pytest

import shared.boot_timing as boot
import shared.deploy_timing as deploy
from shared.config import settings
from shared.timing import CLOCKS, CONSTRAINTS, assert_clock_lattice, validate_clock_lattice


def test_default_lattice_holds() -> None:
    """The full declared lattice must hold for the settings defaults.

    This is the topology pin: every constraint in `shared.timing.CONSTRAINTS`
    (26 orderings across the boot / deploy / schedule-supervision / agent-lease /
    updater / wedged / restarter families) is asserted against the live default values. A change to
    any default that inverts a load-bearing ordering fails here, with the
    constraint's intent in the failure message.
    """
    failures = validate_clock_lattice()
    assert failures == [], "lattice violated:\n  " + "\n  ".join(failures)


def test_removed_self_respawn_has_no_competing_grace_clock() -> None:
    """Only the controller's bounded boot clocks remain; no atexit fallback timer."""
    assert "SELF_RESPAWN_RESTARTER_GRACE_S" not in CLOCKS
    assert "SELF_RESPAWN_RESTARTER_SCHEDULING_MARGIN_S" not in CLOCKS


def test_restarter_poll_override_preserves_remaining_lattice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing the controller cadence must not recreate the deleted launcher."""
    monkeypatch.setattr(settings.daemon, "restarter_poll_interval_seconds", 2.0)

    assert CLOCKS["RESTARTER_POLL_INTERVAL_S"].get() == 2.0
    assert "SELF_RESPAWN_RESTARTER_GRACE_S" not in CLOCKS
    assert validate_clock_lattice() == []


def test_dispatch_client_outlives_owner_wait_and_release_preflight() -> None:
    """A true start must not surface as a client timeout and invite a retry."""
    release_preflight_max_s = 3 * 15.0
    assert (
        deploy.ORCHESTRATION_OWNER_WAIT_S + release_preflight_max_s
    ) < deploy.CLUSTER_DISPATCH_TIMEOUT_S


def test_boot_chain_is_registered_and_pinned() -> None:
    """The boot chain stall < confirm < budget < grace, with the exact default
    values that the 2026-07-30 spawn incident taught us to pin."""
    stall = CLOCKS["BOOT_STALL_SEC"].get()
    confirm = CLOCKS["LAUNCH_CONFIRM_TIMEOUT_SEC"].get()
    budget = CLOCKS["BOOT_BUDGET_SEC"].get()
    grace = CLOCKS["BOOT_REAP_GRACE_SEC"].get()
    assert stall < confirm < budget < grace, (
        f"expected BOOT_STALL_SEC ({stall}s) < LAUNCH_CONFIRM_TIMEOUT_SEC "
        f"({confirm}s) < BOOT_BUDGET_SEC ({budget}s) < BOOT_REAP_GRACE_SEC "
        f"({grace}s)"
    )


def test_boot_reap_grace_exceeds_the_launch_confirm_window() -> None:
    """The reap grace must outlast the launch confirm, by construction.

    Two defaults in two files with an ordering between them, described only in
    prose ("must exceed boot plus the launch-confirm window"), is how the
    2026-07-30 spawn incident stayed hidden: the confirm window was raised
    without the grace and the reaper started taking rows from launches that were
    still legitimately waiting. The grace is also the ceiling on the confirm's
    one live-child extension, so an inverted pair does not just lose margin — it
    makes the extension a no-op, silently restoring the behavior this test's
    incident was about.
    """
    assert boot.LAUNCH_CONFIRM_TIMEOUT_SEC < boot.BOOT_REAP_GRACE_SEC, (
        f"boot_reap_grace_seconds ({boot.BOOT_REAP_GRACE_SEC}s) must "
        f"exceed launch_confirm_timeout_seconds ({boot.LAUNCH_CONFIRM_TIMEOUT_SEC}s) "
        "— with headroom for the boot that runs between them"
    )


def test_boot_stall_fires_before_the_launch_confirm_deadline() -> None:
    """The child's watchdog must beat the launcher to the verdict.

    The launcher treats a live process as a progressing boot, and that inference
    is only sound because a stalled child has already exited itself by the time
    the launcher looks. Invert this pair and the launcher reaches its deadline
    while a wedged child is still holding a pid, reads it as "slow boot on a
    loaded box", and grants the extension — spending the full reap grace on a
    boot that stopped moving long before, which is the residual the child-owned
    deadline exists to remove.
    """
    assert boot.BOOT_STALL_SEC < boot.LAUNCH_CONFIRM_TIMEOUT_SEC, (
        f"agent_boot_stall_seconds ({boot.BOOT_STALL_SEC}s) must be below "
        f"launch_confirm_timeout_seconds ({boot.LAUNCH_CONFIRM_TIMEOUT_SEC}s) — "
        "otherwise the launcher adjudicates a wedged child before the child has "
        "removed itself"
    )


def test_boot_budget_stays_under_the_reaper_grace() -> None:
    """The child must be gone before the reaper could claim its row.

    The stall window bounds one phase, so it bounds the whole boot only at
    phases x stall — arithmetic over a number that moves whenever a boot phase is
    added, and which at 4 phases x 30s already equals the grace exactly. The
    restarter's dead-birth reaper takes an unclaimed 'idling' row on age alone: its clock
    is `status_changed_at`, which only a status flip resets, so no amount of
    pre-flip progress holds it off. A boot allowed to outlive the grace would have
    its row reaped out from under a live, progressing child — the 2026-07-30
    incident again, relocated from the launcher to the reaper, and the reason that
    incident's fix capped the launcher's own extension at this same grace.
    """
    assert boot.BOOT_STALL_SEC < boot.BOOT_BUDGET_SEC < boot.BOOT_REAP_GRACE_SEC, (
        f"expected agent_boot_stall_seconds ({boot.BOOT_STALL_SEC}s) < "
        f"agent_boot_budget_seconds ({boot.BOOT_BUDGET_SEC}s) < "
        f"boot_reap_grace_seconds ({boot.BOOT_REAP_GRACE_SEC}s); a "
        "budget at or above the grace lets the reaper take the row of a child "
        "that is still alive, and a budget below the stall window makes the "
        "stall window unreachable"
    )


# --- the checker must catch every kind of violation it declares ---------------


def test_checker_catches_lt_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """An inverted boot chain (stall raised above confirm) must be reported."""
    monkeypatch.setattr(boot, "BOOT_STALL_SEC", boot.LAUNCH_CONFIRM_TIMEOUT_SEC + 10)
    failures = validate_clock_lattice()
    assert any("BOOT_STALL_SEC < LAUNCH_CONFIRM_TIMEOUT_SEC" in f for f in failures)


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


def test_checker_catches_idling_wedged_threshold_violation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idling detector must allow two fallback SELECT rounds before recovery."""
    monkeypatch.setattr(
        "shared.config.settings.daemon.wedged_idling_agent_inbound_age_seconds",
        30.0,
    )
    failures = validate_clock_lattice()
    assert any("IDLING_WEDGED_AGE_SEC >= 2 * IDLE_CLAIM_BACKSTOP_S" in f for f in failures)


def test_checker_catches_scaled_eq_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lease TTL that drifts off its 10x renewal ratio must be reported."""
    monkeypatch.setattr(deploy, "AGENT_LEASE_TTL_S", 700.0)  # not 10 x 60
    failures = validate_clock_lattice()
    assert any("AGENT_LEASE_TTL_S == 10 * AGENT_LEASE_RENEW_INTERVAL_S" in f for f in failures)


def test_assert_clock_lattice_raises_on_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fail-fast entry point raises, never returns, on a violation."""
    from shared.timing import ClockLatticeError

    monkeypatch.setattr(boot, "BOOT_STALL_SEC", boot.BOOT_REAP_GRACE_SEC + 10)
    with pytest.raises(ClockLatticeError):
        assert_clock_lattice()


# --- registration completeness: no family-module clock may sit outside CLOCKS --


def test_every_boot_family_clock_is_registered() -> None:
    """A clock added to shared/boot_timing.py without registering it in
    shared/timing.py.CLOCKS fails here — the lattice cannot order a clock it
    cannot see."""
    registered = set(CLOCKS)
    for name in dir(boot):
        if name.startswith("_") or name in ("settings",):
            continue
        value = getattr(boot, name)
        if isinstance(value, (int, float)):
            assert name in registered, f"{name} defined in boot_timing but not registered in CLOCKS"


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
