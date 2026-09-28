"""A finite executor's deploy lease survives a missed renewal round.

`shared.deploy_timing` promises that a missed round (a slow database, one
dropped connection) is never fatal: a renewal that raises is retried until
the lease could lapse before the next round. A renewal that answers "not
yours" — another holder, or already expired — loses it at once. The fleet
coordinator's `DeployLease` and PITR activation's renewer keep that promise.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from uuid import uuid4

import pytest

from cli.release_fleet.gateway import DeployLease
from services.pitr import activation_lease
from shared import cluster_lock, deploy_timing

_INTERVAL_S = 0.01
_TTL_S = 0.2


@pytest.fixture(autouse=True)
def _fast_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deploy_timing, "LEASE_RENEW_INTERVAL_S", _INTERVAL_S)
    monkeypatch.setattr(activation_lease, "LEASE_RENEW_INTERVAL_S", _INTERVAL_S)
    monkeypatch.setattr(cluster_lock, "LEASE_RENEW_INTERVAL_S", _INTERVAL_S)
    monkeypatch.setattr(cluster_lock, "LOCK_TTL_S", _TTL_S)


def _renewals(answers: Iterator[bool | None]) -> Callable[..., bool]:
    """Each call answers the next value; None raises like a dropped connection."""

    def renew(_holder: str, **_kwargs: object) -> bool:
        answer = next(answers)
        if answer is None:
            raise ConnectionError("server closed the connection unexpectedly")
        return answer

    return renew


def _held(monkeypatch: pytest.MonkeyPatch, answers: Iterator[bool | None]) -> DeployLease:
    monkeypatch.setattr(cluster_lock, "renew_update_lock", _renewals(answers))

    def acquired(_holder: str, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(cluster_lock, "acquire_update_lock", acquired)
    lease = DeployLease(uuid4())
    lease.hold()
    return lease


def _forever(*first: bool | None, then: bool | None) -> Iterator[bool | None]:
    yield True  # `hold` re-arms by renewing first
    yield from first
    while True:
        yield then


def test_the_fleet_lease_survives_a_missed_round(monkeypatch: pytest.MonkeyPatch) -> None:
    lease = _held(monkeypatch, _forever(None, None, then=True))
    time.sleep(_TTL_S * 2)
    lease.require()
    lease.release()


def test_the_fleet_lease_is_lost_once_failing_rounds_could_let_it_lapse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _held(monkeypatch, _forever(then=None))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            lease.require()
        except RuntimeError:
            break
        time.sleep(_INTERVAL_S)
    with pytest.raises(RuntimeError, match="lost its cluster deploy lease"):
        lease.require()
    lease.release()


def test_the_fleet_lease_answered_not_ours_is_lost_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _held(monkeypatch, _forever(then=False))
    time.sleep(_INTERVAL_S * 10)
    with pytest.raises(RuntimeError, match="lost its cluster deploy lease"):
        lease.require()
    lease.release()


def test_pitr_activation_survives_a_missed_round(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        activation_lease, "renew_update_lock", _renewals(_forever(None, None, then=True))
    )

    def action(stop: threading.Event) -> str:
        time.sleep(_TTL_S * 2)
        assert not stop.is_set()
        return "activated"

    assert activation_lease.run_while_renewing("pitr:test", action) == "activated"


def test_pitr_activation_answered_not_ours_stops_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(activation_lease, "renew_update_lock", _renewals(_forever(then=False)))

    def action(stop: threading.Event) -> str:
        assert stop.wait(5), "the lost lease stops the action"
        return "stopped"

    with pytest.raises(RuntimeError, match="lost its deployment lease"):
        activation_lease.run_while_renewing("pitr:test", action)
