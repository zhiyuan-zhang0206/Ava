"""The session-level leak guard's inventory (`tests/_os_jobs.py`).

`pytest_sessionfinish` diffs `host_ava_os_jobs()` against the snapshot taken at
conftest import and fails the run on anything new. These tests cover what that
diff can see and what it is allowed to delete — the guard itself only runs at
session end, where a self-test cannot observe it.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from tests import _os_jobs


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    # No crontab / schtasks on the fake host — this fixture isolates the launchd half.
    monkeypatch.setattr(_os_jobs, "_crontab_jobs", set)
    monkeypatch.setattr(_os_jobs, "_schtasks_jobs", set)
    return tmp_path


def _plant(home: Path, label: str) -> Path:
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    p = agents / f"{label}.plist"
    p.write_text("<plist/>")
    return p


def test_sees_a_planted_launchagent(fake_home: Path) -> None:
    _plant(fake_home, "com.ava.ava_e2e_home_1_2-abcd1234.health-probe")
    assert _os_jobs.host_ava_os_jobs() == frozenset(
        {"launchd:com.ava.ava_e2e_home_1_2-abcd1234.health-probe.plist"}
    )


def test_ignores_plists_that_are_not_ours(fake_home: Path) -> None:
    """`com.ava.*` only — a developer's unrelated LaunchAgents must never show up
    as a leak (and so never be swept)."""
    _plant(fake_home, "com.example.something")
    assert _os_jobs.host_ava_os_jobs() == frozenset()


def test_no_launchagents_dir_is_not_an_error(fake_home: Path) -> None:
    assert _os_jobs.host_ava_os_jobs() == frozenset()


@pytest.mark.parametrize(
    ("job", "owned"),
    [
        ("launchd:com.ava.ava_e2e_home_9_9-aaaaaaaa.health-probe.plist", True),
        ("launchd:com.ava.ava_test_home_9_9_x-bbbbbbbb.autostart.plist", True),
        # A live prod cluster's jobs — an operator running `ava start` in another
        # terminal mid-suite. Reported if new, never deleted.
        ("launchd:com.ava.ava-56a51359.watchdog-probe.agent-runner.plist", False),
        (
            "crontab:*/5 * * * * AVA_HOME=/home/u/.ava /u/ava cluster health-probe "
            "--auto-rollback --threshold 3 # ava-health-probe.ava-56a51359",
            False,
        ),
        ("schtasks:\\Ava\\ava_e2e_home_1_2-abcd1234\\health-probe", True),
    ],
)
def test_only_this_suites_homes_are_sweepable(job: str, owned: bool) -> None:
    assert _os_jobs.is_test_owned_job(job) is owned


def test_remove_launchd_job_boots_out_then_unlinks(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    label = "com.ava.ava_e2e_home_1_2-abcd1234.health-probe"
    plist = _plant(fake_home, label)
    calls: list[list[str]] = []

    def _run(cmd, **_kw):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_os_jobs.subprocess, "run", _run)
    _os_jobs.remove_os_job(f"launchd:{label}.plist")

    assert not plist.exists()
    assert calls and calls[0][:2] == ["launchctl", "bootout"]
    assert calls[0][2].endswith(f"/{label}")


def test_remove_crontab_job_drops_only_that_line(monkeypatch: pytest.MonkeyPatch) -> None:
    mine = "*/5 * * * * /x/ava cluster health-probe # ava-health-probe.ava_e2e_home_1_2-a"
    theirs = "0 3 * * * /usr/bin/backup"
    written: dict[str, str] = {}

    def _run(cmd, **kw):  # type: ignore[no-untyped-def]
        if cmd[:2] == ["crontab", "-l"]:
            return types.SimpleNamespace(returncode=0, stdout=f"{theirs}\n{mine}\n", stderr="")
        if cmd == ["crontab", "-"]:
            written["input"] = kw.get("input", "")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_os_jobs.subprocess, "run", _run)
    _os_jobs.remove_os_job(f"crontab:{mine}")

    assert written["input"] == f"{theirs}\n"
