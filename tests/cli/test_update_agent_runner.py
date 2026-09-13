"""The agent-runner updater must attach the CLI loguru sink set.

`_update_agent_runner` runs as `python -m cli.commands._update_agent_runner`
(spawned by `spawn_update`), which bypasses `cli/main.py` — the only place the
CLI sink set is normally attached. Without it, `shared.log`'s module-level
`logger.remove()` leaves the process with no handler at all, and every
`logger.error` inside converge is dropped: for months the schtasks failure
detail behind "watchdog-probe registration failed on Windows for agent-runner"
(#885 / #1117) was invisible, leaving a failed Windows self-update with no
diagnosable cause in its own log.

The normal dispatch is intercepted before any update work. Help and prepared
bootstrap dispatch must not initialize ordinary file/database logging sinks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.commands import _update_agent_runner as updater


def test_main_attaches_cli_sinks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Normal dispatch attaches logging before entering actual update work."""
    from shared import log

    calls: list[dict[str, object]] = []

    def _fake_init(**kw: object) -> None:
        calls.append(kw)

    monkeypatch.setattr(log, "init_cli_process", _fake_init)
    monkeypatch.setattr(updater, "_repo_root", lambda: tmp_path)

    def dispatch(*_args: object, **_kwargs: object) -> int:
        assert calls == [{"name": "updater"}]
        return 17

    monkeypatch.setattr(updater, "_run_agent_runner_self_update", dispatch)
    assert updater.main([]) == 17
    assert calls == [{"name": "updater"}]


