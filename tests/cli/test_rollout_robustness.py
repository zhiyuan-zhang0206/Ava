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

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

import cli.commands as _ns
from cli.commands import _update_orchestration as orch
from shared.exit_codes import RESTART_DECLINED_EXIT_CODE
from shared.platform import IS_WINDOWS

# ─── Defect 1: the fan-out reconciled against a live probe ───────────────────


@pytest.fixture
def fanout(monkeypatch: pytest.MonkeyPatch):
    """Drive `_resolve_fanout_targets` against a fake `machines` table + probe.

    Returns a callable taking (live_rows, stopped_rows, verdicts) and yielding the
    reconciled target list; `cleared` records every stop marker the reconcile wrote
    back."""
    cleared: list[str] = []
    monkeypatch.setattr(
        "shared.machines.clear_stopped_marker",
        lambda name: cleared.append(name) or True,  # pyright: ignore[reportUnknownArgumentType]
    )  # type: ignore[func-returns-value]

    def _drive(
        live: list,
        stopped: list,
        verdicts: dict[str, str],
        *,
        clear_stale_markers: bool = True,
    ) -> tuple[list, list[str]]:
        monkeypatch.setattr(_ns, "_list_agent_runners", lambda: list(live))  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr("shared.machines.list_stopped_agent_runners", lambda: list(stopped))  # pyright: ignore[reportUnknownArgumentType]

        async def _fake_probe(rows: list) -> dict[str, str]:
            return {name: verdicts[name] for name, _url in rows}

        monkeypatch.setattr(orch, "_probe_stopped_agent_runners", _fake_probe)  # pyright: ignore[reportUnknownArgumentType]
        return orch._resolve_fanout_targets(clear_stale_markers=clear_stale_markers), cleared

    return _drive


def test_probe_live_host_is_pulled_back_into_the_rollout(fanout, capsys) -> None:
    """The defect: a host with a stale stop marker was excluded from Phase A and
    Phase B entirely while every dashboard showed it online. The probe — not the
    marker — decides, so an answering host is a rollout target."""
    targets, cleared = fanout(
        [("gateway-host", "http://gateway-host:8600"), ("wsl", "http://wsl:8600")],
        [("laptop-host", "http://laptop-host:8600")],
        {"laptop-host": "live"},
    )
    assert targets == [
        ("gateway-host", "http://gateway-host:8600"),
        ("laptop-host", "http://laptop-host:8600"),
        ("wsl", "http://wsl:8600"),
    ]
    assert cleared == ["laptop-host"]  # and the stale marker is reconciled away
    out = capsys.readouterr()  # pyright: ignore[reportUnknownMemberType]
    combined = out.out + out.err  # pyright: ignore[reportUnknownMemberType]
    assert "3 of 3 registered agent-runner(s)" in combined
    assert "laptop-host" in combined and "stale" in combined


def test_prepare_reconciliation_does_not_clear_a_stale_marker(fanout) -> None:
    """Prepare may protect the rollout set, but it cannot mutate cluster state."""
    targets, cleared = fanout(
        [],
        [("laptop-host", "http://laptop-host:8600")],
        {"laptop-host": "live"},
        clear_stale_markers=False,
    )
    assert targets == [("laptop-host", "http://laptop-host:8600")]
    assert cleared == []


def test_genuinely_stopped_host_stays_out_but_is_named(fanout, capsys) -> None:
    """A host that really is stopped cannot answer its probe: it stays excluded,
    and the count says so out loud rather than shrinking in silence."""
    targets, cleared = fanout(
        [("gateway-host", "http://gateway-host:8600")],
        [("laptop", "http://laptop:8600")],
        {"laptop": "down"},
    )
    assert targets == [("gateway-host", "http://gateway-host:8600")]
    assert cleared == []
    combined = "".join(capsys.readouterr())  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert "1 of 2 registered agent-runner(s)" in combined
    assert "1 skipped" in combined
    assert "laptop" in combined


def test_identity_mismatch_is_not_silently_included(fanout, capsys) -> None:
    """An answer under a different machine_name means this row's dial URL points at
    the wrong host — re-including it would fire an update at a stranger."""
    targets, cleared = fanout(
        [("gateway-host", "http://gateway-host:8600")],
        [("ghost", "http://someone-else:8600")],
        {"ghost": "mismatch"},
    )
    assert targets == [("gateway-host", "http://gateway-host:8600")]
    assert cleared == []
    combined = "".join(capsys.readouterr())  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert "DIFFERENT machine name" in combined


