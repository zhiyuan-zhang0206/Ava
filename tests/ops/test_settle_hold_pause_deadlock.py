"""The settle-hold/pause deadlock, composed (issue #1116).

Issue #1020 taught the pin and code controllers that a **settle hold naming this
host** is not a deploy to defer to: nothing executes under it, and the convergence it
waits for is exactly what those controllers produce. That exception is correct and it
was unreachable in the shape this file pins.

`PauseController` sits ahead of both and blocks `BlockScope.ALL`, so `ops.manager`
short-circuits the round. A host that is *paused* and *named by a settle hold* — which
is what a rollout leaves behind when an agent-runner's updater dies, since
`_still_converging` folds `POLL_STALLED` hosts into the hold — therefore never reached
the two controllers carrying the exception. The hold waited for a convergence only they
produce; the pause forbade them from running; `settle_hosts_converged` could never
observe convergence, so the hold never released early and lapsed on `SETTLE_TTL_S`.

The unit tests beside this one pin each half. This file asserts the composed property:
**in the incident's state, the pause gate is what stands between the host and its own
heal, and it now yields.**
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ops.controllers import stranded_pause as sp
from ops.controllers.base import BlockScope, ReconcileResult
from ops.manager import ControllerManager, build_controllers
from shared.cluster_lock import DeployLease, settle_note

_THIS_HOST = "laptop-host"


class _SpyController:
    """Stands in for the pin / code controllers — the ones that carry the settle-hold
    exception. It only records whether the round reached it."""

    def __init__(self, name: str, reached: list[str]) -> None:
        self.name = name
        self._reached = reached

    def reconcile(self, role: str) -> ReconcileResult:
        self._reached.append(self.name)
        return ReconcileResult(dimension=self.name, blocks=BlockScope.NONE)


@pytest.fixture
def stranded_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, list]:
    """The state a rollout leaves on an agent-runner whose updater died: Phase A's
    `cluster_paused` flag, well past the unowned bound; a settle hold naming this host;
    nothing executing locally."""
    from datetime import UTC, datetime, timedelta

    from shared.host_deploy_state import HostDeployState

    holder: dict[str, HostDeployState | None] = {
        "state": HostDeployState(
            machine=_THIS_HOST,
            posture="paused",
            updated_at=datetime.now(UTC) - timedelta(seconds=sp.STRANDED_PAUSE_TIMEOUT_S + 60),
            updater_lease_expires_at=None,
        )
    }

    def _read(machine: str | None = None, **_kw: object) -> HostDeployState | None:
        return holder["state"]

    monkeypatch.setattr("shared.host_deploy_state.read", _read)
    monkeypatch.setattr(sp, "machine_name", lambda: _THIS_HOST)
    monkeypatch.setattr("ops.cluster.current_orchestration", lambda: None)
    monkeypatch.setattr(
        sp,
        "read_update_lease",
        lambda: DeployLease(
            holder="gateway-host:pid65237",
            held_for_s=300.0,
            expires_in_s=600.0,
            note=settle_note([_THIS_HOST]),
        ),
    )
    unpaused: list[bool] = []

    def _unpause() -> None:
        unpaused.append(True)
        holder["state"] = None  # the unpause clears the paused row

    monkeypatch.setattr(sp, "unpause_local_cluster", _unpause)
    return {"unpaused": unpaused}


async def test_the_pause_gate_is_what_hides_the_settle_hold_exception_downstream(
    stranded_host: dict[str, list],
) -> None:
    """Why the fix had to land in the pause controller and not beside the others.

    While the flag is present the round stops at `pause`, so the controllers holding
    the #1020 exception do not run — the exception cannot fire on the one host the hold
    is waiting for. This is a structural fact about the ordering, and it is why teaching
    only `pin` and `code` about `awaits()` left this shape deadlocked."""
    reached: list[str] = []
    mgr = ControllerManager([sp.PauseController(), _SpyController("pin", reached)])
    assert await mgr.reconcile("agent-runner") is BlockScope.ALL
    assert reached == []  # short-circuited: the settle-hold exception never gets a turn


async def test_a_round_in_the_incident_state_clears_the_pause(
    stranded_host: dict[str, list],
) -> None:
    """The mutual wait breaks here. The hold is the orchestration's record that this
    host's pause lost its owner, so it does not own it; the pause is unowned and past
    its bound, and the round self-unpauses."""
    mgr = ControllerManager([sp.PauseController()])
    assert await mgr.reconcile("agent-runner") is BlockScope.ALL  # the acting round still blocks
    assert stranded_host["unpaused"] == [True]
    assert mgr.last_results()["pause"].acted is True


async def test_the_next_round_reaches_the_controllers_that_converge_the_host(
    stranded_host: dict[str, list],
) -> None:
    """And convergence follows: once the flag is gone the round runs through, so the
    pin/code controllers reach their own settle-hold exception and produce the
    `head_sha == pin AND running_sha == pin` that `settle_hosts_converged` re-probes
    for — which is what releases the hold early instead of idling out `SETTLE_TTL_S`."""
    reached: list[str] = []
    mgr = ControllerManager([sp.PauseController(), _SpyController("pin", reached)])
    await mgr.reconcile("agent-runner")  # unpauses, still blocks
    assert await mgr.reconcile("agent-runner") is BlockScope.NONE
    assert reached == ["pin"]


def test_the_pause_controller_still_precedes_pin_and_code() -> None:
    """The property above is only interesting while `pause` runs ahead of them. If the
    order ever changes this file is testing something else, so say so here."""
    order = [c.name for c in build_controllers()]
    assert order.index("pause") < order.index("pin") < order.index("code")
