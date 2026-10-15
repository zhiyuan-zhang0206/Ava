from __future__ import annotations

import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base import paths
from base.clock import Clock, ClockConfig
from base.clock.tests.manual_timers import ManualTimers
from base.host.system.tests.fake_commands import fake_host
from scripts.ci.cli import CIStatus

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEMPLATE = _REPO_ROOT / "scripts" / "ci" / "pull_requests" / "watch_pr.py"
_VERDICT_NAME = "ci-verdict-1234.txt"


class _Result:
    def __init__(self, verdict: Any) -> None:
        self.verdict = verdict
        self.mergeable = "MERGEABLE"
        self.passed: list[str] = []
        self.failed: list[dict[str, str]] = []
        self.error_detail = ""


class _FakeAva:
    def __init__(
        self,
        files_root: Path,
        send_failures: int = 0,
        write_failures: int = 0,
        send_error: Exception | None = None,
    ) -> None:
        self.wakes: list[tuple[int, str]] = []
        self.send_calls = 0
        self.send_failures = send_failures
        self.write_calls = 0
        self.write_failures = write_failures
        self.send_error = send_error
        self.verdict_written_at_send: list[bool] = []
        fake = self

        class _Agents:
            @staticmethod
            def send_message(agent_id: int, content: str) -> None:
                fake.send_calls += 1
                fake.verdict_written_at_send.append((files_root / _VERDICT_NAME).exists())
                if fake.send_error is not None:
                    raise fake.send_error
                if fake.send_calls <= fake.send_failures:
                    raise RuntimeError("gateway refused the connection")
                fake.wakes.append((agent_id, content))

        class _Files:
            @staticmethod
            def write(path: str, content: str) -> None:
                fake.write_calls += 1
                if fake.write_calls <= fake.write_failures:
                    raise RuntimeError("disk is full")
                target = files_root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

        self.agents = _Agents()
        self.files = _Files()


def _run_watcher(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    verdicts: list[Any],
    *,
    fake: _FakeAva | None = None,
    raise_on_poll: Exception | None = None,
    run_name: str = "__ci_watcher__",
    forbid_sleep: bool = False,
    on_atexit: Callable[..., None] | None = None,
) -> tuple[list[tuple[int, str]], list[Any], _FakeAva]:
    polled: list[Any] = []
    timers = ManualTimers()
    clock = Clock(
        ClockConfig(
            timezone="UTC",
            authoritative_timezone="UTC",
            message_timestamp_weekday=True,
        ),
        timers=timers,
        now=timers.now,
    )
    host = fake_host()

    def _check_ci(
        _pr: str | int,
        *,
        host: object = None,
        clock: object = None,
    ) -> _Result:
        assert host is not None and clock is not None
        if raise_on_poll is not None:
            raise raise_on_poll
        verdict = verdicts[min(len(polled), len(verdicts) - 1)]
        polled.append(verdict)
        return _Result(verdict)

    fake_ava = fake if fake is not None else _FakeAva(tmp_path)
    monkeypatch.chdir(tmp_path)

    def workspace_for_test(_agent_id: int) -> Path:
        return tmp_path

    monkeypatch.setattr(paths, "workspace_dir", workspace_for_test)

    code = _TEMPLATE.read_text()
    substitutions = (
        ('REPO_ROOT = ""', f'REPO_ROOT = "{tmp_path}"'),
        ('PR_NUMBER = ""', 'PR_NUMBER = "1234"'),
        ('CI_UTILS = ""', f'CI_UTILS = "{tmp_path}"'),
        ("WATCHER_ID = 0", "WATCHER_ID = 42"),
        ("import atexit\n", ""),
        ("import ava\n", ""),
        ("from base.clock import Clock\n", ""),
        ("from base.host.system.hostport import Host\n", ""),
        ("from scripts.ci.pull_requests.status import CIStatus, check_ci  # noqa: E402\n", ""),
    )
    for old, new in substitutions:
        assert old in code, f"template no longer contains {old!r} — update this test"
        code = code.replace(old, new)
    try:
        exec(
            compile(code, str(_TEMPLATE), "exec"),
            {
                "__name__": run_name,
                "atexit": types.SimpleNamespace(
                    register=on_atexit if on_atexit is not None else lambda *_a, **_kw: None
                ),
                "ava": fake_ava,
                "Clock": types.SimpleNamespace(from_settings=lambda: clock),
                "Host": types.SimpleNamespace(system=lambda: host),
                "CIStatus": CIStatus,
                "check_ci": _check_ci,
            },
        )
    finally:
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))
        if forbid_sleep:
            assert timers.sleeps == [], f"slept {timers.sleeps} after a failed wake"
    return fake_ava.wakes, polled, fake_ava


def test_attach_window_does_not_wake_as_no_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wakes, polled, _ = _run_watcher(
        monkeypatch,
        tmp_path,
        [CIStatus.NO_CHECKS, CIStatus.NO_CHECKS, CIStatus.PENDING, CIStatus.ALL_PASSED],
    )

    assert len(polled) == 4
    assert len(wakes) == 1
    agent_id, message = wakes[0]
    assert agent_id == 42
    assert "all_passed" in message


def test_persistent_no_checks_is_reported_after_bounded_retries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wakes, polled, _ = _run_watcher(monkeypatch, tmp_path, [CIStatus.NO_CHECKS])

    assert len(polled) == 4
    assert len(wakes) == 1
    assert "no_checks" in wakes[0][1]
    assert "re-polled 3 times" in wakes[0][1]


