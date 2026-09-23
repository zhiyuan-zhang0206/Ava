"""Update stall verdicts and rendering; split from tests/cli/test_commands.py (task #4554)."""

from __future__ import annotations

import re

import pytest

from cli import commands as _cli
from cli.commands.update import _poll_verdict_detail
from tests.cli._commands_helpers import _fake_session_backends as _fake_session_backends
from tests.cli._commands_helpers import _hermetic_gateway_base as _hermetic_gateway_base
from tests.cli._commands_helpers import _noop_start_prechecks as _noop_start_prechecks


def test_phase_b_deadline_contract_matches_the_timing_invariants() -> None:
    """The public 900-second absolute deadline and C3's 300-second handoff are
    different clocks with one authoritative definition each."""
    from shared.deploy_timing import (
        CONVERGING_POLL_TIMEOUT_S,
        NO_PROGRESS_TIMEOUT_S,
        PHASE_B_ABSOLUTE_TIMEOUT_S,
    )

    assert _cli._POLL_TIMEOUT_S == PHASE_B_ABSOLUTE_TIMEOUT_S == NO_PROGRESS_TIMEOUT_S == 900.0
    assert _cli._CONVERGING_TIMEOUT_S == CONVERGING_POLL_TIMEOUT_S == 300.0
    assert CONVERGING_POLL_TIMEOUT_S < PHASE_B_ABSOLUTE_TIMEOUT_S