def test_help_does_not_attach_cli_sinks(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared import log

    def forbidden(**_kwargs: object) -> None:
        pytest.fail("help must not initialize file/database logging")

    monkeypatch.setattr(log, "init_cli_process", forbidden)

    with pytest.raises(SystemExit) as exc_info:
        updater.main(["--help"])
    assert exc_info.value.code == 0


def test_normal_release_dispatch_has_no_source_or_logging_effect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cli.commands import _update_normal_release as normal
    from shared import log

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("prepared normal dispatch must not use source or logging initialization")

    def run(path: Path) -> int:
        assert path == tmp_path / "request.json"
        return 29

    monkeypatch.setattr(log, "init_cli_process", forbidden)
    monkeypatch.setattr(updater, "_repo_root", forbidden)
    monkeypatch.setattr(normal, "run_normal_release", run)
    assert updater.main(["--normal-release", str(tmp_path / "request.json")]) == 29


@pytest.mark.parametrize("flag", ["--restart-only", "--force-reap", "--post-checkout"])
def test_normal_release_rejects_source_flags(flag: str) -> None:
    with pytest.raises(SystemExit) as error:
        updater.main(["--normal-release", "/not-read.json", flag])
    assert error.value.code == 2


def test_main_survives_log_init_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A DB-unreachable postgres sink (real on the recovery path) must not
    abort the updater — stderr/file sinks attach before the postgres sink."""
    from shared import log

    attempted: list[bool] = []

    def boom(**kw: object) -> None:
        attempted.append(True)
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(log, "init_cli_process", boom)
    monkeypatch.setattr(updater, "_repo_root", lambda: tmp_path)

    def dispatch(*_args: object, **_kwargs: object) -> int:
        return 17

    monkeypatch.setattr(updater, "_run_agent_runner_self_update", dispatch)
    assert updater.main([]) == 17
    assert attempted == [True]


def test_bootstrap_dispatch_does_not_attach_normal_sinks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from shared import log

    def forbidden(**_kwargs: object) -> None:
        pytest.fail("prepared bootstrap must not initialize ordinary logging")

    def dispatch(*_args: object, **kwargs: object) -> int:
        assert kwargs["bootstrap_request"] == tmp_path / "request.json"
        return 3

    monkeypatch.setattr(log, "init_cli_process", forbidden)
    monkeypatch.setattr(updater, "_run_agent_runner_self_update", dispatch)
    assert updater.main(["--bootstrap-hop", str(tmp_path / "request.json")]) == 3


def test_normal_preflight_refusal_precedes_updater_lock_and_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from unittest.mock import Mock

    from cli.commands import _update_bootstrap as bootstrap
    from cli.commands import _update_normal_release as normal
    from shared import host_deploy_state
    from shared.runtime_release import ReleaseRejectedError

    prepared = Mock(spec=bootstrap.PreparedBootstrapHop)
    prepared.request = Mock(normal_release_path=str(tmp_path / "normal.json"))

    def prepared_hop(_path: Path) -> bootstrap.PreparedBootstrapHop:
        return prepared

    monkeypatch.setattr(bootstrap, "prepare_bootstrap_hop", prepared_hop)

    def reject(_prepared: bootstrap.PreparedBootstrapHop) -> None:
        assert _prepared is prepared
        raise ReleaseRejectedError("unsupported normal roster")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("failed normal preflight must precede lock, handoff and stop effects")

    monkeypatch.setattr(normal, "prepare_after_bootstrap", reject)
    monkeypatch.setattr(host_deploy_state, "try_acquire_updater_lock", forbidden)
    monkeypatch.setattr(bootstrap, "execute_bootstrap_hop", forbidden)
    with pytest.raises(ReleaseRejectedError, match="unsupported normal roster"):
        updater._run_agent_runner_self_update(
            tmp_path, bootstrap_request=tmp_path / "bootstrap.json"
        )


def test_disabled_normal_activation_precedes_updater_lock_and_bootstrap_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from unittest.mock import Mock

    from cli.commands import _update_bootstrap as bootstrap
    from cli.commands import _update_normal_release as normal
    from shared import host_deploy_state
    from shared.runtime_release import ReleaseRejectedError

    prepared = Mock(spec=bootstrap.PreparedBootstrapHop)
    prepared.request = Mock(normal_release_path=str(tmp_path / "normal.json"))

    def prepare_hop(_path: Path) -> bootstrap.PreparedBootstrapHop:
        return prepared

    def prepare_normal(
        _prepared: bootstrap.PreparedBootstrapHop,
    ) -> normal.PreparedNormalRelease:
        return Mock(spec=normal.PreparedNormalRelease)

    monkeypatch.setattr(bootstrap, "prepare_bootstrap_hop", prepare_hop)
    monkeypatch.setattr(normal, "prepare_after_bootstrap", prepare_normal)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("disabled normal activation must precede updater ownership and effects")

    monkeypatch.setattr(host_deploy_state, "try_acquire_updater_lock", forbidden)
    monkeypatch.setattr(bootstrap, "execute_bootstrap_hop", forbidden)
    with pytest.raises(ReleaseRejectedError, match="checked crash recovery"):
        updater._run_agent_runner_self_update(
            tmp_path, bootstrap_request=tmp_path / "bootstrap.json"
        )


def test_short_target_sha_is_refused_before_any_update_work() -> None:
    """Issue #2343: the target machine refuses a prefix at its own entrypoint —
    the last gate before a paused host could be left behind by a deep failure."""
    with pytest.raises(SystemExit) as exc_info:
        updater.main(["--target-sha", "30df11a83"])
    assert exc_info.value.code == 2


def _held_snapshot(phase: str) -> object:
    from datetime import UTC, datetime

    from shared import pause_owner
    from shared.maintenance_state import MaintenanceHold

    return pause_owner.PauseOwnerSnapshot(
        status="paused",
        holder="op",
        acquired_at=datetime(2026, 9, 13, 3, 0, tzinfo=UTC),
        maintenance=MaintenanceHold(phase),  # type: ignore[arg-type]
    )


def test_self_release_runs_for_a_pre_stop_hold(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Task #3270: a standalone updater that failed before the stop releases the
    hold it was spawned under instead of leaving it for a manual recovery."""
    import contextlib

    from shared import maintenance, ui_update_state

    def _reader() -> object:
        return _held_snapshot("drained")

    monkeypatch.setattr(maintenance, "snapshot", _reader)
    monkeypatch.setattr(ui_update_state, "lifecycle_lock", contextlib.nullcontext)
    called: list[str] = []

    def _record(*, reason: str) -> None:
        called.append(reason)

    monkeypatch.setattr("ops.cluster_pause.release_pre_stop_hold", _record)
    updater._self_release_pre_stop_hold("test failure")
    assert called == ["test failure"]
    assert "released the pre-stop maintenance hold" in capsys.readouterr().out


def test_self_release_skips_a_post_stop_hold(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A started stop cannot be cancelled: post-stop recovery belongs to the
    watchdog's bounded paths, so the updater only prints the note."""
    from shared import maintenance

    def _reader() -> object:
        return _held_snapshot("stopping")

    monkeypatch.setattr(maintenance, "snapshot", _reader)
    called: list[str] = []

    def _record(*, reason: str) -> None:
        called.append(reason)

    monkeypatch.setattr("ops.cluster_pause.release_pre_stop_hold", _record)
    updater._self_release_pre_stop_hold("test failure")
    assert called == []
    assert "services were already stopped" in capsys.readouterr().err


def test_self_release_reports_a_refused_release(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refusal (e.g. an unreachable data plane) is loud and leaves the hold;
    the stranded-hold verdict is the operator-facing record behind it."""
    import contextlib

    from shared import maintenance, ui_update_state

    def _reader() -> object:
        return _held_snapshot("drained")

    monkeypatch.setattr(maintenance, "snapshot", _reader)
    monkeypatch.setattr(ui_update_state, "lifecycle_lock", contextlib.nullcontext)

    def _refuse(*, reason: str) -> None:
        raise RuntimeError("dependencies unavailable")

    monkeypatch.setattr("ops.cluster_pause.release_pre_stop_hold", _refuse)
    updater._self_release_pre_stop_hold("test failure")
    err = capsys.readouterr().err
    assert "could not self-release the pre-stop hold" in err
    assert "dependencies unavailable" in err


def test_self_release_is_a_noop_without_a_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared import maintenance

    def _none() -> None:
        return None

    monkeypatch.setattr(maintenance, "snapshot", _none)
    called: list[str] = []

    def _record(*, reason: str) -> None:
        called.append(reason)

    monkeypatch.setattr("ops.cluster_pause.release_pre_stop_hold", _record)
    updater._self_release_pre_stop_hold("nothing held")
    assert called == []