def test_count_is_printed_even_when_nothing_is_excluded(fanout, capsys) -> None:
    """The all-clear case prints the count too — a count only shown on trouble is a
    count nobody learns to read."""
    targets, _ = fanout([("gateway-host", "http://gateway-host:8600")], [], {})
    assert targets == [("gateway-host", "http://gateway-host:8600")]
    assert "1 of 1 registered agent-runner(s)" in "".join(capsys.readouterr())  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]


def test_marker_clear_failure_does_not_abort_the_rollout(
    fanout, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Tidying the roster is best-effort; the host is already back in the rollout,
    which is the part that protects the migration."""

    def _boom(_name: str) -> bool:
        raise RuntimeError("db gone")

    monkeypatch.setattr("shared.machines.clear_stopped_marker", _boom)
    targets, _ = fanout([], [("air", "http://laptop-host:8600")], {"air": "live"})
    assert targets == [("air", "http://laptop-host:8600")]
    assert "could not clear" in "".join(capsys.readouterr())  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]


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
    monkeypatch.setattr(_ns, "_preflight_probes", lambda: 1)
    monkeypatch.setattr(_ns, "_do_stop", lambda *_a, **_k: stopped.append(True) or 0)  # type: ignore[func-returns-value]
    monkeypatch.setattr(_ns, "_release_self_heal_pause", lambda: None)

    assert _ns.cmd_restart() == RESTART_DECLINED_EXIT_CODE
    assert stopped == []  # validate-before-kill: nothing was taken down


def test_failed_restart_after_the_stop_is_not_reported_as_declined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the stop has happened the host may be DOWN, so its code must NOT be the
    one the updater treats as "still serving"."""
    monkeypatch.setattr(_ns, "_preflight_probes", lambda: 0)
    monkeypatch.setattr(_ns, "_do_stop", lambda *_a, **_k: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_ns, "_cmd_start_body", lambda **_k: 1)  # pyright: ignore[reportUnknownArgumentType]

    rc = _ns.cmd_restart()
    assert rc != 0
    assert rc != RESTART_DECLINED_EXIT_CODE


def test_native_updater_ladder_recovers_a_failed_restart_but_not_a_declined_one() -> None:
    """cmd.exe-only now (R1-6): the POSIX ladder retired with the execution-shape
    convergence — its detached session runs the in-process self-update, where the
    decline-vs-failure distinction is the entry's rc. The cmd.exe chain used to be
    `if checkout && sync; then ava restart; else ... ava start; fi` — the `else`
    could only fire on a checkout/sync failure, so the real incident (both
    succeeded, `ava restart` failed) reached no fallback at all."""
    from ops.cluster import _restart_recovery_cmd

    cmd = _restart_recovery_cmd()

    # cmd.exe: `if errorlevel N` is ">= N", so the ladder must be ordered high-to-low.
    assert cmd.index(f"errorlevel {RESTART_DECLINED_EXIT_CODE + 1}") < (
        cmd.index(f"errorlevel {RESTART_DECLINED_EXIT_CODE}")
    )
    assert cmd.index(f"errorlevel {RESTART_DECLINED_EXIT_CODE}") < (cmd.index("errorlevel 1"))


def test_native_ladder_recovery_starts_run_as_internal_child() -> None:
    """The recovery `ava start` arms run as INTERNAL child starts: a Phase-B
    updater runs under the cluster-wide executing deploy lease, and an
    operator-mode start is refused by the rollout boundary — the 2026-09-02 win
    shape (a restart refused by a co-located unit's health port fell into the
    recovery arm; the arm's operator-mode start was refused by the lease; the
    updater exited rc=1 before its services started). `--persist-services` is
    the cmd.exe ladder's internal-child marker, the same posture the POSIX
    in-process updater's internal start already has."""
    from ops.cluster import _restart_recovery_cmd

    cmd = _restart_recovery_cmd()
    # Both recovery arms (errorlevel >3 and 1..2) start internally; the plain
    # `ava restart` keeps its operator rc semantics (it declines, never refuses).
    assert cmd.count("ava start --persist-services") == 2
    assert "ava restart --persist-services" not in cmd
    # No operator-mode `ava start` remains anywhere in the ladder.
    assert "(ava start &" not in cmd


@pytest.mark.skipif(IS_WINDOWS, reason="runs the POSIX wrapper fragment through /bin/sh")
@pytest.mark.parametrize("entry_rc", [0, 1, RESTART_DECLINED_EXIT_CODE, 7])
def test_posix_updater_wrapper_preserves_the_entry_rc(entry_rc: int, tmp_path: Path) -> None:
    """R1-6: the POSIX updater session runs the in-process entry, so the shell
    ladder's branching is gone — but the wrapper spawn_update builds must still
    report the entry's verdict on `[session-exit] rc=`, not the trailing echo's 0
    (`ops.updater_outcome` reads exactly that line; a decline travels as
    `rc == RESTART_DECLINED_EXIT_CODE`). Run the wrapper shape through /bin/sh
    against a fake `python -m` entry that exits with the given rc."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    runner = fake_bin / "python"
    runner.write_text(f'#!/bin/sh\necho "FAKE entry $*"\nexit {entry_rc}\n')
    runner.chmod(0o755)

    # The wrapper spawn_update emits for the restart-only chain, with the venv
    # activation replaced by the fake bin on PATH (the `python` resolution is the
    # only thing venv_activation_prefix provides the real command).
    wrapper = (
        f"if cd {tmp_path}; then python -m cli.commands._update_agent_runner "
        f"--restart-only --mode smooth; rc=$?; "
        f"else rc=$?; echo '[updater] cannot enter the repo; nothing to bounce'; fi; "
        'echo "[session-exit] rc=$rc"'
    )
    result = subprocess.run(  # noqa: S603 — /bin/sh with a fragment this repo generates
        ["/bin/sh", "-c", wrapper],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        check=False,
    )
    assert "FAKE entry" in result.stdout
    # the session-exit line reports the entry's verdict, not the trailing echo's 0
    assert f"[session-exit] rc={entry_rc}" in result.stdout


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
    monkeypatch.setattr("ops.cluster.unpause_local_cluster", lambda: unpaused.append(True))

    _ns._release_self_heal_pause()
    assert unpaused == [True]


def test_declined_restart_leaves_a_rollouts_pause_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A live update lock means the rollout owns this pause and will resume the host
    itself; unpausing now would let old-code agents respawn mid-migration."""
    _paused_posture(monkeypatch)
    monkeypatch.setattr("shared.cluster_lock.update_lock_holder", lambda: "cloud:pid1")
    unpaused: list[bool] = []
    monkeypatch.setattr("ops.cluster.unpause_local_cluster", lambda: unpaused.append(True))

    _ns._release_self_heal_pause()
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
    monkeypatch.setattr(_ns, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(status_mod, "_cluster_pin_status", lambda: ("aaaaaaa", "bbbbbbb"))
    monkeypatch.setattr(status_mod, "prod_source_pin_relation", lambda _p, _h: "ahead")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(status_mod, "_update_in_flight", lambda: True)
    monkeypatch.setattr(status_mod, "_detect_prod_source_drift", lambda: None)
    monkeypatch.setattr(status_mod, "_print_gateway_cluster_status", lambda: None)
    monkeypatch.setattr(status_mod, "print_data_plane_status", lambda: None)
    monkeypatch.setattr(status_mod, "_print_service_row", lambda *_a: None)  # pyright: ignore[reportUnknownArgumentType]

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
    monkeypatch.setattr(_ns, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(status_mod, "_cluster_pin_status", lambda: ("aaaaaaa", "bbbbbbb"))
    monkeypatch.setattr(status_mod, "prod_source_pin_relation", lambda _p, _h: "ahead")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(status_mod, "_update_in_flight", lambda: False)
    monkeypatch.setattr(status_mod, "_detect_prod_source_drift", lambda: None)
    monkeypatch.setattr(status_mod, "_print_gateway_cluster_status", lambda: None)
    monkeypatch.setattr(status_mod, "print_data_plane_status", lambda: None)
    monkeypatch.setattr(status_mod, "_print_service_row", lambda *_a: None)  # pyright: ignore[reportUnknownArgumentType]

    assert status_mod.cmd_status() == 0
    assert "stray" in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
