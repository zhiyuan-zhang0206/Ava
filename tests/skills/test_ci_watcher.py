"""Hermetic tests for the ship-a-change reference CI watcher.

`.agents/skills/ship-a-change/reference/ci_watcher.py` is a launched code string,
not an importable module: the launching agent reads the file, string-substitutes
the placeholder assignments (REPO_ROOT / PR_NUMBER / CI_UTILS / WATCHER_ID), then
hands the result to `ava.watcher.launch`. These tests exercise that same
artifact the same way — substitute the placeholders, stub `ava` and `ci_utils`,
exec — so the settle semantics the template exists to guarantee stay locked
(2026-09-12, task #3158: a just-pushed PR read as a settled NO_CHECKS) and the
delivery semantics stay locked too (2026-09-17, task #3692: the verdict is
persisted before any send, delivery retries ride out a restart window, and an
exhausted delivery exits 2 with the persisted verdict left behind).
"""

from __future__ import annotations

import importlib.util
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE = _REPO_ROOT / ".agents" / "skills" / "ship-a-change" / "reference" / "ci_watcher.py"
# The substitution below pins PR_NUMBER = "1234"; the verdict file name follows
# from it (the template's VERDICT_FILE).
_VERDICT_NAME = "ci-verdict-1234.txt"


@lru_cache(maxsize=1)
def _ci_status() -> Any:
    """The repo's own CIStatus enum — the watcher branches on it, never strings."""
    spec = importlib.util.spec_from_file_location(
        "ci_utils_under_watcher_test", _REPO_ROOT / "scripts" / "ci_utils.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.CIStatus


class _Result:
    """The CIResult surface the watcher reads."""

    def __init__(self, verdict: Any) -> None:
        self.verdict = verdict
        self.mergeable = "MERGEABLE"
        self.passed: list[str] = []
        self.failed: list[dict[str, str]] = []
        self.error_detail = ""


class _FakeAva:
    """Records wakes and file writes instead of doing them.

    `send_failures` / `write_failures` make that many leading calls raise —
    the shapes of a gateway refusing connections inside a restart window and
    of an unwritable verdict file.
    """

    def __init__(self, files_root: Path, send_failures: int = 0, write_failures: int = 0) -> None:
        self.wakes: list[tuple[int, str]] = []
        self.send_calls = 0
        self.send_failures = send_failures
        self.write_calls = 0
        self.write_failures = write_failures
        # Whether the verdict file existed at each send attempt — locks the
        # persist-before-send order, not just the end state.
        self.verdict_written_at_send: list[bool] = []
        fake = self

        class _Agents:
            @staticmethod
            def send_message(agent_id: int, content: str) -> None:
                fake.send_calls += 1
                fake.verdict_written_at_send.append((files_root / _VERDICT_NAME).exists())
                if fake.send_calls <= fake.send_failures:
                    raise RuntimeError("gateway refused the connection")
                fake.wakes.append((agent_id, content))

        class _Files:
            @staticmethod
            def write(path: str, content: str) -> None:
                fake.write_calls += 1
                if fake.write_calls <= fake.write_failures:
                    raise RuntimeError("disk is full")
                # Stand-in for the SDK's workspace resolution: the watcher's
                # relative VERDICT_FILE lands under the test's tmp dir.
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
) -> tuple[list[tuple[int, str]], list[Any], _FakeAva]:
    """Exec the substituted template; return its wakes, polls, and fake.

    `verdicts` is how `check_ci` answers, one entry per poll; the last entry
    repeats if the watcher polls again.  A caller that must assert after the
    watcher raises (the exhausted-delivery or probe-error exit) passes its own
    fake; `raise_on_poll` makes the probe raise instead of answering.
    """
    statuses = _ci_status()
    polled: list[Any] = []

    def _check_ci(_pr: str | int) -> _Result:
        if raise_on_poll is not None:
            raise raise_on_poll
        verdict = verdicts[min(len(polled), len(verdicts) - 1)]
        polled.append(verdict)
        return _Result(verdict)

    fake_ava = fake if fake is not None else _FakeAva(tmp_path)
    monkeypatch.chdir(tmp_path)

    code = _TEMPLATE.read_text()
    substitutions = (
        ('REPO_ROOT = ""', f'REPO_ROOT = "{tmp_path}"'),
        ('PR_NUMBER = ""', 'PR_NUMBER = "1234"'),
        ('CI_UTILS = ""', f'CI_UTILS = "{tmp_path}"'),
        ("WATCHER_ID = 0", "WATCHER_ID = 42"),
        # The retry windows are behavior; their wall clocks are not. Sleep-free
        # polls keep this test instant — and a template whose default moves must
        # fail here loudly, not silently sleep the real 60s per retry.
        ("CHECK_EVERY = 60", "CHECK_EVERY = 0"),
        ("WAKE_BACKOFF_S = 10.0", "WAKE_BACKOFF_S = 0.0"),
        # The two imports are substituted rather than patched through
        # sys.modules: a stub left there leaks into fixture teardown, which
        # re-imports `ava` (same substitution the generated-watcher tests use).
        ("import ava\n", ""),
        ("from ci_utils import CIStatus, check_ci  # noqa: E402\n", ""),
    )
    for old, new in substitutions:
        assert old in code, f"template no longer contains {old!r} — update this test"
        code = code.replace(old, new)
    try:
        exec(
            compile(code, str(_TEMPLATE), "exec"),
            {
                "__name__": "__ci_watcher__",
                "ava": fake_ava,
                "CIStatus": statuses,
                "check_ci": _check_ci,
            },
        )
    finally:
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))
    return fake_ava.wakes, polled, fake_ava


