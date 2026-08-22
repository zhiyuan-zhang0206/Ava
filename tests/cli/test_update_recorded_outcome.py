"""What the gateway orchestration RECORDS about its own run — the write side of
`shared.last_update`.

Two facts travel from the run to the record and are testable nowhere else, because
by the time a surface reads the row the run that knew them is gone:

- **that the rollout recovered itself.** A local update returning rc=1 on the pull
  path means the gateway rolled back to last-known-good and came up, so the cluster
  is fine and only the update failed. Nothing downstream can tell that apart from
  the abort that left a gateway down — both are `RolloutOutcome.ABORTED` — so the
  leg that watched it happen reports it.
- **which log this run was writing.** The path is created by `spawn_rollout`, in
  another process, seconds before this one exists.

Seam-level throughout: the DB behavior of the record is `tests/shared/test_last_update.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cli import commands as _cli
from cli.commands import _update_recover as _rec
from cli.commands import update as _up


@pytest.fixture(autouse=True)
def _orchestration_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """Everything between the preflight and the `finally`, stubbed to nothing: these
    tests are about what reaches `finalize_rollout`, not about the rollout."""
    monkeypatch.setattr(_up, "acquire_update_lock", lambda _holder, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "release_update_lock", lambda _holder: None)  # pyright: ignore[reportUnknownArgumentType]
    # The record's own write is stubbed at `shared.last_update`, not at
    # `_begin_update_record`, so the plumbing between them stays under test.
    monkeypatch.setattr("shared.last_update.begin_update", lambda **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "_run_preflight_fetch", lambda *_a, **_kw: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "_stop_the_world", lambda _runners, **_kw: (set(), True))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_resolve_fanout_targets", list)
    monkeypatch.setattr("ops.cluster.unpause_local_cluster", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]


def _capture_finalize(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def _finalize(*_a: Any, **kw: Any) -> None:
        captured.update(kw)

    monkeypatch.setattr(_up, "finalize_rollout", _finalize)
    return captured


def _run_with_local_rc(
    monkeypatch: pytest.MonkeyPatch, rc: int, *, restart_only: bool = False
) -> dict[str, Any]:
    monkeypatch.setattr(
        _up,
        "_rollout_preflight",
        lambda *_a, **_kw: (None, False, "PINNEDSHA1234567"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_cli, "_run_gateway_local_update", lambda *_a, **_kw: rc)  # pyright: ignore[reportUnknownArgumentType]
    captured = _capture_finalize(monkeypatch)
    _up._run_gateway_orchestration(Path("/unused"), origin="test-origin", restart_only=restart_only)
    return captured


def test_a_gateway_leg_that_rolled_itself_back_reports_the_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rc=1 on the pull path is `_run_gateway_local_update` saying it recovered to
    last-known-good and the gateway is back. The outcome stays ABORTED — the
    aftermath report resumes the same hosts either way — but the record has to carry
    the difference, because "failed, cluster fine" and "failed, cluster down" want
    different things from an operator."""
    captured = _run_with_local_rc(monkeypatch, 1)

    assert captured["recovered"] is True


def test_a_gateway_left_down_is_not_dressed_up_as_a_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rc=2 is the gateway DOWN with no auto-fix — the case that needs a human. It
    must never reach the record as `recovered`."""
    captured = _run_with_local_rc(monkeypatch, 2)

    assert captured["recovered"] is False


def test_a_failed_restart_only_bounce_is_not_a_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart-only bounce pulls nothing, so it has nothing to roll back TO: its
    rc=1 is the raw `ava start` code of a failed bounce, and reading it as the pull
    path's recovery would report a rollback that never happened."""
    captured = _run_with_local_rc(monkeypatch, 1, restart_only=True)

    assert captured["recovered"] is False


def test_a_clean_rollout_reports_no_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _run_with_local_rc(monkeypatch, 0)

    assert captured["recovered"] is False


# ─── the mapping onto the recorded outcome ───────────────────────────────────


