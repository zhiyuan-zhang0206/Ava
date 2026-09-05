"""shared.os_watchdog_probe — per-capability labels, crontab safety, plist wiring.

The probe is what breaks the "who watches the watchdog" recursion, so the things
worth pinning are the ones whose silent failure would put it back: two
capabilities on one box must get two INDEPENDENT jobs, the launchd job must
carry a PATH that can find the repo's tools, and a crontab rewrite must never clobber the
user's real crontab.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from shared import os_cron
from shared import os_watchdog_probe as probe


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _stable_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe, "_home_slug", lambda: "ava-deadbeef")


# --- labels ---------------------------------------------------------------


def test_label_is_distinct_per_capability() -> None:
    """A single box runs BOTH watchdogs; one shared label would mean one job
    supervising one capability and the other left unwatched — the exact
    collision that split the watchdog daemon in two."""
    gw = probe.probe_label("gateway", "ava-deadbeef")
    ar = probe.probe_label("agent-runner", "ava-deadbeef")
    assert gw != ar
    assert gw == "com.ava.ava-deadbeef.watchdog-probe.gateway"
    assert ar == "com.ava.ava-deadbeef.watchdog-probe.agent-runner"


def test_cron_marker_scopes_by_role_and_home() -> None:
    """The marker carries both, so a co-located second cluster's rewrite never
    removes this home's line and vice versa."""
    assert (
        probe._cron_marker("gateway", "ava-deadbeef") == "# ava-watchdog-probe.gateway.ava-deadbeef"
    )
    assert "agent-runner" in probe._cron_marker("agent-runner", "ava-deadbeef")


# --- launchd --------------------------------------------------------------


