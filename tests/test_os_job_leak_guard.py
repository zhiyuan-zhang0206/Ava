"""The session-level leak guard's inventory (`tests/_os_jobs.py`).

`pytest_sessionfinish` diffs `host_ava_os_jobs()` against the snapshot taken at
provisioning-plugin import and fails the run on anything new or changed. These
tests cover what that diff can see — the guard itself only runs at session end,
where a self-test cannot observe it. It removes nothing: a job's label names the
job, not a home, so a leaked one replaced the host's real job.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import _os_jobs


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    # No crontab on the fake host — this fixture isolates the launchd half.
    monkeypatch.setattr(_os_jobs, "_crontab_jobs", set)
    return tmp_path


def _plant(home: Path, label: str, body: str = "<plist/>") -> Path:
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    p = agents / f"{label}.plist"
    p.write_text(body)
    return p


def test_sees_a_planted_launchagent(fake_home: Path) -> None:
    _plant(fake_home, "com.ava.autostart")
    (job,) = _os_jobs.host_ava_os_jobs()
    assert job.startswith("launchd:com.ava.autostart.plist@")


def test_a_rewritten_job_is_a_new_inventory_entry(fake_home: Path) -> None:
    """A fixed label means a leak overwrites the host's real job rather than
    adding a second one, so the id must change with the plist's content."""
    plist = _plant(fake_home, "com.ava.logs-maintenance", "<real-plist/>")
    before = _os_jobs.host_ava_os_jobs()
    assert _os_jobs.host_ava_os_jobs() == before
    plist.write_text("<a-test-wrote-this/>")
    after = _os_jobs.host_ava_os_jobs()
    assert after != before and len(after) == 1


def test_ignores_plists_that_are_not_ours(fake_home: Path) -> None:
    """`com.ava.*` only — a developer's unrelated LaunchAgents must never show up
    as a leak."""
    _plant(fake_home, "com.example.something")
    assert _os_jobs.host_ava_os_jobs() == frozenset()


def test_no_launchagents_dir_is_not_an_error(fake_home: Path) -> None:
    assert _os_jobs.host_ava_os_jobs() == frozenset()


def test_only_ava_marked_crontab_lines_are_inventoried(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    mine = "*/5 * * * * /x/ava cluster health-probe # ava-health-probe"
    theirs = "0 3 * * * /usr/bin/backup"

    def _run(cmd, **_kw):  # type: ignore[no-untyped-def]
        assert list(cmd) == ["crontab", "-l"]
        return types.SimpleNamespace(returncode=0, stdout=f"{theirs}\n{mine}\n", stderr="")

    monkeypatch.setattr(_os_jobs.subprocess, "run", _run)
    assert _os_jobs._crontab_jobs() == {f"crontab:{mine}"}
