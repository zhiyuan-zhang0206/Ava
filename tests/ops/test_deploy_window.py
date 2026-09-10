"""The deploy window — the one question every pin-moving actor asks.

Three regressions are pinned here, and they pull in different directions:

- **The lease must hold while the transitioning host is unreachable.** A runner's
  self-update stops `ops`, the very daemon that answers `status_probe`, so a
  probe-only design reads "no deploy" through the whole stop -> start window: the
  longest, most dangerous part, and the exact scenario of the 2026-07-29 collision.
  `test_lease_holds_even_when_every_host_is_unreachable` is that case.
- **A settle hold must end when the cluster converges**, not when its timer runs
  out — a hold that outlives its condition is the bug class of that whole night
  (the stale `stopped_at` latch, the orphaned launchd probe). That is
  `test_settle_hold_is_released_once_every_host_reaches_the_pin`.
- **A stale posture on an operator-excluded machine must not refuse the next
  update**, while a live updater behind the same row still must — the cohort
  filter plus its freshness/exclusion diagnostics (issue #2160). That is the
  `signal 2: the cohort filter...` section below.
"""

from __future__ import annotations

from typing import Any

import pytest

from ops import deploy_window as dw
from shared.cluster_lock import DeployLease, settle_hosts, settle_note
from shared.host_deploy_state import HostDeployState

_PIN = "abc1234abc1234"
_OLD = "0ld0ld0ld0ld0l"

_EXECUTING = DeployLease(
    holder="gateway-host:pid81319", held_for_s=60.0, expires_in_s=1740.0, note=None
)
_SETTLING = DeployLease(
    holder="gateway-host:pid81319",
    held_for_s=300.0,
    expires_in_s=600.0,
    note=settle_note(["win"]),
)


def _runner(head: str, running: str) -> dict[str, object]:
    return {"serve_agent_runner": True, "head_sha": head, "running_sha": running}


@pytest.fixture(autouse=True)
def _quiet_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    """An idle cluster. Each test re-arms exactly the signal it is about."""
    monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: None)
    monkeypatch.setattr("ops.cluster.current_orchestration", lambda: None)
    monkeypatch.setattr("shared.machines.list_all", list)
    monkeypatch.setattr("shared.machine_exclusions.list_excluded_machines", list)
    monkeypatch.setattr(dw, "_read_deploy_states", dict)
    monkeypatch.setattr("shared.cluster_pin.get_cluster_target_sha", lambda: _PIN)


def _probing(results: dict[str, dict[str, object]]):
    async def _fake(machines: list[tuple[str, str | None]]) -> dict[str, dict[str, object]]:
        return {name: results[name] for name, _u in machines if name in results}

    return _fake


# ─── signal 1: the lease is the floor ────────────────────────────────────────


def test_idle_cluster_is_not_a_deploy_window() -> None:
    assert dw.deploy_in_flight().active is False


def test_lease_holds_even_when_every_host_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """**The case a probe-only design gets wrong.** `ops` is itself stopped by a
    runner's self-update, so during the stop -> start window no host answers. The
    lease is held by the gateway and does not care."""
    monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: _EXECUTING)
    monkeypatch.setattr("shared.machines.list_all", lambda: [("win", "http://win:8600")])
    monkeypatch.setattr(dw, "_probe_machines", _probing({}))  # nobody answers

    window = dw.deploy_in_flight()
    assert window.active is True
    assert "gateway-host:pid81319" in window.detail


def test_lease_outranks_the_other_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Confidence order, not cost order: an executing lease is reported as such
    without spending a probe round on hosts."""

    def _never(_machines: list[tuple[str, str | None]]) -> dict[str, dict[str, object]]:
        raise AssertionError("probed hosts despite a live lease")

    monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: _EXECUTING)
    monkeypatch.setattr(dw, "_probe_machines", _never)
    assert dw.deploy_in_flight().active is True


# ─── signal 3: a deploy that takes no lease at all ───────────────────────────


def test_sees_a_lease_less_update_on_another_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The watchdog's pin / code controllers spawn a host-local `ava-updater` without
    going near the gateway's orchestration, so no lease exists to see. The code
    controller shipped in #917 means this path gets more traffic, not less. R1
    (Task #1021): the signal is the machine's `host_deploy_state` posture row."""
    from datetime import UTC, datetime

    from shared.host_deploy_state import HostDeployState

    monkeypatch.setattr("shared.machines.list_all", lambda: [("win", "http://win:8600")])
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
    """With no lease, an absent host is not a deploying host — otherwise one dead
    machine wedges every future deploy. This is the permissive polarity, and the
    documented blind spot; the lease is what covers it."""
    monkeypatch.setattr("shared.machines.list_all", lambda: [("gone", "http://gone:8600")])
    monkeypatch.setattr(dw, "_read_deploy_states", dict)
    assert dw.deploy_in_flight().active is False