def test_attach_window_does_not_wake_as_no_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reported bug (2026-09-12, PR #2249): the watcher starts right after
    the push, and the empty rollup while the checks attach must not be reported
    as a settled NO_CHECKS — the wake comes only once CI actually settles."""
    statuses = _ci_status()
    wakes, polled, _ = _run_watcher(
        monkeypatch,
        tmp_path,
        [statuses.NO_CHECKS, statuses.NO_CHECKS, statuses.PENDING, statuses.ALL_PASSED],
    )

    assert len(polled) == 4  # both attach-window NO_CHECKS verdicts were absorbed
    assert len(wakes) == 1
    agent_id, message = wakes[0]
    assert agent_id == 42
    assert "all_passed" in message


def test_persistent_no_checks_is_reported_after_bounded_retries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A rollup that stays empty is still reported — never swallowed: after its
    retry budget the watcher wakes with the verdict and says it re-polled."""
    statuses = _ci_status()
    wakes, polled, _ = _run_watcher(monkeypatch, tmp_path, [statuses.NO_CHECKS])

    assert len(polled) == 4  # the 3 retries + the poll that reports
    assert len(wakes) == 1
    assert "no_checks" in wakes[0][1]
    assert "re-polled 3 times" in wakes[0][1]


@pytest.mark.parametrize("member", ["FAILED", "ERROR"])
def test_settled_verdict_wakes_on_the_first_poll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, member: str
) -> None:
    """Retries are confined to NO_CHECKS: any other settled verdict — a red PR or
    a failed probe — wakes on the first poll."""
    statuses = _ci_status()
    wakes, polled, _ = _run_watcher(monkeypatch, tmp_path, [getattr(statuses, member)])

    assert len(polled) == 1
    assert len(wakes) == 1
    assert member.lower() in wakes[0][1]


def test_settled_verdict_is_persisted_before_the_wake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fallback record (2026-09-17, task #3692): the settled verdict is
    written to the agent workspace before the first send attempt and holds
    exactly the message the wake delivers — one representation, so a wake lost
    in a restart window still leaves the verdict behind."""
    statuses = _ci_status()
    wakes, _, fake = _run_watcher(monkeypatch, tmp_path, [statuses.FAILED])

    assert fake.send_calls == 1
    assert fake.verdict_written_at_send == [True]
    assert len(wakes) == 1
    persisted = (tmp_path / _VERDICT_NAME).read_text(encoding="utf-8")
    assert persisted == wakes[0][1] + "\n"
    assert "failed" in persisted


def test_wake_retries_across_a_restart_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two refused sends — a gateway restart window — and the third lands: the
    retry loop delivers the verdict the single send would have dropped."""
    statuses = _ci_status()
    wakes, _, fake = _run_watcher(
        monkeypatch, tmp_path, [statuses.ALL_PASSED], fake=_FakeAva(tmp_path, send_failures=2)
    )

    assert fake.send_calls == 3
    assert all(fake.verdict_written_at_send)  # persisted before every retry
    assert len(wakes) == 1
    assert "all_passed" in wakes[0][1]
    assert "wake attempt 1/8 failed" in capsys.readouterr().out


def test_exhausted_delivery_exits_2_and_keeps_the_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every attempt refused is not a silent death: the watcher exits 2 (the
    exit notice is the second delivery path) and the persisted verdict remains
    for a later read."""
    statuses = _ci_status()
    fake = _FakeAva(tmp_path, send_failures=100)
    with pytest.raises(SystemExit) as excinfo:
        _run_watcher(monkeypatch, tmp_path, [statuses.FAILED], fake=fake)

    assert excinfo.value.code == 2
    assert fake.send_calls == 8  # WAKE_ATTEMPTS, exhaustion really tried
    assert "wake delivery failed after 8 attempts" in capsys.readouterr().out
    assert "failed" in (tmp_path / _VERDICT_NAME).read_text(encoding="utf-8")


def test_probe_error_is_persisted_then_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The probe-error terminal path (2026-09-17 review): persist + echo run
    before exit 1, across the full retry budget, so the error survives even a
    wake that cannot land."""
    fake = _FakeAva(tmp_path, send_failures=100)
    error = RuntimeError("gh exploded")
    with pytest.raises(SystemExit) as excinfo:
        _run_watcher(monkeypatch, tmp_path, [], fake=fake, raise_on_poll=error)

    assert excinfo.value.code == 1
    assert fake.send_calls == 8  # the retry budget was spent before exiting
    persisted = (tmp_path / _VERDICT_NAME).read_text(encoding="utf-8")
    assert "CI watcher error" in persisted
    assert "gh exploded" in persisted
    assert "the verdict is at ci-verdict-1234.txt" in capsys.readouterr().out


def test_persist_failure_does_not_claim_a_stored_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A persist failure must not be reported as a stored verdict (2026-09-17
    review): the message still goes out, and the exhaustion report says the log
    holds the only copy."""
    statuses = _ci_status()
    fake = _FakeAva(tmp_path, send_failures=100, write_failures=100)
    with pytest.raises(SystemExit) as excinfo:
        _run_watcher(monkeypatch, tmp_path, [statuses.FAILED], fake=fake)

    assert excinfo.value.code == 2
    out = capsys.readouterr().out
    assert "verdict persist failed" in out
    assert "the verdict was not persisted" in out
    assert not (tmp_path / _VERDICT_NAME).exists()
