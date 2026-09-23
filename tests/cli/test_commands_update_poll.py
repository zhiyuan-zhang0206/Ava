"""Update polling and convergence decisions; split from tests/cli/test_commands.py (task #4554)."""

from __future__ import annotations

import pytest

from cli import commands as _cli
from tests.cli._commands_helpers import _fake_session_backends as _fake_session_backends
from tests.cli._commands_helpers import _hermetic_gateway_base as _hermetic_gateway_base
from tests.cli._commands_helpers import _noop_start_prechecks as _noop_start_prechecks


def test_poll_until_unpaused_returns_ok_when_agent_runner_unpauses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ready process on the checkout still waits for its DB posture to become idle."""
    from datetime import UTC, datetime

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    calls = {"wsl": 0}

    async def _reachable(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        assert kind == "status_probe"
        assert payload == {}
        assert retries == 0  # the outer Phase-B loop is the only retry policy
        # the poll threads the pre-resolved ops URL so it never re-queries Postgres
        assert ops_url == "http://unused"
        calls[target_machine] += 1
        return {"paused": False, "head_sha": "a" * 40, "running_sha": "a" * 40}

    def _fake_read(machine=None, **_kw):
        posture = "paused" if calls["wsl"] < 2 else "idle"
        return HostDeployState(
            machine=machine or "wsl",  # pyright: ignore[reportUnknownArgumentType]
            posture=posture,
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=None,
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _reachable)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 5.0)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    out = _cli._poll_until_unpaused([("wsl", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"wsl": "ok"}
    assert calls["wsl"] >= 2


def test_poll_until_unpaused_marks_converging_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent-runner whose updater lease stays live until the deadline is
    POLL_CONVERGING — 'the poll ran out of patience', not 'this host stopped'.
    The live lease is the evidence it is still working (R1, Task #1021)."""
    from datetime import UTC, datetime, timedelta

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    async def _reachable(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        return {}

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "wsl",  # pyright: ignore[reportUnknownArgumentType]
            posture="paused",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _reachable)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    out = _cli._poll_until_unpaused([("wsl", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"wsl": _cli.POLL_CONVERGING}


def test_poll_gives_up_at_once_on_a_host_that_provably_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host whose deploy-state row says `converging` with NO live updater lease
    has lost its updater without resuming (the declined-restart shape: the chain
    touched the lease, the restart refused, and the host never returned to idle).
    More waiting cannot help. The poll returns POLL_STALLED within a couple of
    intervals instead of burning the whole bound — which is what makes a bound
    long enough for a Windows leg affordable. Measured on prod: three
    consecutive rollouts spent the full poll on a host whose updater had exited
    in 3s. R1 (Task #1021): the row is the verdict.
    """
    from datetime import UTC, datetime

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    probes = {"n": 0}

    async def _reachable(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        probes["n"] += 1
        return {}

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "air",  # pyright: ignore[reportUnknownArgumentType]
            posture="converging",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=None,
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _reachable)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    # A bound this generous would take ~an hour to reach; the assertion is that the
    # verdict does NOT wait for it.
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 3600.0)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    out = _cli._poll_until_unpaused([("air", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"air": _cli.POLL_STALLED}
    # Two consecutive confirmations, not one: a single contrary reading is a
    # spawn/teardown race, not evidence.
    assert probes["n"] == 2


def test_a_previous_updates_uncleared_lease_is_not_this_ones_stall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The false positive behind two of prod's `win: STALLED` rounds. The pause does
    not clear the lease column, so a run that ended without clearing leaves its expiry
    in the row; the next update's pause then sits in front of it, and for the seconds
    between `pause_local_cluster` and the updater's first touch — a session spawn plus
    a Python cold start, which on a Windows host is the slow part — the row reads
    exactly like a host whose updater died. Two probes later Phase B abandoned a host
    that went on to converge minutes afterwards, and held the cluster for a settle
    window waiting on it."""
    from datetime import UTC, datetime, timedelta

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import UPDATER_LEASE_TTL_S, HostDeployState

    async def _reachable(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        return {}

    now = datetime.now(UTC)
    expired = now - timedelta(seconds=60)

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "win",  # pyright: ignore[reportUnknownArgumentType]
            posture="paused",
            updated_at=now,
            # Armed by the PREVIOUS update — a whole TTL before it expired, which is
            # before this pause window opened.
            updater_lease_expires_at=expired,
            paused_at=expired - timedelta(seconds=UPDATER_LEASE_TTL_S) + timedelta(seconds=1),
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _reachable)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    out = _cli._poll_until_unpaused([("win", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"win": _cli.POLL_CONVERGING}


def test_poll_gives_up_on_a_written_verdict_the_stale_lease_contradicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lease is one write at the run's start, armed for the same 15 minutes this
    poll is willing to wait — so every way of never reaching its clear step (a cmd.exe
    chain that `exit /b`s first, a clear that cannot reach the DB through the restart
    it is part of, a killed process) leaves a host that stopped in seconds claiming to
    be busy for the whole bound. Its own written ending outranks the claim: the updater
    said it finished, and the posture row says it did not converge."""
    from datetime import UTC, datetime, timedelta

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    probes = {"n": 0}

    async def _reachable(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        probes["n"] += 1
        return {
            "last_updater_outcome": {
                "kind": "exited",
                "rc": 1,
                "detail": "[updater] checkout/sync or tree verification FAILED",
                "log": "ava-updater.out.log",
            },
        }

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "win",  # pyright: ignore[reportUnknownArgumentType]
            posture="converging",
            updated_at=datetime.now(UTC),
            # The lease the abort never cleared: still minutes from expiring.
            updater_lease_expires_at=datetime.now(UTC) + timedelta(seconds=800),
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _reachable)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 3600.0)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    out = _cli._poll_until_unpaused([("win", "http://unused")])

    assert {n: v.status for n, v in out.items()} == {"win": _cli.POLL_STALLED}
    assert probes["n"] == 2  # the same two confirmations, not one
    assert out["win"].updater is not None
    assert out["win"].updater["rc"] == 1


def test_a_live_lease_with_no_written_ending_still_keeps_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard on the short-cut above, and the reason it is keyed on a *terminal*
    outcome rather than on there being one at all: a host mid-`uv sync` has a log and
    a live lease and is working. `unknown` is what its log says, and abandoning it
    would trade a slow rollout for a broken host."""
    from datetime import UTC, datetime, timedelta

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    async def _reachable(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        return {"last_updater_outcome": {"kind": "unknown", "rc": None, "log": "x.log"}}

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "win",  # pyright: ignore[reportUnknownArgumentType]
            posture="converging",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=datetime.now(UTC) + timedelta(seconds=800),
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _reachable)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    out = _cli._poll_until_unpaused([("win", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"win": _cli.POLL_CONVERGING}


def test_a_live_lease_with_stuck_stage_evidence_is_no_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The P1 (2026-08-30) shape: the updater lease is one write at the run's start,
    so a host hung inside `uv` (a stalled network download on the Windows runner)
    used to read "still working" for the whole 900 s bound while its own stage
    evidence showed nothing completing. Two consecutive probes naming the same
    stage in flight beyond STAGE_NO_PROGRESS_TIMEOUT_S end the poll with
    POLL_NO_PROGRESS — the lease stays live, but the progress fact outranks the
    claim."""
    from datetime import UTC, datetime, timedelta

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    probes = {"n": 0}

    async def _stuck(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        probes["n"] += 1
        return {
            "last_updater_outcome": {
                "kind": "unknown",
                "rc": None,
                "log": "ava-updater.out.log",
                "current_stage": "uv",
                "current_stage_s": 700.0,
            },
        }

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "win",  # pyright: ignore[reportUnknownArgumentType]
            posture="converging",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=datetime.now(UTC) + timedelta(seconds=800),
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _stuck)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 3600.0)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(_cli, "_STAGE_NO_PROGRESS_S", 600.0)

    out = _cli._poll_until_unpaused([("win", "http://unused")])

    assert {n: v.status for n, v in out.items()} == {"win": _cli.POLL_NO_PROGRESS}
    assert probes["n"] == 2  # the same two confirmations, not one
    assert out["win"].updater is not None
    assert out["win"].updater["current_stage"] == "uv"


def test_other_hosts_converged_while_one_is_stuck_in_a_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The poll is per host: a stuck Windows box must not drag the hosts that
    converged. The two healthy hosts return ok at once; the stuck one returns
    no_progress as soon as its evidence proves it — the poll does not wait out the
    whole bound for it, and the caller's settle hold covers exactly the stuck
    host."""
    from datetime import UTC, datetime, timedelta

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    calls = {"air": 0, "mini": 0, "win": 0}

    async def _probe(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        calls[target_machine] += 1
        if target_machine == "win":
            return {
                "last_updater_outcome": {
                    "kind": "unknown",
                    "rc": None,
                    "log": "ava-updater.out.log",
                    "current_stage": "uv",
                    "current_stage_s": 700.0,
                },
            }
        return {"paused": False, "head_sha": "a" * 40, "running_sha": "a" * 40}

    def _fake_read(machine=None, **_kw):
        if machine == "win":
            posture = "converging"
            lease = datetime.now(UTC) + timedelta(seconds=800)
        else:
            posture = "idle"
            lease = None
        return HostDeployState(
            machine=machine or "air",  # pyright: ignore[reportUnknownArgumentType]
            posture=posture,
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=lease,
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _probe)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 3600.0)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(_cli, "_STAGE_NO_PROGRESS_S", 600.0)

    out = _cli._poll_until_unpaused(
        [("air", "http://unused"), ("mini", "http://unused"), ("win", "http://unused")]
    )

    assert {n: v.status for n, v in out.items()} == {
        "air": _cli.POLL_OK,
        "mini": _cli.POLL_OK,
        "win": _cli.POLL_NO_PROGRESS,
    }
    # The stuck host ends the poll on its own verdict; the converged hosts needed
    # exactly one probe each and the stuck one its two confirmations.
    assert calls["air"] == 1
    assert calls["mini"] == 1
    assert calls["win"] == 2


def test_stage_evidence_that_advances_keeps_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stage in flight BELOW the bound, or a stage that changes between probes
    (progress), is working — the no-progress streak must not fire. A slow Windows
    uv leg is exactly this shape: the same stage name, an age that keeps growing,
    and the poll's own deadline remains the patience."""
    from datetime import UTC, datetime, timedelta

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "win",  # pyright: ignore[reportUnknownArgumentType]
            posture="converging",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=datetime.now(UTC) + timedelta(seconds=800),
        )

    async def _young_stage(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        return {
            "last_updater_outcome": {
                "kind": "unknown",
                "rc": None,
                "log": "updater-178.log",
                "current_stage": "uv",
                "current_stage_s": 100.0,
            },
        }

    monkeypatch.setattr(cr, "dispatch_to_machine", _young_stage)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(_cli, "_STAGE_NO_PROGRESS_S", 600.0)

    out = _cli._poll_until_unpaused([("win", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"win": _cli.POLL_CONVERGING}


def test_continuous_progress_past_the_converging_bound_hands_the_host_to_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C3: a host that is ALIVE AND MAKING PROGRESS must not burn the whole 900 s
    poll bound — 340 probes on 2026-08-30's wsl runner — when the settle hold +
    watchdog already cover its remaining convergence. Continuous progress past
    `_CONVERGING_TIMEOUT_S` ends the poll early with POLL_CONVERGING (the same
    verdict the deadline produces, so the settle set / report / resume need no
    branching)."""
    from datetime import UTC, datetime, timedelta

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    probes = {"n": 0}

    async def _young_stage(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        probes["n"] += 1
        return {
            "last_updater_outcome": {
                "kind": "unknown",
                "rc": None,
                "log": "updater-178.log",
                "current_stage": "uv",
                "current_stage_s": 100.0,
            },
        }

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "wsl",  # pyright: ignore[reportUnknownArgumentType]
            posture="converging",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=datetime.now(UTC) + timedelta(seconds=800),
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _young_stage)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    # A generous absolute deadline, so the ONLY thing that can end the poll is the
    # converging bound — the assertion is that the poll does NOT wait it out.
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 3600.0)
    monkeypatch.setattr(_cli, "_CONVERGING_TIMEOUT_S", 0.05)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(_cli, "_STAGE_NO_PROGRESS_S", 600.0)

    out = _cli._poll_until_unpaused([("wsl", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"wsl": _cli.POLL_CONVERGING}
    # ~6 probes at 0.01 s intervals reach the 0.05 s bound; the early exit must
    # not spend anything like the 3600 s deadline. (Tight bounds would flake on
    # monotonic drift, so assert the order of magnitude.)
    assert probes["n"] < 50
