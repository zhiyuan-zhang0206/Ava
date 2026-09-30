"""The deploy window — what the health probe asks before it grades an outage.

Two signals are pinned, and they pull in different directions:

- **A live lease holds even while every host is unreachable.** It is read from the
  database, not probed from the hosts, so a machine whose `ops` daemon is stopped
  (`test_lease_holds_even_when_every_host_is_unreachable`) cannot make it read idle.
- **A stale posture on an operator-excluded machine must not read as a deploy**,
  while a live updater behind the same row still must — the cohort filter plus its
  freshness/exclusion diagnostics (issue #2160). That is the `signal 2: the cohort
  filter...` section below.
"""

from __future__ import annotations

from typing import Any

import pytest

from base.deploy.state.cluster_lock import DeployLease
from base.deploy.state.host_deploy_state import HostDeployState
from ops import deploy_window as dw

_EXECUTING = DeployLease(holder="gateway-host:pid81319", held_for_s=60.0, expires_in_s=1740.0)


@pytest.fixture(autouse=True)
def _quiet_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    """An idle cluster. Each test re-arms exactly the signal it is about."""
    monkeypatch.setattr("base.deploy.state.cluster_lock.read_update_lease", lambda: None)
    monkeypatch.setattr("base.cluster.machines.list_all", list)
    monkeypatch.setattr("base.cluster.machine_exclusions.list_excluded_machines", list)
    monkeypatch.setattr(dw, "_read_deploy_states", dict)


# ─── signal 1: the lease ─────────────────────────────────────────────────────


def test_idle_cluster_is_not_a_deploy_window() -> None:
    assert dw.deploy_in_flight().active is False


def test_lease_holds_even_when_every_host_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lease is read from the database, so a stopped `ops` daemon on every
    host cannot make a held lease read idle."""
    monkeypatch.setattr("base.deploy.state.cluster_lock.read_update_lease", lambda: _EXECUTING)
    monkeypatch.setattr("base.cluster.machines.list_all", lambda: [("win", "http://win:8600")])

    window = dw.deploy_in_flight()
    assert window.active is True
    assert "gateway-host:pid81319" in window.detail


def test_lease_outranks_the_posture_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Confidence order: a held lease is reported as such without reading a
    single machine's posture."""

    def _never() -> dict[str, HostDeployState]:
        raise AssertionError("read posture rows despite a live lease")

    monkeypatch.setattr("base.deploy.state.cluster_lock.read_update_lease", lambda: _EXECUTING)
    monkeypatch.setattr(dw, "_read_deploy_states", _never)
    assert dw.deploy_in_flight().active is True


# ─── signal 2: a transition that takes no lease ──────────────────────────────


def test_sees_a_lease_less_update_on_another_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Host-local maintenance takes no cluster lease, so no lease exists to see.
    R1 (Task #1021): the signal is the machine's `host_deploy_state` posture row."""
    from datetime import UTC, datetime

    from base.deploy.state.host_deploy_state import HostDeployState

    monkeypatch.setattr("base.cluster.machines.list_all", lambda: [("win", "http://win:8600")])
    monkeypatch.setattr(
        dw,
        "_read_deploy_states",
        lambda: {
            "win": HostDeployState(
                machine="win",
                posture="converging",
                updated_at=datetime.now(UTC),
                updater_lease_expires_at=None,
            )
        },
    )

    window = dw.deploy_in_flight()
    assert window.active is True
    assert "win" in window.detail


def test_unreachable_machine_does_not_block_a_deploy_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no lease and no posture row, an absent host is not a deploying host —
    otherwise one dead machine would read as a deploy forever."""
    monkeypatch.setattr("base.cluster.machines.list_all", lambda: [("gone", "http://gone:8600")])
    monkeypatch.setattr(dw, "_read_deploy_states", dict)
    assert dw.deploy_in_flight().active is False


# ─── signal 2: the cohort filter, freshness and exclusion (issue #2160) ──────


def _posture(machine: str, posture: str, *, age_s: float, lease_s: float | None) -> HostDeployState:
    """A `host_deploy_state` row anchored the way `read_all()` anchors it: the
    "now" the liveness judgment compares against is the DB's own, selected with
    the row, so tests date a row relative to its own clock, not the wall's."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    return HostDeployState(
        machine=machine,
        posture=posture,
        updated_at=now - timedelta(seconds=age_s),
        updater_lease_expires_at=None if lease_s is None else now + timedelta(seconds=lease_s),
        db_now=now,
    )


def _paused_since_0909():
    from datetime import UTC, datetime

    return datetime(2026, 9, 9, 13, 54, 31, tzinfo=UTC)


