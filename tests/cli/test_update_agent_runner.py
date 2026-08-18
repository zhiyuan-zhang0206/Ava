"""The agent-runner updater must attach the CLI loguru sink set.

`_update_agent_runner` runs as `python -m cli.commands._update_agent_runner`
(spawned by `spawn_update`), which bypasses `cli/main.py` — the only place the
CLI sink set is normally attached. Without it, `shared.log`'s module-level
`logger.remove()` leaves the process with no handler at all, and every
`logger.error` inside converge is dropped: for months the schtasks failure
detail behind "watchdog-probe registration failed on Windows for agent-runner"
(#885 / #1117) was invisible, leaving a failed Windows self-update with no
diagnosable cause in its own log.

The assertion runs `main()` with `--help`, which exits before any update work —
the sink attachment happens at the top of `main()`, so this pins exactly the
lines that make failures visible without exercising the update flow.
"""

from __future__ import annotations

import pytest

from cli.commands import _update_agent_runner as updater


def test_main_attaches_cli_sinks(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() calls init_cli_process(name="updater") before doing anything else."""
    from shared import log

    calls: list[dict[str, object]] = []

    def _fake_init(**kw: object) -> None:
        calls.append(kw)

    monkeypatch.setattr(log, "init_cli_process", _fake_init)

    with pytest.raises(SystemExit) as exc_info:
        updater.main(["--help"])
    assert exc_info.value.code == 0
    assert calls == [{"name": "updater"}]


def test_main_survives_log_init_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DB-unreachable postgres sink (real on the recovery path) must not
    abort the updater — stderr/file sinks attach before the postgres sink."""
    from shared import log

    def boom(**kw: object) -> None:
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(log, "init_cli_process", boom)

    with pytest.raises(SystemExit) as exc_info:
        updater.main(["--help"])
    assert exc_info.value.code == 0
