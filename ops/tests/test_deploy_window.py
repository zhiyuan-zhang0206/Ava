"""The deploy window — what the health probe asks before it grades an outage.

The one signal is the per-machine `host_deploy_state` posture row, and it is
pinned in two directions:

- **A non-idle posture holds even while every host is unreachable.** It is read
  from the database, not probed from the hosts, so a machine whose `ops` daemon
  is stopped cannot make the window read idle.
- **A stale posture on an operator-excluded machine must not read as a deploy**
  — the cohort filter plus its freshness/exclusion diagnostics (issue #2160).
  There is no lease behind such a row any more, so an excluded machine's row is
  ignored whatever its posture.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from base.deploy.state.host_deploy_state import HostDeployState
from ops import deploy_window as dw


@pytest.fixture(autouse=True)
def _quiet_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    """An idle cluster. Each test re-arms exactly the signal it is about."""
    monkeypatch.setattr("base.cluster.machines.list_all", list)
    monkeypatch.setattr("base.cluster.machine_exclusions.list_excluded_machines", list)
    monkeypatch.setattr(dw, "_read_deploy_states", dict)


def _posture(machine: str, posture: str, *, age_s: float) -> HostDeployState:
    """A `host_deploy_state` row anchored the way `read_all()` anchors it: the
    "now" the age judgment compares against is the DB's own, selected with the
    row, so tests date a row relative to its own clock, not the wall's."""
    now = datetime.now(UTC)
    return HostDeployState(
        machine=machine,
        posture=posture,
        updated_at=now - timedelta(seconds=age_s),
        db_now=now,
    )


def _paused_since_0909() -> datetime:
    return datetime(2026, 9, 9, 13, 54, 31, tzinfo=UTC)