def test_remote_leg_is_skippable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _never() -> dict[str, object]:
        raise AssertionError("remote read ran despite include_remote=False")

    monkeypatch.setattr("shared.machines.list_all", lambda: [("win", "http://win:8600")])
    monkeypatch.setattr(dw, "_read_deploy_states", _never)
    assert dw.deploy_in_flight(include_remote=False).active is False


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
    monkeypatch.setattr("shared.machines.list_all", lambda: [("win", "http://win:18121")])
    monkeypatch.setattr(
        "shared.machine_exclusions.list_excluded_machines",
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
    monkeypatch.setattr("shared.machines.list_all", lambda: [("win", "http://win:18121")])
    monkeypatch.setattr(
        "shared.machine_exclusions.list_excluded_machines",
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
    monkeypatch.setattr("shared.machines.list_all", lambda: [("macmini", "http://m:8106")])
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
    monkeypatch.setattr("shared.machines.list_all", lambda: [("stage", "http://stage:9000")])
    monkeypatch.setattr(
        "shared.machine_exclusions.list_excluded_machines", lambda: [("stage", "staging", None)]
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
    monkeypatch.setattr("shared.machines.list_all", lambda: [("win", "http://win:18121")])
    monkeypatch.setattr(
        "shared.machine_exclusions.list_excluded_machines",
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

    monkeypatch.setattr("shared.machine_exclusions.list_excluded_machines", _fail)
    monkeypatch.setattr("shared.machines.list_all", lambda: [("win", "http://win:18121")])
    monkeypatch.setattr(
        dw,
        "_read_deploy_states",
        lambda: {"win": _posture("win", "paused", age_s=31 * 3600, lease_s=None)},
    )

    assert dw.deploy_in_flight().active is True


# ─── the settle hold ends on convergence, not on its timer ───────────────────


def test_settle_hold_is_released_once_every_host_reaches_the_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hold whose hosts converged in the first thirty seconds must not keep the
    cluster — and auto-rollback — blocked for the rest of its window."""
    monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: _SETTLING)
    monkeypatch.setattr("shared.machines.list_all", lambda: [("win", "http://win:8600")])
    monkeypatch.setattr(dw, "_probe_machines", _probing({"win": _runner(_PIN, _PIN)}))
    released: list[str] = []
    monkeypatch.setattr(
        "shared.cluster_lock.release_settle_hold",
        lambda h: released.append(h) or True,  # pyright: ignore[reportUnknownArgumentType]
    )

    assert dw.deploy_in_flight().active is False
    assert released == ["gateway-host:pid81319"]


def test_settle_release_prints_the_hold_duration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The settle phase's one telemetry record (C3, task #2189) is printed at the
    early release — the only moment a process is executing at the hold's end. The
    duration is the server-side elapsed the lease read carried; the host set is
    read back from the note just released."""
    import json

    settling = DeployLease(
        holder="gateway-host:pid81319",
        held_for_s=600.0,
        expires_in_s=300.0,
        note=settle_note(["win"]),
        settle_started_at=None,  # a real DB read supplies the elapsed directly
        settle_elapsed_s=600.0,
    )
    monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: settling)
    monkeypatch.setattr("shared.machines.list_all", lambda: [("win", "http://win:8600")])
    monkeypatch.setattr(dw, "_probe_machines", _probing({"win": _runner(_PIN, _PIN)}))
    monkeypatch.setattr(
        "shared.cluster_lock.release_settle_hold",
        lambda _h: True,  # pyright: ignore[reportUnknownArgumentType]
    )

    assert dw.deploy_in_flight().active is False
    lines = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("[rollout-telemetry] ")
    ]
    assert len(lines) == 1
    payload = json.loads(lines[0].removeprefix("[rollout-telemetry] "))
    assert payload == {"settle": {"dur_s": 600.0, "hosts": ["win"]}}


def test_settle_hold_stands_while_a_host_still_runs_the_old_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checkout landed, processes not — the `code` drift. That host is exactly what
    the hold is waiting for, so `head_sha` alone would release far too early."""
    monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: _SETTLING)
    monkeypatch.setattr("shared.machines.list_all", lambda: [("win", "http://win:8600")])
    monkeypatch.setattr(dw, "_probe_machines", _probing({"win": _runner(_PIN, _OLD)}))

    window = dw.deploy_in_flight()
    assert window.active is True
    assert "settling" in window.detail


def test_silence_is_not_convergence(monkeypatch: pytest.MonkeyPatch) -> None:
    """The conservative polarity, and the opposite of the refusal path's: a host that
    cannot be reached is the *least* likely to have finished, so it keeps the hold."""
    monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: _SETTLING)
    monkeypatch.setattr("shared.machines.list_all", lambda: [("win", "http://win:8600")])
    monkeypatch.setattr(dw, "_probe_machines", _probing({}))
    assert dw.deploy_in_flight().active is True


def test_release_probes_only_the_hosts_the_hold_was_taken_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The population defect.** The release used to iterate the whole `machines`
    table — every row that ever registered, with no `stopped_at`, capability or
    liveness filter — and any row that did not answer pinned convergence to False
    forever. A gateway-only unit runs no `ops` daemon and so can NEVER answer; an
    intentionally stopped host never answers either. Either one meant the hold only
    ever expired on its timer, which is the behaviour this release exists to remove.

    So the release asks about exactly the acked hosts the hold names, and the machine
    table is used only to look their dial URLs up."""
    monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: _SETTLING)
    monkeypatch.setattr(
        "shared.machines.list_all",
        lambda: [
            ("win", "http://win:8600"),  # the held host
            ("gw", "http://gw:8000"),  # gateway-only: runs no ops daemon, never answers
            ("retired", "http://retired:8600"),  # stopped / decommissioned, never answers
        ],
    )
    asked: list[list[str]] = []

    async def _fake(machines: list[tuple[str, str | None]]) -> dict[str, dict[str, object]]:
        asked.append([n for n, _u in machines])
        return {"win": _runner(_PIN, _PIN)}

    monkeypatch.setattr(dw, "_probe_machines", _fake)
    monkeypatch.setattr("shared.cluster_lock.release_settle_hold", lambda _h: True)  # pyright: ignore[reportUnknownArgumentType]

    assert dw.deploy_in_flight().active is False, "the hold must release"
    # The FIRST round is the convergence question, and it must ask only the held host.
    # (A second round follows once the hold is gone — that is signal 3 looking for a
    # lease-less host-local update, which legitimately asks everyone.)
    assert asked[0] == ["win"], "the convergence check must ask only the held host"


def test_a_held_host_that_vanished_never_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host named by the hold but no longer registered cannot be probed, so its
    convergence cannot be proven — fall back to the TTL rather than release."""
    monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: _SETTLING)
    monkeypatch.setattr("shared.machines.list_all", lambda: [("other", "http://other:8600")])
    assert dw.deploy_in_flight().active is True


def test_an_unparseable_note_never_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    """A note someone later reworded is not evidence of convergence. The format is an
    owned contract precisely so this cannot happen silently — but if it does, the
    hold falls back to its TTL rather than releasing."""
    reworded = DeployLease(
        holder="gateway-host:pid1", held_for_s=1.0, expires_in_s=600.0, note="still settling"
    )
    monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: reworded)
    assert settle_hosts(reworded.note) == []
    assert dw.deploy_in_flight().active is True


def test_settle_note_round_trips() -> None:
    """The builder and the parser are one contract; the release breaks the moment they
    disagree."""
    assert settle_hosts(settle_note(["win", "laptop-host"])) == ["laptop-host", "win"]
    assert settle_hosts(settle_note([])) == []
    assert settle_hosts(None) == []


def test_an_unreadable_pin_never_releases_a_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Convergence must be *proven*; an unknown pin proves nothing."""
    monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: _SETTLING)
    monkeypatch.setattr("shared.cluster_pin.get_cluster_target_sha", lambda: None)
    assert dw.deploy_in_flight().active is True


def test_an_executing_lease_is_never_convergence_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a settle hold (note set) is re-examined. Releasing a lease an
    orchestration is executing under would unlock a live rollout."""
    monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: _EXECUTING)

    def _never(_holder: str) -> bool:
        raise AssertionError("tried to release a lease that is actively executing")

    monkeypatch.setattr("shared.cluster_lock.release_settle_hold", _never)
    assert dw.deploy_in_flight().active is True


# ─── never raises ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "broken",
    [
        "shared.cluster_lock.read_update_lease",
        "shared.machines.list_all",
        "shared.machine_exclusions.list_excluded_machines",
        "ops.deploy_window._read_deploy_states",
    ],
)
def test_never_raises_when_a_signal_is_broken(broken: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both callers are refusal/suppression paths: a traceback either blocks every
    deploy or breaks the auto-rollback that catches a bad release."""

    def _raise() -> object:
        raise RuntimeError("db gone")

    monkeypatch.setattr(broken, _raise)
    assert dw.deploy_in_flight().active is False
