"""Captured fleet policy, threshold arithmetic and frozen cohort capture."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cli.release_fleet.policy import (
    AlertRoute,
    Cohort,
    FleetPolicy,
    UnitCohort,
    UnitKey,
    capture_cohort,
    drain_report,
)
from shared.deploy.maintenance.state import MaintenanceHold
from tests.lifecycle.release_fleet.conftest import GATEWAY, RUNNER, at, two_units


def test_policy_defaults_carry_the_plan_and_the_one_host_closure_bounds() -> None:
    policy = FleetPolicy()
    assert (policy.threshold_percent, policy.min_affected) == (20, 2)
    # The one-host transition's hard-coded writer-closure bounds
    # (cli/release_transition/local.py: 30 s work wait, 10 s cancel grace).
    assert (policy.close_s, policy.cancel_grace_s, policy.drain_s) == (30, 10, 90)
    assert policy.alert_route == AlertRoute()
    assert FleetPolicy.model_validate_json(policy.model_dump_json()) == policy


@pytest.mark.parametrize(
    ("affected", "cohort", "exceeds"),
    [
        (2, 10, False),  # exactly 20 %: not above the threshold
        (3, 10, True),
        (1, 5, False),  # exactly 20 % again, also below min_affected
        (1, 3, False),  # 33 % but below min_affected = 2
        (2, 3, True),
        (1, 1, False),  # a one-agent cohort never rolls back on one failure
        (0, 0, False),  # empty cohort
        (0, 10, False),
        (10, 10, True),
    ],
)
def test_threshold_is_strictly_above_and_at_least_min_affected(
    affected: int, cohort: int, exceeds: bool
) -> None:
    assert FleetPolicy().exceeds(affected, cohort) is exceeds


def test_threshold_extremes() -> None:
    assert FleetPolicy(threshold_percent=0, min_affected=1).exceeds(1, 100)
    assert not FleetPolicy(threshold_percent=100, min_affected=1).exceeds(100, 100)
    with pytest.raises(ValueError, match="subset"):
        FleetPolicy().exceeds(3, 2)


@pytest.mark.parametrize(
    "fields",
    [
        {"threshold_percent": 101},
        {"threshold_percent": -1},
        {"min_affected": 0},
        {"watch_s": 0},
        {"close_s": 0},
        {"cancel_grace_s": 0},
    ],
)
def test_policy_refuses_out_of_range_bounds(fields: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        FleetPolicy.model_validate(fields)


def test_policy_refuses_fractional_bounds_from_json() -> None:
    with pytest.raises(ValidationError):
        FleetPolicy.model_validate_json('{"watch_s": 1.5}')


@pytest.mark.parametrize("name", ["../url", "a/b", "", ".hidden", "x" * 129])
def test_webhook_route_names_a_file_not_a_path(name: str) -> None:
    with pytest.raises(ValidationError):
        AlertRoute(webhook_file=name)


def test_alert_route_accepts_a_recipient_and_a_file_name() -> None:
    route = AlertRoute(recipient_agent=1818, webhook_file="release-webhook")
    assert (route.recipient_agent, route.webhook_file) == (1818, "release-webhook")


@pytest.mark.parametrize(
    ("machine", "home"),
    [
        ("m", "relative/.ava"),
        ("m", "/a/../b"),
        ("m", "/a/"),
        ("m\n", "/a"),
        ("", "/a"),
        ("m", "C:/Users/zzy"),  # not the form a Windows unit records
        ("m", "C:\\a\\..\\b"),
    ],
)
def test_unit_key_requires_a_normalized_absolute_home(machine: str, home: str) -> None:
    with pytest.raises(ValidationError):
        UnitKey(machine=machine, home=home)


@pytest.mark.parametrize("home", ["/home/zzy/.ava", "C:\\Users\\zzy\\.ava"])
def test_unit_key_takes_the_home_in_the_unit_platform_form(home: str) -> None:
    assert UnitKey(machine="win", home=home).label == f"win:{home}"


def test_capture_orders_units_and_keeps_empty_units() -> None:
    cohort = two_units(gateway_agents=(1, 2), runner_agents=())
    assert cohort.unit_keys == (RUNNER, GATEWAY)  # sorted by (machine, home)
    assert cohort.size == 2
    assert cohort.members == {1: GATEWAY, 2: GATEWAY}
    assert cohort.agents_on(RUNNER) == ()
    assert Cohort.model_validate_json(cohort.model_dump_json()) == cohort


def test_cohort_requires_the_gateway_among_its_units() -> None:
    with pytest.raises(ValidationError, match="gateway"):
        capture_cohort(
            gateway=GATEWAY, reports=(UnitCohort(unit=RUNNER, agents=(1,)),), captured_at=at(0)
        )


def test_an_agent_belongs_to_exactly_one_unit() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        two_units(gateway_agents=(1,), runner_agents=(1,))


def test_duplicate_units_are_refused() -> None:
    with pytest.raises(ValidationError, match="unique"):
        capture_cohort(
            gateway=GATEWAY,
            reports=(UnitCohort(unit=GATEWAY), UnitCohort(unit=GATEWAY)),
            captured_at=at(0),
        )


@pytest.mark.parametrize(
    ("agents", "reaped"), [((2, 1), ()), ((1, 1), ()), ((0,), ()), ((1,), (2,))]
)
def test_unit_cohort_is_canonical(agents: tuple[int, ...], reaped: tuple[int, ...]) -> None:
    with pytest.raises(ValidationError):
        UnitCohort(unit=RUNNER, agents=agents, reaped=reaped)


def test_drain_report_takes_the_restart_cohort_and_its_cancellations() -> None:
    hold = MaintenanceHold(
        phase="drained",
        commands={7: 70, 3: 30, 5: 50},
        drained=(3, 5),
        reaped={7: "straggler"},
        # A failure the reap released is settled; the idle parked agent is no member.
        failures={7: "interrupted"},
        parked=(9,),
    )
    assert drain_report(RUNNER, hold) == UnitCohort(unit=RUNNER, agents=(3, 5, 7), reaped=(7,))


def test_drain_report_refuses_an_unfinished_or_failed_drain() -> None:
    with pytest.raises(ValueError, match="has not finished draining"):
        drain_report(RUNNER, MaintenanceHold(phase="draining", commands={1: 10}))
    failed = MaintenanceHold(phase="drained", commands={1: 10}, failures={1: "lost"})
    with pytest.raises(ValueError, match="unsettled drain failures"):
        drain_report(RUNNER, failed)
