"""`cluster_cancel_op` — the formal cancel: SIGINT, never a kill, never a guess.

The P1 (2026-08-30) cancel path. A rollout being dragged by one stuck host used to
leave an operator with hand-kills as the only way out — and a killed orchestration
cannot run its own recovery, so the cluster sat paused until the lease lapsed. The
op interrupts the orchestration's own pid instead: its `finally` resumes every
paused host, releases or settles the deploy lease and clears the durable
maintenance marker. These tests pin the refusals to *process liveness* — a cancel
must only ever signal a provably live local holder, and must refuse with its own
next step on every other reading.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path
from typing import Literal, cast

import pytest

import ops.ops_cluster as _ops
from ops.cluster import ClusterUpdateInProgress
from shared.cluster_lock import DeployLease

_Kind = Literal["rollout", "restart", "update"]


def _lease(
    holder: str,
    *,
    kind: _Kind | None = "rollout",
    note: str | None = None,
) -> DeployLease:
    return DeployLease(holder=holder, held_for_s=0.0, expires_in_s=600.0, note=note, kind=kind)


@pytest.fixture
def cancel_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Stub the collaborators; `signals` records what the op delivered."""
    signals: list[tuple[int, int]] = []
    env: dict[str, object] = {"signals": signals, "session": "rollout"}
    # holder_pid_if_local (and the op's own refusal text) resolve machine_name at
    # call time from shared.machine; the test host is "m1".
    monkeypatch.setattr("shared.machine.machine_name", lambda: "m1")

    def _live() -> str | None:
        from shared.cluster import session_name

        return session_name(env["session"]) if env["session"] else None  # type: ignore[arg-type]

    monkeypatch.setattr(_ops.cluster_session, "live_orchestration_session", _live)

    def _kill(pid: int, sig: int) -> None:
        env["signals"].append((pid, sig))  # type: ignore[index]

    monkeypatch.setattr(_ops.os, "kill", _kill)
    return env


def _set_lease(monkeypatch: pytest.MonkeyPatch, lease: DeployLease | None) -> None:
    monkeypatch.setattr(_ops, "read_update_lease", lambda: lease)


def test_cancel_sigints_the_live_local_holder(
    monkeypatch: pytest.MonkeyPatch, cancel_env: dict[str, object]
) -> None:
    """The happy path: a running rollout whose holder names this machine and a live
    pid (our own) gets SIGINT — the signal whose KeyboardInterrupt makes the
    orchestration run its own recovery. Nothing else is touched: no kill, no lock
    write — the finally does the work."""
    holder = f"m1:pid{os.getpid()}"
    _set_lease(monkeypatch, _lease(holder))

    result = _ops.cluster_cancel_op()

    assert result == {"cancelled": holder}
    # The pid-liveness probe also goes through os.kill(pid, 0); what the op may
    # deliver on top of it is exactly one SIGINT to the holder.
    signals = cast("list[tuple[int, int]]", cancel_env["signals"])
    assert (os.getpid(), signal.SIGINT) in signals
    assert all(sig in (0, signal.SIGINT) for _pid, sig in signals)


def test_cancel_refuses_with_no_orchestration(
    monkeypatch: pytest.MonkeyPatch, cancel_env: dict[str, object]
) -> None:
    cancel_env["session"] = None
    with pytest.raises(ClusterUpdateInProgress, match="no rollout/restart orchestration"):
        _ops.cluster_cancel_op()
    assert cancel_env["signals"] == []


def test_cancel_refuses_a_host_self_update(
    monkeypatch: pytest.MonkeyPatch, cancel_env: dict[str, object]
) -> None:
    """A live ava-updater is not a cluster orchestration — the op owns nothing of
    it (the host's own reaper does)."""
    cancel_env["session"] = "updater"
    with pytest.raises(ClusterUpdateInProgress, match="mid self-update"):
        _ops.cluster_cancel_op()
    assert cancel_env["signals"] == []


def test_cancel_refuses_a_settle_hold(
    monkeypatch: pytest.MonkeyPatch, cancel_env: dict[str, object]
) -> None:
    """A settle hold is a stated waiting period with nothing executing — nothing to
    cancel; `ava cluster recover` is the tool that breaks it."""
    _set_lease(monkeypatch, _lease("m1:pid1", note="settling, waiting for: win"))
    with pytest.raises(ClusterUpdateInProgress, match="settle hold"):
        _ops.cluster_cancel_op()
    assert cancel_env["signals"] == []


def test_cancel_refuses_a_kindless_lease(
    monkeypatch: pytest.MonkeyPatch, cancel_env: dict[str, object]
) -> None:
    """A rollback or legacy orchestration holds the lease without a kind; cancel is
    scoped to rollout/restart and says so instead of guessing."""
    _set_lease(monkeypatch, _lease(f"m1:pid{os.getpid()}", kind=None))
    with pytest.raises(ClusterUpdateInProgress, match="no rollout/restart kind"):
        _ops.cluster_cancel_op()
    assert cancel_env["signals"] == []


def test_cancel_refuses_a_holder_on_another_machine(
    monkeypatch: pytest.MonkeyPatch, cancel_env: dict[str, object]
) -> None:
    """The holder string's pid is meaningless in this host's namespace — signalling
    it could hit an unrelated local process. Refuse and name the machine the
    orchestration actually runs on."""
    _set_lease(monkeypatch, _lease(f"other:pid{os.getpid()}"))
    with pytest.raises(ClusterUpdateInProgress, match="on other"):
        _ops.cluster_cancel_op()
    assert cancel_env["signals"] == []


