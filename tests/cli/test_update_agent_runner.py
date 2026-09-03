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
