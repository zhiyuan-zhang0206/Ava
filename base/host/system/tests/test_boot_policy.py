from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.host.system import boot_policy


@pytest.fixture()
def boot_time(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    clock = [1_000_000.0]
    monkeypatch.setattr(boot_policy.psutil, "boot_time", lambda: clock[0])
    return clock


def test_attempts_count_up_within_one_boot(tmp_path: Path, boot_time: list[float]) -> None:
    assert [boot_policy.begin_attempt(tmp_path) for _ in range(3)] == [1, 2, 3]


def test_the_count_survives_clock_jitter_of_the_boot_time(
    tmp_path: Path, boot_time: list[float]
) -> None:
    boot_policy.begin_attempt(tmp_path)
    boot_time[0] += 1.5
    assert boot_policy.begin_attempt(tmp_path) == 2


def test_a_new_boot_starts_the_count_again(tmp_path: Path, boot_time: list[float]) -> None:
    for _ in range(boot_policy.BOOT_MAX_ATTEMPTS):
        boot_policy.begin_attempt(tmp_path)
    boot_time[0] += 3600
    assert boot_policy.begin_attempt(tmp_path) == 1


def test_success_clears_the_count(tmp_path: Path, boot_time: list[float]) -> None:
    boot_policy.begin_attempt(tmp_path)
    boot_policy.clear_attempts(tmp_path)
    boot_policy.clear_attempts(tmp_path)
    assert boot_policy.begin_attempt(tmp_path) == 1


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"attempts": 4}', '{"boot_time": "x"}'])
def test_an_unreadable_record_counts_again_instead_of_failing_every_boot(
    tmp_path: Path, boot_time: list[float], content: str
) -> None:
    path = tmp_path / "run" / "boot-attempts.json"
    path.parent.mkdir()
    path.write_text(content)
    assert boot_policy.begin_attempt(tmp_path) == 1
    assert json.loads(path.read_text())["attempts"] == 1


def test_the_budget_is_spent_by_the_last_allowed_attempt() -> None:
    assert not boot_policy.budget_spent(boot_policy.BOOT_MAX_ATTEMPTS - 1)
    assert boot_policy.budget_spent(boot_policy.BOOT_MAX_ATTEMPTS)
    assert boot_policy.budget_spent(boot_policy.BOOT_MAX_ATTEMPTS + 1)


@pytest.mark.parametrize(("macos", "expected"), [(True, 0), (False, 75)])
def test_only_launchd_needs_a_zero_exit_to_stop_retrying(
    monkeypatch: pytest.MonkeyPatch, macos: bool, expected: int
) -> None:
    monkeypatch.setattr(boot_policy, "is_macos", lambda: macos)
    assert boot_policy.exit_code_after_budget(75) == expected