def _record_via_finalize(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> list[Any]:
    """Drive `finalize_rollout` with nothing to resume and capture what it writes."""
    from shared import last_update as _lu

    written: list[Any] = []
    monkeypatch.setattr(_lu, "finish_update", lambda outcome, **_kw: written.append(outcome))  # pyright: ignore[reportUnknownArgumentType]
    _rec.finalize_rollout([], lambda *_a, **_k: [], 1.0, pin_advanced=False, **kw)  # pyright: ignore[reportUnknownArgumentType]
    return written


def test_a_self_recovered_abort_is_recorded_as_recovered(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.last_update import UpdateOutcome

    written = _record_via_finalize(monkeypatch, outcome=_rec.RolloutOutcome.ABORTED, recovered=True)

    assert written == [UpdateOutcome.RECOVERED]


def test_an_abort_with_no_recovery_is_recorded_as_aborted(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.last_update import UpdateOutcome

    written = _record_via_finalize(
        monkeypatch, outcome=_rec.RolloutOutcome.ABORTED, recovered=False
    )

    assert written == [UpdateOutcome.ABORTED]


# ─── the rollout's own log path ──────────────────────────────────────────────


def test_the_orchestration_stamps_the_log_it_was_handed_onto_the_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--rollout-log` exists so the record names THE log rather than the newest
    `rollout-*.log` a reader would otherwise have to guess at."""
    monkeypatch.setattr(
        _up,
        "_rollout_preflight",
        lambda *_a, **_kw: (None, False, "PINNEDSHA1234567"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_cli, "_run_gateway_local_update", lambda *_a, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]
    _capture_finalize(monkeypatch)
    opened: dict[str, Any] = {}
    monkeypatch.setattr(_up, "_begin_update_record", lambda sha, **kw: opened.update(kw, sha=sha))  # pyright: ignore[reportUnknownArgumentType]

    _up._run_gateway_orchestration(
        Path("/unused"),
        origin="test-origin",
        rollout_log="/home/ava/.ava/logs/rollout-1785470000.log",
    )

    assert opened["rollout_log"] == "/home/ava/.ava/logs/rollout-1785470000.log"


def test_local_dispatch_stamps_the_detached_rollout_log_onto_the_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The detached session enters through `cmd_update --local`; every dispatch
    seam between that entry and the record opener must preserve its log path."""
    from cli.commands import _update_dispatch as _dispatch

    monkeypatch.setattr(_up, "_repo_root", lambda: Path("/unused"))
    monkeypatch.setattr(_up, "ava_home", lambda: Path("/home/ava/.ava"))
    monkeypatch.setattr(_up, "get_record", lambda _home: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_dispatch, "hosting_supervised_session", lambda: None)
    monkeypatch.setattr(
        _up,
        "_rollout_preflight",
        lambda *_a, **_kw: (None, False, "PINNEDSHA1234567"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_cli, "_run_gateway_local_update", lambda *_a, **_kw: 0)  # pyright: ignore[reportUnknownArgumentType]
    _capture_finalize(monkeypatch)
    opened: dict[str, Any] = {}
    monkeypatch.setattr(_up, "_begin_update_record", lambda sha, **kw: opened.update(kw, sha=sha))  # pyright: ignore[reportUnknownArgumentType]
    log_path = "/home/ava/.ava/logs/rollout-1785470000.log"

    assert _cli.cmd_update(local=True, origin="test-origin", rollout_log=log_path) == 0

    assert opened["rollout_log"] == log_path


def test_the_record_write_carries_the_log_through_to_shared_last_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The intent write is the only one that records the path, so this is the whole
    of the plumbing: anything it drops is dropped for good."""
    from shared import last_update as _lu

    seen: dict[str, Any] = {}
    monkeypatch.setattr(_lu, "begin_update", lambda **kw: seen.update(kw))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.cluster_lock.self_holder", lambda: "mini:pid1")

    _up._begin_update_record(
        "PINNEDSHA1234567",
        origin="test-origin",
        rollout_log="/home/ava/.ava/logs/rollout-1785470000.log",
    )

    assert seen["log_path"] == "/home/ava/.ava/logs/rollout-1785470000.log"


def test_a_foreground_local_update_records_no_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ava cluster update --local` from a terminal is not teed to a file. Recording the
    previous rollout's log would point an operator at a different run."""
    from shared import last_update as _lu

    seen: dict[str, Any] = {}
    monkeypatch.setattr(_lu, "begin_update", lambda **kw: seen.update(kw))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.cluster_lock.self_holder", lambda: "mini:pid1")

    _up._begin_update_record("PINNEDSHA1234567", origin="cli:mini")

    assert seen["log_path"] is None