def test_excluded_machines_stale_posture_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The 2026-09-10 production refusal, replayed** (issue #2160). `win` was
    operator-excluded on 09-09 (paused 13:54:31Z, then stopped) and its posture
    froze at `paused` — no live updater lease, and no rollout roster that
    contains it. The recovery that would clear such a row on a live host never
    runs on an excluded machine (leaving it alone is what preserving the
    exclusion means), so before the cohort filter this one row refused every
    update cluster-wide until the operator resumed the machine or learned to
    pass `--force`."""
    monkeypatch.setattr("base.cluster.machines.list_all", lambda: [("win", "http://win:18121")])
    monkeypatch.setattr(
        "base.cluster.machine_exclusions.list_excluded_machines",
        lambda: [("win", "paused", _paused_since_0909())],
    )
    monkeypatch.setattr(
        dw,
        "_read_deploy_states",
        lambda: {"win": _posture("win", "paused", age_s=31 * 3600, lease_s=None)},
    )

    assert dw.deploy_in_flight().active is False


def test_a_live_updater_outranks_the_exclusion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exclusion withholds stale *history*, not a live owner: a converging row
    whose updater lease is unexpired is a deploy wherever it runs, so the very
    machine that must not block above still blocks here — and `detail` says
    both facts, because a refusal that names an excluded machine owes the
    reader the reason."""
    monkeypatch.setattr("base.cluster.machines.list_all", lambda: [("win", "http://win:18121")])
    monkeypatch.setattr(
        "base.cluster.machine_exclusions.list_excluded_machines",
        lambda: [("win", "paused", _paused_since_0909())],
    )
    monkeypatch.setattr(
        dw,
        "_read_deploy_states",
        lambda: {"win": _posture("win", "converging", age_s=30, lease_s=900)},
    )

    window = dw.deploy_in_flight()
    assert window.active is True
    assert "machine 'win' is mid-deploy" in window.detail
    assert "live updater lease" in window.detail
    assert "operator-excluded (paused since 2026-09-09T13:54:31+00:00)" in window.detail


def test_stale_posture_on_a_cohort_machine_still_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The filter must not widen. Without an operator latch a lease-less row
    keeps the conservative reading — the machine is a rollout target, its
    checkout may have moved, and the cluster's own recovery (not this window)
    is what ends the state. `detail` now dates the evidence so the reader can
    tell how stale "mid-deploy" is."""
    monkeypatch.setattr("base.cluster.machines.list_all", lambda: [("macmini", "http://m:8106")])
    monkeypatch.setattr(
        dw,
        "_read_deploy_states",
        lambda: {"macmini": _posture("macmini", "converging", age_s=2 * 86400, lease_s=None)},
    )

    window = dw.deploy_in_flight()
    assert window.active is True
    assert "no live updater lease" in window.detail
    assert "2d ago" in window.detail


def test_a_staging_machine_with_a_stale_posture_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The third latch. A staging host is registered and visible but never a
    rollout target, and the flag carries no date column — the diagnostic reads
    it bare."""
    monkeypatch.setattr("base.cluster.machines.list_all", lambda: [("stage", "http://stage:9000")])
    monkeypatch.setattr(
        "base.cluster.machine_exclusions.list_excluded_machines",
        lambda: [("stage", "staging", None)],
    )
    monkeypatch.setattr(
        dw,
        "_read_deploy_states",
        lambda: {"stage": _posture("stage", "converging", age_s=86400, lease_s=None)},
    )

    assert dw.deploy_in_flight().active is False


def test_the_skip_line_names_exclusion_freshness_and_lease(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    """Issue #2160 diagnostics: a skipped row is neither silent nor a bare
    "not blocking" — the line carries the machine, the exclusion and its date,
    the posture's age and the absence of a live lease, so "deliberately
    ignored" cannot be confused with "never read"."""
    monkeypatch.setattr("base.cluster.machines.list_all", lambda: [("win", "http://win:18121")])
    monkeypatch.setattr(
        "base.cluster.machine_exclusions.list_excluded_machines",
        lambda: [("win", "paused", _paused_since_0909())],
    )
    monkeypatch.setattr(
        dw,
        "_read_deploy_states",
        lambda: {"win": _posture("win", "paused", age_s=3600, lease_s=None)},
    )

    assert dw.deploy_in_flight().active is False
    lines = [r["message"] for r in loguru_records if "[deploy-window]" in r["message"]]
    assert len(lines) == 1
    assert "machine 'win'" in lines[0]
    assert "operator-excluded (paused since 2026-09-09T13:54:31+00:00)" in lines[0]
    assert "posture=paused" in lines[0]
    assert "no live updater lease" in lines[0]


def test_an_unreadable_exclusion_read_still_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure direction: the exclusion map can only withhold a refusal, so
    "could not read" must not come back as "nothing is excluded" — a Postgres
    hiccup degrades to the pre-#2160 reading (every non-idle posture blocks),
    never to a pardoned stale deploy."""

    def _fail():
        raise RuntimeError("db down")

    monkeypatch.setattr("base.cluster.machine_exclusions.list_excluded_machines", _fail)
    monkeypatch.setattr("base.cluster.machines.list_all", lambda: [("win", "http://win:18121")])
    monkeypatch.setattr(
        dw,
        "_read_deploy_states",
        lambda: {"win": _posture("win", "paused", age_s=31 * 3600, lease_s=None)},
    )

    assert dw.deploy_in_flight().active is True


# ─── never raises ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "broken",
    [
        "base.deploy.state.cluster_lock.read_update_lease",
        "base.cluster.machines.list_all",
        "base.cluster.machine_exclusions.list_excluded_machines",
        "ops.deploy_window._read_deploy_states",
    ],
)
def test_never_raises_when_a_signal_is_broken(broken: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every caller is a refusal/suppression path: a traceback would block every
    deploy or break the alerting that reads the window."""

    def _raise() -> object:
        raise RuntimeError("db gone")

    monkeypatch.setattr(broken, _raise)
    assert dw.deploy_in_flight().active is False
