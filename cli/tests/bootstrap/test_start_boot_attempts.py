"""`ava start --boot`: a failing boot start asks its scheduler to retry, boundedly."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from base.host.system import boot_policy
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from cli import start_intent
from cli.parsers import build_parser


@pytest.fixture(autouse=True)
def _boot_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(boot_policy.psutil, "boot_time", lambda: 1_000_000.0)


def _boot(home: Path, code: int) -> int:
    return start_intent._run_boot_attempt(home, lambda: code)


def test_the_parser_marks_a_boot_start_only_when_asked() -> None:
    parser = build_parser()
    assert parser.parse_args(["start"]).boot is False
    assert parser.parse_args(["start", "--boot"]).boot is True


@pytest.mark.parametrize("macos", [True, False])
def test_failed_attempts_before_the_budget_keep_their_exit_code(
    tmp_path: Path,
    emitted_events: EmittedEvents,
    monkeypatch: pytest.MonkeyPatch,
    macos: bool,
) -> None:
    monkeypatch.setattr(boot_policy, "is_macos", lambda: macos)
    codes = [_boot(tmp_path, 75) for _ in range(boot_policy.BOOT_MAX_ATTEMPTS - 1)]
    assert codes == [75] * (boot_policy.BOOT_MAX_ATTEMPTS - 1)
    assert len(emitted_events) == 0


def test_the_attempt_that_spends_the_budget_alerts_and_stops_launchd(
    tmp_path: Path,
    emitted_events: EmittedEvents,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(boot_policy, "is_macos", lambda: True)
    for _ in range(boot_policy.BOOT_MAX_ATTEMPTS - 1):
        assert _boot(tmp_path, 75) == 75
    assert _boot(tmp_path, 75) == 0  # launchd retries every nonzero exit
    (event,) = emitted_events
    assert (event.category, event.event_name) == ("telemetry", "boot_retries_exhausted")
    assert event.level == "error"
    assert event.attributes == {"attempts": boot_policy.BOOT_MAX_ATTEMPTS, "exit_code": 75}
    assert "not retrying" in capsys.readouterr().err


def test_on_linux_the_spending_attempt_still_fails_for_systemd(
    tmp_path: Path,
    emitted_events: EmittedEvents,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(boot_policy, "is_macos", lambda: False)
    codes = [_boot(tmp_path, 75) for _ in range(boot_policy.BOOT_MAX_ATTEMPTS)]
    assert codes[-1] == 75
    assert len(emitted_events) == 1


def test_a_start_after_the_budget_still_runs_and_success_clears_the_count(
    tmp_path: Path,
    emitted_events: EmittedEvents,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The count only decides what a failure asks for; a restarted boot job gets a real attempt."""
    monkeypatch.setattr(boot_policy, "is_macos", lambda: True)
    for _ in range(boot_policy.BOOT_MAX_ATTEMPTS):
        _boot(tmp_path, 75)
    ran: list[int] = []

    def recovered() -> int:
        ran.append(1)
        return 0

    assert start_intent._run_boot_attempt(tmp_path, recovered) == 0
    assert ran == [1]
    assert boot_policy.begin_attempt(tmp_path) == 1


def test_run_start_hands_the_boot_flag_to_the_start_and_wraps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def once(args: Any, runtime: Any, retained: Any, *, boot: bool = False, **_owners: Any) -> int:
        seen["boot"] = boot
        return 0

    def wrapped(home: Path, start: Any) -> int:
        seen["home"] = home
        return start()

    monkeypatch.setattr(start_intent, "start_once", once)
    monkeypatch.setattr(start_intent, "_run_boot_attempt", wrapped)
    monkeypatch.setattr(start_intent, "_home", lambda: tmp_path)

    def run(args: Any) -> int:
        return start_intent.run_start(
            args,
            retained_children=[],
            database_factory=object,
            producer=cast(Any, object),
        )

    args = build_parser().parse_args(["start", "--boot"])
    assert run(args) == 0
    assert seen == {"boot": True, "home": tmp_path}
    args = build_parser().parse_args(["start"])
    seen.clear()
    assert run(args) == 0
    assert seen == {"boot": False}