def test_cancel_refuses_a_dead_local_holder(
    monkeypatch: pytest.MonkeyPatch, cancel_env: dict[str, object]
) -> None:
    """The holder names this machine but the pid is gone: the orchestration is
    dead, and the right tool is recover, not cancel — an interrupt cannot reach
    a process that no longer exists."""
    _set_lease(monkeypatch, _lease("m1:pid999999999"))

    # High pids recycle fast on macOS — pin the liveness verdict instead of
    # gambling that 999999999 is free.
    def _dead(_pid: int) -> bool:
        return False

    monkeypatch.setattr("shared.proc.process_alive", _dead)
    with pytest.raises(ClusterUpdateInProgress, match="recover"):
        _ops.cluster_cancel_op()
    assert cancel_env["signals"] == []


def test_cancel_in_the_prelease_window_uses_the_running_record(
    monkeypatch: pytest.MonkeyPatch, cancel_env: dict[str, object]
) -> None:
    """Between the session spawn and the lease acquire there is no lease row yet;
    the RUNNING last-update record still names the process, and canceling it is
    legitimate — the finally has not paused anything, so it unwinds clean."""
    from datetime import UTC, datetime

    from shared.last_update import LastUpdate, UpdateOutcome

    _set_lease(monkeypatch, None)
    holder = f"m1:pid{os.getpid()}"
    record = LastUpdate(
        outcome=UpdateOutcome.RUNNING,
        failed=False,
        target_sha="abc1234",
        origin="cli:m1",
        holder=holder,
        started_at=datetime.now(UTC),
    )
    monkeypatch.setattr("shared.last_update.read_last_update", lambda: record)

    result = _ops.cluster_cancel_op()

    assert result == {"cancelled": holder}
    signals = cast("list[tuple[int, int]]", cancel_env["signals"])
    assert (os.getpid(), signal.SIGINT) in signals


def test_cancel_refuses_when_neither_lease_nor_record_names_an_owner(
    monkeypatch: pytest.MonkeyPatch, cancel_env: dict[str, object]
) -> None:
    _set_lease(monkeypatch, None)
    monkeypatch.setattr("shared.last_update.read_last_update", lambda: None)
    with pytest.raises(ClusterUpdateInProgress, match="neither a deploy lease"):
        _ops.cluster_cancel_op()
    assert cancel_env["signals"] == []


def test_cancel_unwind_settles_the_lease_and_clears_the_maintenance_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Durable maintenance/hold stay correct after a cancel — the reason the cancel
    is a SIGINT and never a kill: the signal becomes KeyboardInterrupt inside the
    orchestration, and its `finally` is the recovery. With hosts still mid-transition it converts
    the deploy lease into a settle hold over exactly those hosts and clears the
    durable maintenance generation — the two durable facts that would otherwise
    block every later deploy (P1, 2026-08-30)."""
    from datetime import UTC, datetime
    from pathlib import Path as _Path

    from cli.commands import update as update_mod
    from cli.commands.update import _run_gateway_orchestration
    from shared import ui_update_state as uis
    from shared.cluster_lock import DeployLease

    monkeypatch.setattr("shared.machine.machine_name", lambda: "m1")
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)

    holder = f"m1:pid{os.getpid()}"
    settled: list[list[str]] = []
    released: list[str] = []
    cleared: list[str | None] = []

    def _acquire(_holder: str, kind: str | None = None) -> bool:
        return True

    monkeypatch.setattr(update_mod, "acquire_update_lock", _acquire)
    monkeypatch.setattr(
        update_mod,
        "settle_update_lock",
        lambda _h, hosts, **_k: settled.append(hosts) or True,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(update_mod, "release_update_lock", released.append)  # type: ignore[arg-type]
    monkeypatch.setattr(
        update_mod,
        "read_update_lease",
        lambda: DeployLease(
            holder=holder,
            held_for_s=0.0,
            expires_in_s=600.0,
            note=None,
            kind="rollout",
            acquired_at=datetime.now(UTC),
        ),
    )
    monkeypatch.setattr(uis, "read", lambda: uis.UiUpdateSnapshot(status="inactive"))

    def _begin(kind: str | None = None, origin: str | None = None) -> object:
        return uis.UiUpdateSnapshot(status="updating", generation="g1", kind="rollout")

    monkeypatch.setattr(uis, "begin", _begin)
    monkeypatch.setattr(uis, "set_phase", lambda *_a, **_k: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(uis, "clear", cleared.append)  # type: ignore[arg-type]

    def _inner(repo: _Path, *, unconverged: list[str], **kwargs: object) -> int:
        # The SIGINT lands while two hosts acked but never came back.
        unconverged.extend(["win", "wsl"])
        raise KeyboardInterrupt

    monkeypatch.setattr(update_mod, "_run_gateway_orchestration_inner", _inner)  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(KeyboardInterrupt):
        _run_gateway_orchestration(tmp_path, restart_only=False, origin="cli:m1")

    assert settled == [["win", "wsl"]], (
        "the lease must convert to a settle hold over the unconverged hosts"
    )
    assert released == [], "an interrupted rollout with hosts mid-transition must NOT release"
    assert cleared == ["g1"], "the durable maintenance marker must be cleared"
