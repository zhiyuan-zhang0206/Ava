"""Rollout robustness — the four defects a live 2026-07-28 rollout exposed.

Each of these is a regression test for something the rollout did silently:
- the fan-out dropped a probe-live host because of a stale `machines.stopped_at`,
  and reported a bare count that hid it;
- the roster showed that same host `online` throughout, so the two sources of
  truth disagreed with nothing to see;
- the updater's recovery branch could only fire on a checkout/sync failure, so a
  failed `ava restart` reached no fallback at all;
- `ava status`'s pin hint accused an in-flight rollout of being a stray `git pull`.

No cluster is required: the `machines` reads and the ops probe are stubbed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import cli.commands._repo as _repo_commands
import cli.commands._start_readiness_preflight as _start_readiness_preflight_commands
import cli.commands.start as _start_commands
import cli.commands.stop as _stop_commands
from shared.exit_codes import RESTART_DECLINED_EXIT_CODE

# ─── Defect 1: the fan-out reconciled against a live probe ───────────────────


# ─── Defect 1 (visibility): the roster names the disagreement ────────────────


def test_roster_flags_a_live_host_that_carries_a_stop_marker() -> None:
    """`online` here is what hid the exclusion: the roster's own source of truth
    (a live probe) contradicted the fan-out's (the marker) with nothing to see."""
    from cli.commands.cluster import _status_cell

    stopped = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    assert _status_cell(online=True, identity_mismatch=False, stopped_at=stopped) == "STALE-STOP"
    # the other three verdicts are unchanged
    assert _status_cell(online=True, identity_mismatch=False, stopped_at=None) == "online"
    assert _status_cell(online=False, identity_mismatch=False, stopped_at=stopped) == "stopped"
    assert _status_cell(online=True, identity_mismatch=True, stopped_at=stopped) == "MISMATCH"


# ─── Defect 3: declined restart vs failed restart ────────────────────────────


def test_declined_restart_reports_its_own_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """A preflight refusal stops nothing, so the host is still serving. It must be
    distinguishable from a failure after the stop — the updater shell branches on
    exactly this code to decide whether to run `ava start`."""
    stopped: list[bool] = []
    monkeypatch.setattr(_repo_commands, "_preflight_probes", lambda: 1)
    monkeypatch.setattr(_stop_commands, "_do_stop", lambda *_a, **_k: stopped.append(True) or 0)  # type: ignore[func-returns-value]
    monkeypatch.setattr(_stop_commands, "_release_self_heal_pause", lambda: None)

    assert _stop_commands.cmd_restart() == RESTART_DECLINED_EXIT_CODE
    assert stopped == []  # validate-before-kill: nothing was taken down


def test_failed_restart_after_the_stop_is_not_reported_as_declined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the stop has happened the host may be DOWN, so its code must NOT be the
    one the updater treats as "still serving"."""
    monkeypatch.setattr(_repo_commands, "_preflight_probes", lambda: 0)
    monkeypatch.setattr(
        _start_readiness_preflight_commands,
        "preflight_start_readiness",
        lambda *_a, **_k: 0,  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_stop_commands, "_do_stop", lambda *_a, **_k: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_start_commands, "_cmd_start_body", lambda **_k: 1)  # pyright: ignore[reportUnknownArgumentType]

    rc = _stop_commands.cmd_restart()
    assert rc != 0
    assert rc != RESTART_DECLINED_EXIT_CODE


def _paused_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `_release_self_heal_pause`'s posture read answer `paused` — the pause
    state it heals (R1 old-signal sweep, PR5: the posture row replaced the
    `cluster_paused` file)."""
    from datetime import datetime

    from shared.host_deploy_state import HostDeployState

    monkeypatch.setattr(
        "shared.host_deploy_state.read",
        lambda *_a, **_k: HostDeployState(  # pyright: ignore[reportUnknownArgumentType]
            machine="test",
            posture="paused",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=None,
            paused_at=datetime.now(UTC),
        ),
    )


def test_declined_restart_releases_a_pause_no_rollout_owns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A locally spawned self-heal pauses this host before running `ava restart`. If
    that restart declines, nothing else clears the pause — so a healthy host would
    sit with its restarter killed until the 10-minute stranded-pause recovery."""
    _paused_posture(monkeypatch)
    monkeypatch.setattr("shared.cluster_lock.update_lock_holder", lambda: None)
    unpaused: list[bool] = []
    monkeypatch.setattr("ops.cluster_pause.unpause_local_cluster", lambda: unpaused.append(True))

    _stop_commands._release_self_heal_pause()
    assert unpaused == [True]


def test_declined_restart_leaves_a_rollouts_pause_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A live update lock means the rollout owns this pause and will resume the host
    itself; unpausing now would let old-code agents respawn mid-migration."""
    _paused_posture(monkeypatch)
    monkeypatch.setattr("shared.cluster_lock.update_lock_holder", lambda: "cloud:pid1")
    unpaused: list[bool] = []
    monkeypatch.setattr("ops.cluster_pause.unpause_local_cluster", lambda: unpaused.append(True))

    _stop_commands._release_self_heal_pause()
    assert unpaused == []


# ─── Defect 4: the pin hint during an in-flight rollout ──────────────────────


def test_pin_hint_does_not_cry_stray_git_pull_during_a_rollout(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Mid-rollout the checkout legitimately runs ahead of a pin that is only
    written once the gateway lands the target. Read live in a rollout log, the
    standing hint reads as an incident."""
    from cli.commands import status as status_mod

    monkeypatch.setattr(status_mod, "_repo_root", lambda: "/repo")
    monkeypatch.setattr(_repo_commands, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(status_mod, "_cluster_pin_status", lambda: ("aaaaaaa", "bbbbbbb"))
    monkeypatch.setattr(status_mod, "prod_source_pin_relation", lambda _p, _h: "ahead")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(status_mod, "_update_in_flight", lambda: True)
    monkeypatch.setattr(status_mod, "_detect_prod_source_drift", lambda: None)
    monkeypatch.setattr(status_mod, "_print_gateway_cluster_status", lambda: None)
    monkeypatch.setattr(status_mod, "print_data_plane_status", lambda: None)
    monkeypatch.setattr(status_mod, "_print_service_row", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]

    assert status_mod.cmd_status() == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "update in progress" in out
    assert "stray" not in out


def test_pin_hint_still_warns_when_no_update_is_running(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Outside a rollout the same state IS a stray `git pull`, and the hint that
    says so must survive."""
    from cli.commands import status as status_mod

    monkeypatch.setattr(status_mod, "_repo_root", lambda: "/repo")
    monkeypatch.setattr(_repo_commands, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(status_mod, "_cluster_pin_status", lambda: ("aaaaaaa", "bbbbbbb"))
    monkeypatch.setattr(status_mod, "prod_source_pin_relation", lambda _p, _h: "ahead")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(status_mod, "_update_in_flight", lambda: False)
    monkeypatch.setattr(status_mod, "_detect_prod_source_drift", lambda: None)
    monkeypatch.setattr(status_mod, "_print_gateway_cluster_status", lambda: None)
    monkeypatch.setattr(status_mod, "print_data_plane_status", lambda: None)
    monkeypatch.setattr(status_mod, "_print_service_row", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]

    assert status_mod.cmd_status() == 0
    assert "stray" in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