@pytest.mark.parametrize("member", ["FAILED", "ERROR"])
def test_settled_verdict_wakes_on_the_first_poll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, member: str
) -> None:
    wakes, polled, _ = _run_watcher(monkeypatch, tmp_path, [getattr(CIStatus, member)])

    assert len(polled) == 1
    assert len(wakes) == 1
    assert member.lower() in wakes[0][1]


def test_settled_verdict_is_persisted_before_the_wake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wakes, _, fake = _run_watcher(monkeypatch, tmp_path, [CIStatus.FAILED])

    assert fake.send_calls == 1
    assert fake.verdict_written_at_send == [True]
    assert len(wakes) == 1
    persisted = (tmp_path / _VERDICT_NAME).read_text(encoding="utf-8")
    assert persisted == wakes[0][1] + "\n"
    assert "failed" in persisted


def test_wake_is_sent_once_after_the_verdict_is_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wakes, _, fake = _run_watcher(monkeypatch, tmp_path, [CIStatus.ALL_PASSED])

    assert fake.send_calls == 1
    assert fake.verdict_written_at_send == [True]
    assert len(wakes) == 1
    assert "all_passed" in wakes[0][1]


def test_failed_delivery_exits_2_after_one_send_and_keeps_the_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeAva(tmp_path, send_failures=100)
    with pytest.raises(SystemExit) as excinfo:
        _run_watcher(monkeypatch, tmp_path, [CIStatus.FAILED], fake=fake, forbid_sleep=True)

    assert excinfo.value.code == 2
    assert fake.send_calls == 1
    out = capsys.readouterr().out
    assert "wake delivery failed — cause: RuntimeError: gateway refused the connection" in out
    assert f"the verdict is at {tmp_path / _VERDICT_NAME}" in out
    assert "failed" in (tmp_path / _VERDICT_NAME).read_text(encoding="utf-8")


def test_probe_error_is_persisted_then_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeAva(tmp_path, send_failures=100)
    error = RuntimeError("gh exploded")
    with pytest.raises(SystemExit) as excinfo:
        _run_watcher(monkeypatch, tmp_path, [], fake=fake, raise_on_poll=error, forbid_sleep=True)

    assert excinfo.value.code == 1
    assert fake.send_calls == 1
    persisted = (tmp_path / _VERDICT_NAME).read_text(encoding="utf-8")
    assert "CI watcher error" in persisted
    assert "gh exploded" in persisted
    assert f"the verdict is at {tmp_path / _VERDICT_NAME}" in capsys.readouterr().out


def test_owner_url_error_reports_its_cause_with_absolute_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from pydantic import BaseModel, ValidationError, model_validator

    from base.config.domains.storage.data_plane import AgentProfileOwnerDbUrlRefusedError

    class _Guarded(BaseModel):
        @model_validator(mode="after")
        def reject(self) -> _Guarded:
            raise AgentProfileOwnerDbUrlRefusedError("agent-profile owner URL refused")

    with pytest.raises(ValidationError) as error:
        _Guarded()
    fake = _FakeAva(tmp_path, send_error=error.value)
    with pytest.raises(SystemExit) as exited:
        _run_watcher(monkeypatch, tmp_path, [CIStatus.ALL_PASSED], fake=fake)

    assert exited.value.code == 2
    assert fake.send_calls == 1
    output = capsys.readouterr().out
    assert f"the verdict is at {tmp_path / _VERDICT_NAME}" in output
    assert "cause: agent-profile owner URL refused" in output
    assert "wake delivery failed" in output
    assert "all_passed" in (tmp_path / _VERDICT_NAME).read_text(encoding="utf-8")


def test_exit_notice_tail_keeps_path_and_cause_after_boot_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from pydantic import BaseModel, ValidationError, model_validator

    from base.config.domains.storage.data_plane import AgentProfileOwnerDbUrlRefusedError

    class _Guarded(BaseModel):
        @model_validator(mode="after")
        def reject(self) -> _Guarded:
            raise AgentProfileOwnerDbUrlRefusedError("owner URL refused")

    with pytest.raises(ValidationError) as error:
        _Guarded()
    callbacks: list[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]] = []

    def record_callback(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        callbacks.append((fn, args, kwargs))

    with pytest.raises(SystemExit):
        _run_watcher(
            monkeypatch,
            tmp_path,
            [CIStatus.ALL_PASSED],
            fake=_FakeAva(tmp_path, send_error=error.value),
            run_name="__main__",
            on_atexit=record_callback,
        )
    assert len(callbacks) == 1
    sys.stdout.write("later boot cleanup traceback" * 120 + "\n")
    fn, args, kwargs = callbacks[0]
    fn(*args, **kwargs)

    notice_tail = capsys.readouterr().out[-2048:]
    assert f"the verdict is at {tmp_path / _VERDICT_NAME}" in notice_tail
    assert "cause: owner URL refused" in notice_tail


def test_persist_failure_does_not_claim_a_stored_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeAva(tmp_path, send_failures=100, write_failures=100)
    with pytest.raises(SystemExit) as excinfo:
        _run_watcher(monkeypatch, tmp_path, [CIStatus.FAILED], fake=fake, forbid_sleep=True)

    assert excinfo.value.code == 2
    out = capsys.readouterr().out
    assert "verdict persist failed" in out
    assert "the verdict was not persisted" in out
    assert not (tmp_path / _VERDICT_NAME).exists()


def test_pending_watcher_reaches_its_deadline_without_real_sleep(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wakes, polled, _ = _run_watcher(monkeypatch, tmp_path, [CIStatus.PENDING])
    assert polled
    assert len(wakes) == 1
    assert "timed out" in wakes[0][1]
    assert "still pending" in wakes[0][1]
