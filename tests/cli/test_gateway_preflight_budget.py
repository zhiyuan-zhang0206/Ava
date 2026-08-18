"""One refused packet is not proof the gateway is down.

The runner's self-update preflight is validate-before-kill: it dials the gateway and,
on failure, declines with `RESTART_DECLINED_EXIT_CODE` rather than stopping services it
might not be able to restart. Declining is right; declining on the *first* refused dial
was not. On the 2026-08-01 rollout (issue #1151) a ~9 s gateway restart hole produced
exactly one ECONNREFUSED on two runners, and both sat stranded until the settle lease
lapsed ~15 minutes later.

The hole that opened it is closed at its source (`_phase_b_targets`); this budget is
what remains for holes the rollout did not open. So the tests are about **what the
budget must and must not buy**:

- a gateway that comes back inside the budget is waited for, not declined;
- a gateway that is genuinely down still fails, inside the bound, so `INCOMPLETE` stays
  fast and legible rather than becoming slow;
- the healthy path pays nothing — a budget that taxes every good `ava start` would be a
  bad trade even when it is correct;
- a status the gateway *chose* to send is still terminal on the first dial.
"""

from __future__ import annotations

import pytest

from cli.commands import _repo
from cli.commands._repo import GatewayProbe
from shared.deploy_timing import GATEWAY_PREFLIGHT_BUDGET_S, NO_PROGRESS_TIMEOUT_S

URL = "http://gw:8000"


class _Clock:
    """A monotonic clock only `sleep()` advances, so the budget is reached in exactly
    the number of dials the interval implies and in no wall-clock time at all."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def preflight(monkeypatch: pytest.MonkeyPatch):
    """Drive `_probe_gateway_or_die` against a scripted sequence of probe results.

    Returns a callable taking the sequence (its last element repeats forever, so an
    "always down" gateway is one element) and yielding (rc, clock)."""
    clock = _Clock()
    monkeypatch.setattr(_repo, "time", clock)

    def _drive(script: list[GatewayProbe]) -> tuple[int, _Clock]:
        remaining = list(script)

        def _probe(_url: str, *, timeout_s: float = 10.0) -> GatewayProbe:
            return remaining.pop(0) if len(remaining) > 1 else remaining[0]

        monkeypatch.setattr(_repo, "probe_gateway_once", _probe)
        return _repo._probe_gateway_or_die(URL), clock

    return _drive


REFUSED = GatewayProbe(None, "[Errno 61] Connection refused")
SERVING = GatewayProbe(200, "{}")


def test_a_gateway_that_comes_back_inside_the_budget_is_not_declined(preflight) -> None:
    """The defect. A restart hole a few seconds wide used to cost the host its whole
    rollout; now the second dial finds the gateway and the self-update proceeds."""
    rc, clock = preflight([REFUSED, REFUSED, SERVING])

    assert rc == 0
    assert sum(clock.slept) <= GATEWAY_PREFLIGHT_BUDGET_S  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]


def test_a_dead_gateway_still_fails_and_inside_the_bound(preflight) -> None:
    """The budget must not become a way to survive a gateway that really died — that
    needs the watchdog's own round, and waiting for it here would turn a legible
    INCOMPLETE into a slow one. It fails, having spent no more than the budget."""
    rc, clock = preflight([REFUSED])

    assert rc == 1
    assert sum(clock.slept) <= GATEWAY_PREFLIGHT_BUDGET_S  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]


def test_a_reachable_gateway_costs_one_dial_and_no_sleep(preflight) -> None:
    """Every healthy `ava start` and every healthy self-update runs this. It must be
    one probe and zero waiting, exactly as before the budget existed."""
    rc, clock = preflight([SERVING])

    assert rc == 0
    assert clock.slept == []  # pyright: ignore[reportUnknownMemberType]


def test_a_5xx_is_retried_on_the_same_budget(preflight) -> None:
    """A gateway that answers 5xx is booting, which is the same transient shape as one
    that answers nothing — it was already retried, and it keeps the same budget rather
    than a second, separately-tuned one."""
    rc, _clock = preflight([GatewayProbe(503, "starting"), SERVING])

    assert rc == 0


def test_a_refusal_the_gateway_chose_is_terminal_at_once(preflight) -> None:
    """401/403/404 mean a credential or route mismatch: the gateway is up and saying no.
    No amount of waiting resolves it, so it must not spend the budget."""
    rc, clock = preflight([GatewayProbe(403, "forbidden")])

    assert rc == 1
    assert clock.slept == []  # pyright: ignore[reportUnknownMemberType]


def test_the_budget_sits_far_under_the_no_progress_bound() -> None:
    """This wait happens inside the self-update leg that `NO_PROGRESS_TIMEOUT_S` bounds
    whole. If it ever approached that number, absorbing a restart hole would be
    indistinguishable from the host having stopped making progress."""
    assert GATEWAY_PREFLIGHT_BUDGET_S < NO_PROGRESS_TIMEOUT_S / 10