def test_a_restart_resets_the_converging_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """C3's patience is the CONTINUOUS progress streak, not total poll time: an
    unreachable reading (the expected mid-restart silence) must reset the clock,
    or a host that restarts midway through its leg would be handed to settle on
    stale progress that predates the restart."""
    from datetime import UTC, datetime, timedelta

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    probes = {"n": 0}

    async def _stop_start(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        probes["n"] += 1
        # One unreachable reading (the mid-restart silence) after two
        # progressing ones: the streak must restart from the silence.
        if probes["n"] == 3:
            raise cr.ClusterOpUnreachable("restarting")
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

    monkeypatch.setattr(cr, "dispatch_to_machine", _stop_start)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 3600.0)
    monkeypatch.setattr(_cli, "_CONVERGING_TIMEOUT_S", 0.05)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(_cli, "_STAGE_NO_PROGRESS_S", 600.0)

    out = _cli._poll_until_unpaused([("wsl", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"wsl": _cli.POLL_CONVERGING}
    # Without the reset, ~5-6 probes (one 0.05 s streak) would end the poll; the
    # reset at probe 3 forces the second streak to restart, so the poll needs
    # meaningfully more probes before the bound accumulates. The bound is set at
    # 7 with headroom: a slow CI box stretches the sleep intervals and shrinks
    # the probe count (QA nit, PR #1200) — 6 would make the assertion flaky.
    assert probes["n"] >= 7


def test_probe_verdict_names_the_progress_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    """`progressing` is True only for the one shape the converging bound exists
    for — a live lease with stage evidence that is not stuck. A paused pre-lease
    window, a stuck stage's first reading, and an idle posture are not progress."""
    from datetime import UTC, datetime, timedelta

    from cli.commands._update_phase_b import _probe_verdict
    from shared.host_deploy_state import HostDeployState

    def _row(posture: str, lease: bool, stage_age: float | None):
        def _read(machine=None, **_kw):
            return HostDeployState(
                machine=machine or "wsl",  # pyright: ignore[reportUnknownArgumentType]
                posture=posture,
                updated_at=datetime.now(UTC),
                updater_lease_expires_at=(
                    datetime.now(UTC) + timedelta(seconds=800) if lease else None
                ),
            )

        monkeypatch.setattr("cli.commands._update_phase_b.read", _read)  # pyright: ignore[reportUnknownArgumentType]
        probe: dict[str, object] = {"paused": False, "head_sha": "a" * 40, "running_sha": "a" * 40}
        if stage_age is not None:
            probe["last_updater_outcome"] = {
                "kind": "unknown",
                "rc": None,
                "log": "x.log",
                "current_stage": "uv",
                "current_stage_s": stage_age,
            }
        return _probe_verdict(probe, 0, "wsl", 0)

    monkeypatch.setattr(_cli, "_STAGE_NO_PROGRESS_S", 600.0)
    # A live lease with a young stage is the progressing shape.
    _v, _s, _n, progressing = _row("converging", True, 100.0)
    assert _v is None and progressing is True
    # A live lease with NO stage fields ("cannot tell", older commit) is not
    # progress — it resets the streak like the no-progress rule's polarity.
    _v, _s, _n, progressing = _row("converging", True, None)
    assert _v is None and progressing is False
    # A live lease with a stuck stage starts the no-progress streak instead.
    _v, _s, _n, progressing = _row("converging", True, 700.0)
    assert _v is None and _n == 1 and progressing is False
    # A paused pre-lease window (no lease yet) is "cannot tell", not progress.
    _v, _s, _n, progressing = _row("paused", False, None)
    assert _v is None and progressing is False
    # Ready on-code idle is convergence, not the progressing shape.
    _v, _s, _n, progressing = _row("idle", False, None)
    assert _v is not None and _v.status == _cli.POLL_OK and progressing is False


def test_paused_without_lease_stalls_once_the_arm_grace_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-09-02 win: the updater's recovery `ava start` exited rc=1 under the
    executing deploy lease, its lease clear ran, and the paused-no-lease reading
    kept the poll "cannot tell" for the whole 900 s bound. Once THIS host's poll
    has run past the lease-arm grace, a paused host with no live lease is
    provably not running an updater — a stall candidate like any other provable
    stop. The grace is measured from the poll's own clock, never `paused_at`:
    the pause (Phase A) and the updater spawn (Phase B trigger) are minutes
    apart by design."""
    from datetime import UTC, datetime, timedelta

    from cli.commands._update_phase_b import _probe_verdict
    from shared.host_deploy_state import HostDeployState

    def _read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "win",  # pyright: ignore[reportUnknownArgumentType]
            posture="paused",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=None,
            paused_at=datetime.now(UTC) - timedelta(minutes=10),
        )

    monkeypatch.setattr("cli.commands._update_phase_b.read", _read)  # pyright: ignore[reportUnknownArgumentType]
    # Inside the arm grace: still "cannot tell"; neither counter advances.
    verdict, stalls, no_progress, progressing = _probe_verdict({}, 0, "win", 0, poll_elapsed=10.0)
    assert verdict is None and stalls == 0 and no_progress == 0
    assert progressing is False
    # Past the grace: the first stalled reading, then two confirmations end it.
    verdict, stalls, no_progress, _ = _probe_verdict({}, 0, "win", 0, poll_elapsed=91.0)
    assert verdict is None and stalls == 1
    verdict, stalls, no_progress, _ = _probe_verdict({}, 1, "win", 0, poll_elapsed=93.0)
    assert verdict is not None and verdict.status == _cli.POLL_STALLED


def test_a_probe_from_an_older_commit_never_reads_as_no_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner answering from a commit that predates the stage fields reports no
    `current_stage` — cannot tell, never progress, so the poll keeps going to its
    deadline. The judgment applies from the rollout after the one that ships it,
    the same one-rollout lag `last_updater_outcome` itself had."""
    from datetime import UTC, datetime, timedelta

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    async def _older_commit(
        *, target_machine, kind, payload, timeout_s, ops_url=None, retries=None
    ):
        return {"last_updater_outcome": {"kind": "unknown", "rc": None, "log": "x.log"}}

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "win",  # pyright: ignore[reportUnknownArgumentType]
            posture="converging",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=datetime.now(UTC) + timedelta(seconds=800),
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _older_commit)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(_cli, "_STAGE_NO_PROGRESS_S", 600.0)

    out = _cli._poll_until_unpaused([("win", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"win": _cli.POLL_CONVERGING}


def test_no_progress_verdict_renders_its_own_next_step() -> None:
    """POLL_NO_PROGRESS is a different fact from CONVERGING and STALLED — the host
    is alive and stuck, and the operator's next move is to look at that machine's
    network, not to wait or to restart it."""
    verdict = _cli.PollVerdict(
        _cli.POLL_NO_PROGRESS,
        {"kind": "unknown", "current_stage": "uv", "current_stage_s": 700.0, "log": "x.log"},
    )
    detail = _poll_verdict_detail(verdict)
    assert "NO PROGRESS" in detail
    assert "uv" in detail
    assert "network" in detail


def test_db_read_failure_keeps_polling_never_reports_converged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB hiccup on the deploy-state row is 'cannot tell', never convergence:
    the old code collapsed the read failure into `state=None` and returned
    POLL_OK, which would release the deploy lease while the host is still
    mid-transition. The poll must keep going to its deadline instead."""
    from ops import cluster_rpc as cr

    async def _reachable(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        return {}

    def _broken_read(machine=None, **_kw):
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(cr, "dispatch_to_machine", _reachable)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _broken_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    out = _cli._poll_until_unpaused([("air", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"air": _cli.POLL_CONVERGING}


def test_db_read_failure_is_not_a_stall_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stall counter must not advance on a read failure either: an
    unreadable row is evidence-free, and two 'cannot tell' readings are not a
    provable stop."""
    from cli.commands._update_phase_b import _probe_verdict

    def _broken_read(machine=None, **_kw):
        raise RuntimeError("db unreachable")

    monkeypatch.setattr("cli.commands._update_phase_b.read", _broken_read)  # pyright: ignore[reportUnknownArgumentType]

    verdict, stalls, no_progress, progressing = _probe_verdict({}, 3, "air", 2)
    assert verdict is None
    assert stalls == 3  # counter untouched — a read failure is not a stall observation
    assert no_progress == 2  # same rule: a read failure is not a no-progress observation
    assert progressing is False  # and it is not progress either (C3): the converging
    # clock must reset on evidence-free readings, or a DB hiccup would keep it running


def test_a_stalled_host_carries_its_own_updater_outcome_off_the_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason rides the same probe that settles the verdict, so it costs no extra
    dial: the response that proves the host stopped is the response that says why."""
    from datetime import UTC, datetime

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    async def _declined(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        return {
            "last_updater_outcome": {
                "kind": "declined",
                "rc": 3,
                "detail": "✗ gateway unreachable at http://gw:8000",
                "log": "updater-1785470000.log",
            },
        }

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "air",  # pyright: ignore[reportUnknownArgumentType]
            posture="converging",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=None,
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _declined)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 3600.0)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    verdict = _cli._poll_until_unpaused([("air", "http://unused")])["air"]

    assert verdict.status == _cli.POLL_STALLED
    assert verdict.updater is not None
    assert verdict.updater["kind"] == "declined"


def test_a_runner_that_never_sent_an_outcome_reports_no_record_not_a_clean_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host on a commit that predates the field sends no `last_updater_outcome` at
    all — the same silence as a host whose log did not speak for this update. Both
    are 'no record', which the report says outright rather than inventing rc=0."""
    from datetime import UTC, datetime

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    async def _older_commit(
        *, target_machine, kind, payload, timeout_s, ops_url=None, retries=None
    ):
        return {}

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "old",  # pyright: ignore[reportUnknownArgumentType]
            posture="converging",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=None,
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _older_commit)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 3600.0)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    verdict = _cli._poll_until_unpaused([("old", "http://unused")])["old"]

    assert verdict.status == _cli.POLL_STALLED
    assert verdict.updater is None
    assert "no updater record" in _poll_verdict_detail(verdict)


def test_two_stalled_hosts_no_longer_read_identically(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect, stated as a test. A refusal and a death both produce
    `POLL_STALLED`, and the report used to print one sentence for both — so the
    operator's only next step was to ssh in and read a log, on the platform where
    that is hardest and for the case where nothing is actually broken."""
    declined = _cli.PollVerdict(
        _cli.POLL_STALLED,
        {"kind": "declined", "rc": 3, "detail": "✗ gateway unreachable at http://gw:8000"},
    )
    died = _cli.PollVerdict(_cli.POLL_STALLED, {"kind": "unknown", "log": "updater-178.log"})

    declined_line = _poll_verdict_detail(declined)
    died_line = _poll_verdict_detail(died)

    assert declined_line != died_line
    # the refusal says the host is intact and names what the preflight complained of
    assert "still serving its old code" in declined_line
    assert "gateway unreachable at http://gw:8000" in declined_line
    # the death says the opposite thing about the host's state
    assert "still serving its old code" not in died_line
    assert "died mid-flight" in died_line


def test_a_refusal_is_not_told_to_wait_for_its_watchdog() -> None:
    """ "Its watchdog re-triggers the self-update" is true of a death and wrong of a
    refusal — and wrong in the expensive direction, because it reads as "wait" for the
    one case waiting does not clear. A declined host is still paused (rc=3 skips the
    `ava start` that unlinks the flag), `PauseController` blocks the tick ahead of
    `PinController`, and the off-pin heal it blocks converges by POSTing the very
    gateway the preflight refused over."""
    declined = _cli.PollVerdict(_cli.POLL_STALLED, {"kind": "declined", "rc": 3})
    died = _cli.PollVerdict(_cli.POLL_STALLED, {"kind": "exited", "rc": 1})

    declined_line = _poll_verdict_detail(declined)
    died_line = _poll_verdict_detail(died)

    assert "will NOT self-heal" in declined_line
    assert "watchdog re-triggers the self-update" not in declined_line
    # the operator gets something to do instead of something to wait for
    assert "re-run the update" in declined_line
    # a death is still the case the watchdog does handle
    assert "watchdog re-triggers the self-update" in died_line


def test_a_death_is_told_what_has_to_happen_before_its_watchdog_can_act() -> None:
    """Issue #1114. The death branch promised a watchdog re-trigger with no precondition
    — to a host this same poll has just classified as *still paused*. The pause gate
    that holds a refusal's heal back holds a death's heal back identically
    (`PauseController` blocks the whole tick ahead of `PinController`), so the verdict
    was not wrong so much as missing the step it depends on: something has to lift the
    pause first, and in this rollout that is the compensating resume `finalize_rollout`
    is about to fan out."""
    died = _cli.PollVerdict(_cli.POLL_STALLED, {"kind": "exited", "rc": 1})
    line = _poll_verdict_detail(died)

    assert "watchdog re-triggers the self-update" in line
    assert "once the pause is lifted" in line
    assert "compensating resume" in line
    # no duration: the flag stands until nothing owns the pause, and this rollout is
    # about to hold the lease over exactly these hosts.
    assert not re.search(r"\d+\s*(m|min|s|sec)\b", line), line


def test_no_stalled_verdict_quotes_a_recovery_deadline() -> None:
    """Both branches, one rule. A number here would be read as "wait that long", which
    is a promise neither the refusal (waiting never clears it) nor the death (the bound
    depends on who owns the pause) can keep."""
    for updater in (
        {"kind": "declined", "rc": 3},
        {"kind": "exited", "rc": 1},
        {"kind": "unknown", "log": "updater-178.log"},
    ):
        line = _poll_verdict_detail(_cli.PollVerdict(_cli.POLL_STALLED, updater))
        assert not re.search(r"\bin \d+\s*(m|min|s|sec)\b", line), line


def test_poll_stall_verdict_needs_consecutive_confirmations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One `converging`-with-no-lease reading between two live-lease ones is a race,
    not a stall: the counter resets, so the host is never abandoned on a single
    sample."""
    from datetime import UTC, datetime, timedelta

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    readings = ["stuck", "live", "stuck", "live"]
    seen = {"n": 0}

    async def _reachable(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        return {}

    def _fake_read(machine=None, **_kw):
        idx = min(seen["n"], len(readings) - 1)
        seen["n"] += 1
        if readings[idx] == "live":
            lease = datetime.now(UTC) + timedelta(seconds=60)
            posture = "converging"
        else:
            lease = None
            posture = "converging"
        return HostDeployState(
            machine=machine or "win",  # pyright: ignore[reportUnknownArgumentType]
            posture=posture,
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=lease,
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _reachable)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 0.08)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    out = _cli._poll_until_unpaused([("win", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"win": _cli.POLL_CONVERGING}


def test_poll_paused_before_the_first_lease_touch_is_never_a_stall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paused with NO updater lease is the fan-out window (spawn -> checkout ->
    uv sync, the updater's first touch lands after them) and every legacy
    pre-lease chain on the first rollout that ships this. Both read "cannot
    tell", never a stall — abandoning a host on this window is the too-eager
    verdict this rewrite exists to remove."""
    from datetime import UTC, datetime

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    async def _reachable(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        return {}

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "old",  # pyright: ignore[reportUnknownArgumentType]
            posture="paused",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=None,
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _reachable)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    out = _cli._poll_until_unpaused([("old", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"old": _cli.POLL_CONVERGING}


def test_poll_unreachable_host_is_never_a_stall(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ops` is itself a service its own self-update stops, so silence is the expected
    reading through the middle of a healthy leg — the longest stretch of it on a
    Windows host. It must never become a verdict."""
    from ops import cluster_rpc as cr

    async def _unreachable(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        raise cr.ClusterOpUnreachable("ops down mid-restart")

    monkeypatch.setattr(cr, "dispatch_to_machine", _unreachable)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    out = _cli._poll_until_unpaused([("win", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"win": _cli.POLL_CONVERGING}
