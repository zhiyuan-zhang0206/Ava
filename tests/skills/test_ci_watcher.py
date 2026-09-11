"""Hermetic tests for the ship-a-change reference CI watcher.

`.agents/skills/ship-a-change/reference/ci_watcher.py` is a launched code string,
not an importable module: the launching agent reads the file, string-substitutes
the placeholder assignments (REPO_ROOT / PR_NUMBER / CI_UTILS / WATCHER_ID), then
hands the result to `ava.watcher.launch`. These tests exercise that same
artifact the same way — substitute the placeholders, stub `ava` and `ci_utils`,
exec — so the settle semantics the template exists to guarantee stay locked
(2026-09-12, task #3158: a just-pushed PR read as a settled NO_CHECKS).
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
    """Records wake messages instead of delivering them."""

    def __init__(self) -> None:
        self.wakes: list[tuple[int, str]] = []
        recorded = self.wakes

        class _Agents:
            @staticmethod
            def send_message(agent_id: int, content: str) -> None:
                recorded.append((agent_id, content))

        self.agents = _Agents()


def _run_watcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, verdicts: list[Any]
) -> tuple[list[tuple[int, str]], list[Any]]:
    """Exec the substituted template; return its wakes and the polls it made.

    `verdicts` is how `check_ci` answers, one entry per poll; the last entry
    repeats if the watcher polls again.
    """
    statuses = _ci_status()
    polled: list[Any] = []

    def _check_ci(_pr: str | int) -> _Result:
        verdict = verdicts[min(len(polled), len(verdicts) - 1)]
        polled.append(verdict)
        return _Result(verdict)

    fake_ava = _FakeAva()
    monkeypatch.chdir(tmp_path)

    code = (
        _TEMPLATE.read_text()
        .replace('REPO_ROOT = ""', f'REPO_ROOT = "{tmp_path}"')
        .replace('PR_NUMBER = ""', 'PR_NUMBER = "1234"')
        .replace('CI_UTILS = ""', f'CI_UTILS = "{tmp_path}"')
        .replace("WATCHER_ID = 0", "WATCHER_ID = 42")
        # The retry window is behavior; its wall clock is not. Sleep-free polls
        # keep this test instant.
        .replace("CHECK_EVERY = 60", "CHECK_EVERY = 0")
        # The two imports are substituted rather than patched through
        # sys.modules: a stub left there leaks into fixture teardown, which
        # re-imports `ava` (same substitution the generated-watcher tests use).
        .replace("import ava\n", "")
        .replace("from ci_utils import CIStatus, check_ci  # noqa: E402\n", "")
    )
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
    return fake_ava.wakes, polled


def test_attach_window_does_not_wake_as_no_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reported bug (2026-09-12, PR #2249): the watcher starts right after
    the push, and the empty rollup while the checks attach must not be reported
    as a settled NO_CHECKS — the wake comes only once CI actually settles."""
    statuses = _ci_status()
    wakes, polled = _run_watcher(
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
    wakes, polled = _run_watcher(monkeypatch, tmp_path, [statuses.NO_CHECKS])

    assert len(polled) == 4  # the 3 retries + the poll that reports
    assert len(wakes) == 1
    assert "no_checks" in wakes[0][1]
    assert "re-polled 3 times" in wakes[0][1]


def test_settled_verdict_wakes_on_the_first_poll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Retries are confined to NO_CHECKS: a red PR wakes immediately."""
    statuses = _ci_status()
    wakes, polled = _run_watcher(monkeypatch, tmp_path, [statuses.FAILED])

    assert len(polled) == 1
    assert len(wakes) == 1
    assert "failed" in wakes[0][1]