def test_plist_carries_path_env_so_the_respawn_can_find_ava(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """launchd hands a job a minimal PATH without Homebrew. The probe's whole
    job is to shell out to the repo's tools, so a plist without an injected PATH would fail
    exactly when it is needed."""
    monkeypatch.setattr(os_cron, "launchd_path_env", lambda: "/opt/homebrew/bin:/usr/bin")
    monkeypatch.setattr(probe, "ava_binary_path", lambda: "/x/ava")
    content = probe._plist_content("agent-runner", 60)
    assert "<key>PATH</key>" in content
    assert "/opt/homebrew/bin" in content


def test_plist_does_not_run_at_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """RunAtLoad would fire the probe during the very `ava start` that registers
    it, racing that start's own watchdog spawn."""
    monkeypatch.setattr(os_cron, "launchd_path_env", lambda: "/usr/bin")
    monkeypatch.setattr(probe, "ava_binary_path", lambda: "/x/ava")
    content = probe._plist_content("gateway", 60)
    assert "<key>RunAtLoad</key>\n    <false/>" in content


def test_plist_invokes_the_probe_for_its_own_role(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os_cron, "launchd_path_env", lambda: "/usr/bin")
    monkeypatch.setattr(probe, "ava_binary_path", lambda: "/x/ava")
    content = probe._plist_content("agent-runner", 90)
    assert "<string>watchdog-probe</string>" in content
    assert "<string>agent-runner</string>" in content
    assert "<integer>90</integer>" in content


def test_register_macos_writes_and_bootstraps(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os_cron, "launchd_path_env", lambda: "/usr/bin")
    monkeypatch.setattr(probe, "ava_binary_path", lambda: "/x/ava")
    calls: list[list[str]] = []

    def _run(cmd, **_kw):  # type: ignore[no-untyped-def]
        calls.append(cmd)  # pyright: ignore[reportUnknownArgumentType]
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr(probe.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]
    assert probe._register_macos("gateway", 60) == 0

    plist = (
        fake_home
        / "Library"
        / "LaunchAgents"
        / f"{probe.probe_label('gateway', 'ava-deadbeef')}.plist"
    )
    assert plist.exists()
    # bootout-then-bootstrap, so a changed interval actually takes effect.
    assert [c[1] for c in calls] == ["bootout", "bootstrap"]


def test_register_macos_reports_bootstrap_failure(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed bootstrap must surface as non-zero — the caller turns that into
    a RuntimeError so converge fails loudly instead of leaving the cluster
    silently unsupervised."""
    monkeypatch.setattr(os_cron, "launchd_path_env", lambda: "/usr/bin")
    monkeypatch.setattr(probe, "ava_binary_path", lambda: "/x/ava")

    def _run(cmd, **_kw):  # type: ignore[no-untyped-def]
        rc = 0 if cmd[1] == "bootout" else 1
        return type("R", (), {"returncode": rc, "stderr": "boom", "stdout": ""})()

    monkeypatch.setattr(probe.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]
    assert probe._register_macos("gateway", 60) == 1


def test_unregister_macos_removes_plist(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agents = fake_home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    plist = agents / f"{probe.probe_label('gateway', 'ava-deadbeef')}.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda *_a, **_k: type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})(),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert probe._unregister_macos("gateway", "ava-deadbeef") == 0
    assert not plist.exists()


# --- crontab --------------------------------------------------------------


def test_register_linux_skips_when_crontab_absent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hermetic bench / CI container has no crontab. That is a missing
    capability, not a bring-up failure."""
    monkeypatch.setattr(shutil, "which", lambda _n: None)  # pyright: ignore[reportUnknownArgumentType]
    assert probe._register_linux("gateway", 60) == 0
    assert "crontab not installed" in capsys.readouterr().out


def test_register_linux_aborts_when_crontab_read_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only the benign "no crontab for <user>" may be read as an empty crontab.
    Any other failure and we must NOT rewrite, or the user's real crontab is
    replaced by just our line."""
    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/crontab")  # pyright: ignore[reportUnknownArgumentType]

    def _run(cmd, **_kw):  # type: ignore[no-untyped-def]
        assert cmd != ["crontab", "-"], "must not write after a failed read"
        return type("R", (), {"returncode": 1, "stderr": "permission denied", "stdout": ""})()

    monkeypatch.setattr(probe.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]
    assert probe._register_linux("gateway", 60) == 1
    assert "avoid clobbering" in capsys.readouterr().err


def test_register_linux_preserves_foreign_lines_and_replaces_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idempotent: this role's own line is replaced, everything else — including
    the OTHER capability's probe line — survives."""
    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/crontab")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(probe, "ava_binary_path", lambda: "/x/ava")
    other = f"*/1 * * * * /x/ava cluster watchdog-probe --role agent-runner  {probe._cron_marker('agent-runner', 'ava-deadbeef')}"
    existing = "\n".join(
        [
            "0 3 * * * /usr/bin/backup.sh",
            other,
            f"*/1 * * * * /old/ava cluster watchdog-probe --role gateway  {probe._cron_marker('gateway', 'ava-deadbeef')}",
        ]
    )
    written: dict[str, str] = {}

    def _run(cmd, **kw):  # type: ignore[no-untyped-def]
        if cmd == ["crontab", "-"]:
            written["body"] = kw["input"]
            return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": existing})()

    monkeypatch.setattr(probe.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]
    assert probe._register_linux("gateway", 60) == 0

    body = written["body"]
    assert "/usr/bin/backup.sh" in body  # foreign line untouched
    assert other in body  # the other capability's job untouched
    assert "/old/ava" not in body  # this role's stale line replaced
    assert body.count(probe._cron_marker("gateway", "ava-deadbeef")) == 1


def test_register_linux_rounds_sub_minute_interval_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """crontab cannot express seconds. A 5s request becomes */1, not */0 (which
    crond rejects) and not a silent "every minute" that hides the mismatch."""
    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/crontab")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(probe, "ava_binary_path", lambda: "/x/ava")
    written: dict[str, str] = {}

    def _run(cmd, **kw):  # type: ignore[no-untyped-def]
        if cmd == ["crontab", "-"]:
            written["body"] = kw["input"]
            return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()
        return type("R", (), {"returncode": 1, "stderr": "no crontab for u", "stdout": ""})()

    monkeypatch.setattr(probe.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]
    assert probe._register_linux("gateway", 5) == 0
    assert written["body"].startswith("*/1 * * * *")


def test_unregister_linux_is_a_noop_without_our_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/crontab")  # pyright: ignore[reportUnknownArgumentType]
    wrote: list[object] = []

    def _run(cmd, **_kw):  # type: ignore[no-untyped-def]
        if cmd == ["crontab", "-"]:
            wrote.append(cmd)  # pyright: ignore[reportUnknownArgumentType]
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": "0 3 * * * backup\n"})()

    monkeypatch.setattr(probe.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]
    assert probe._unregister_linux("gateway", "ava-deadbeef") == 0
    assert wrote == []


def test_register_linux_holds_crontab_lock_around_rmw(monkeypatch: pytest.MonkeyPatch) -> None:
    """The crontab read-modify-write runs under crontab_lock (audit 2026-08-08
    P1): two co-located clusters — or a gateway restart racing a converge —
    must not interleave their read-filter-write and silently drop each
    other's line. For the watchdog-probe that line is the last line of
    supervision; losing it leaves restarter/ops/browser sessions down with
    nobody watching."""
    import contextlib

    import shared.platform as platform_mod

    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/crontab")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(probe, "ava_binary_path", lambda: "/x/ava")
    seen: dict[str, bool] = {}
    lock_paths: list[str] = []

    @contextlib.contextmanager
    def recording_lock(path):
        lock_paths.append(str(path))  # pyright: ignore[reportUnknownArgumentType]
        seen["in_lock"] = True
        try:
            yield
        finally:
            seen["in_lock"] = False

    monkeypatch.setattr(platform_mod, "file_lock", recording_lock)  # pyright: ignore[reportUnknownArgumentType]

    def _run(cmd, **kw):  # type: ignore[no-untyped-def]
        if cmd == ["crontab", "-l"]:
            seen["read_in_lock"] = seen["in_lock"]
            return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()
        if cmd == ["crontab", "-"]:
            seen["write_in_lock"] = seen["in_lock"]
            return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()
        raise AssertionError(f"unexpected crontab invocation: {cmd}")

    monkeypatch.setattr(probe.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]
    assert probe._register_linux("gateway", 60) == 0
    assert seen.get("read_in_lock") is True, "crontab -l must run under the lock"
    assert seen.get("write_in_lock") is True, "crontab - must run under the lock"
    # the lock lives OUTSIDE $AVA_HOME so two clusters on one host serialize
    assert lock_paths and lock_paths[0].endswith(".ava-crontab.lock")
