"""The deploy window — the one question every pin-moving actor asks.

Two regressions are pinned here, and they pull in opposite directions:

- **The lease must hold while the transitioning host is unreachable.** A runner's
  self-update stops `ops`, the very daemon that answers `status_probe`, so a
  probe-only design reads "no deploy" through the whole stop -> start window: the
  longest, most dangerous part, and the exact scenario of the 2026-07-29 collision.
  `test_lease_holds_even_when_every_host_is_unreachable` is that case.
- **A settle hold must end when the cluster converges**, not when its timer runs
  out — a hold that outlives its condition is the bug class of that whole night
  (the stale `stopped_at` latch, the orphaned launchd probe). That is
  `test_settle_hold_is_released_once_every_host_reaches_the_pin`.
"""

from __future__ import annotations

import pytest

from ops import deploy_window as dw
from shared.cluster_lock import DeployLease, settle_hosts, settle_note

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