def _machines(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    monkeypatch.setattr(
        "base.cluster.machines.list_all", lambda: [(n, f"http://{n}:8600") for n in names]
    )


# ─── the posture signal ──────────────────────────────────────────────────────


def test_idle_cluster_is_not_a_deploy_window() -> None:
    assert dw.deploy_in_flight().active is False


def test_a_paused_posture_is_a_deploy_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """The signal is the machine's `host_deploy_state` posture row (R1, Task
    #1021): maintenance and stop write it outside the services they restart."""
    _machines(monkeypatch, "win")
    monkeypatch.setattr(
        dw, "_read_deploy_states", lambda: {"win": _posture("win", "paused", age_s=30)}
    )

    window = dw.deploy_in_flight()
    assert window.active is True
    assert "machine 'win' is mid-deploy" in window.detail
    assert "host_deploy_state.posture=paused" in window.detail


def test_an_idle_posture_is_not_a_deploy_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _machines(monkeypatch, "win")
    monkeypatch.setattr(
        dw, "_read_deploy_states", lambda: {"win": _posture("win", "idle", age_s=30)}
    )
    assert dw.deploy_in_flight().active is False


def test_unreachable_machine_does_not_block_a_deploy_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no posture row, an absent host is not a deploying host — otherwise
    one dead machine would read as a deploy forever."""
    _machines(monkeypatch, "gone")
    assert dw.deploy_in_flight().active is False


def test_a_legacy_converging_row_still_blocks_a_cohort_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retired updater's `converging` posture is no longer written, but a row
    it left behind is any non-idle posture: it keeps the conservative reading
    (the machine's checkout may have moved) until that host's `ava start`
    returns it to `idle`."""
    _machines(monkeypatch, "macmini")
    monkeypatch.setattr(
        dw,
        "_read_deploy_states",
        lambda: {"macmini": _posture("macmini", "converging", age_s=2 * 86400)},
    )

    window = dw.deploy_in_flight()
    assert window.active is True
    assert "converging" in window.detail
    assert "2d ago" in window.detail


# ─── the cohort filter, freshness and exclusion (issue #2160) ────────────────


def test_excluded_machines_stale_posture_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The 2026-09-10 production refusal, replayed** (issue #2160). `win` was
    operator-excluded on 09-09 (paused 13:54:31Z, then stopped) and its posture
    froze at `paused` — and no rollout roster contains it. Nothing runs on an
    excluded machine that would return the row to `idle`, so before the cohort
    filter this one row refused every update cluster-wide until the operator
    resumed the machine or learned to pass `--force`."""
    _machines(monkeypatch, "win")
    monkeypatch.setattr(
        "base.cluster.machine_exclusions.list_excluded_machines",
        lambda: [("win", "paused", _paused_since_0909())],
    )
    monkeypatch.setattr(
        dw,
        "_read_deploy_states",
        lambda: {"win": _posture("win", "paused", age_s=31 * 3600)},
    )

    assert dw.deploy_in_flight().active is False


def test_an_excluded_machines_row_is_ignored_whatever_its_posture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No lease outranks the exclusion any more: a `converging` row on an
    excluded machine is as stale as a `paused` one, and is ignored the same."""
    _machines(monkeypatch, "win")
    monkeypatch.setattr(
        "base.cluster.machine_exclusions.list_excluded_machines",
        lambda: [("win", "paused", _paused_since_0909())],
    )
    monkeypatch.setattr(
        dw,
        "_read_deploy_states",
        lambda: {"win": _posture("win", "converging", age_s=30)},
    )

    assert dw.deploy_in_flight().active is False


def test_an_excluded_machine_does_not_hide_a_cohort_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The filter must not widen: only the excluded machine's row is withheld."""
    _machines(monkeypatch, "win", "macmini")
    monkeypatch.setattr(
        "base.cluster.machine_exclusions.list_excluded_machines",
        lambda: [("win", "paused", _paused_since_0909())],
    )
    monkeypatch.setattr(
        dw,
        "_read_deploy_states",
        lambda: {
            "win": _posture("win", "paused", age_s=31 * 3600),
            "macmini": _posture("macmini", "paused", age_s=60),
        },
    )

    window = dw.deploy_in_flight()
    assert window.active is True
    assert "machine 'macmini'" in window.detail


def test_a_staging_machine_with_a_stale_posture_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The third latch. A staging host is registered and visible but never a
    rollout target, and the flag carries no date column — the diagnostic reads
    it bare."""
    _machines(monkeypatch, "stage")
    monkeypatch.setattr(
        "base.cluster.machine_exclusions.list_excluded_machines",
        lambda: [("stage", "staging", None)],
    )
    monkeypatch.setattr(
        dw,
        "_read_deploy_states",
        lambda: {"stage": _posture("stage", "paused", age_s=86400)},
    )

    assert dw.deploy_in_flight().active is False


def test_the_skip_line_names_exclusion_and_freshness(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    """Issue #2160 diagnostics: a skipped row is neither silent nor a bare
    "not blocking" — the line carries the machine, the exclusion and its date and
    the posture's age, so "deliberately ignored" cannot be confused with "never
    read"."""
    _machines(monkeypatch, "win")
    monkeypatch.setattr(
        "base.cluster.machine_exclusions.list_excluded_machines",
        lambda: [("win", "paused", _paused_since_0909())],
    )
    monkeypatch.setattr(
        dw,
        "_read_deploy_states",
        lambda: {"win": _posture("win", "paused", age_s=3600)},
    )

    assert dw.deploy_in_flight().active is False
    lines = [r["message"] for r in loguru_records if "[deploy-window]" in r["message"]]
    assert len(lines) == 1
    assert "machine 'win'" in lines[0]
    assert "operator-excluded (paused since 2026-09-09T13:54:31+00:00)" in lines[0]
    assert "posture=paused" in lines[0]
    assert "60m ago" in lines[0]


def test_an_unreadable_exclusion_read_still_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure direction: the exclusion map can only withhold a refusal, so
    "could not read" must not come back as "nothing is excluded" — a Postgres
    hiccup degrades to the pre-#2160 reading (every non-idle posture blocks),
    never to a pardoned stale deploy."""

    def _fail() -> object:
        raise RuntimeError("db down")

    monkeypatch.setattr("base.cluster.machine_exclusions.list_excluded_machines", _fail)
    _machines(monkeypatch, "win")
    monkeypatch.setattr(
        dw,
        "_read_deploy_states",
        lambda: {"win": _posture("win", "paused", age_s=31 * 3600)},
    )

    assert dw.deploy_in_flight().active is True


# ─── never raises ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "broken",
    [
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
