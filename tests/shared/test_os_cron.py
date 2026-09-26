"""shared.os_cron — home-slug labels, launchd ownership, crontab safety."""

from __future__ import annotations

import os
import types
from pathlib import Path

import pytest

from shared import os_cron


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _plant_plist(fake_home: Path, label: str) -> Path:
    agents = fake_home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    p = agents / f"{label}.plist"
    p.write_text("<plist/>")
    return p


def _record_launchctl(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def _run(cmd, **_kw):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))  # pyright: ignore[reportUnknownArgumentType]
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(os_cron.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]
    return calls


def _desired_plist(_interval_s: int) -> str:
    return "<desired-plist/>"


def _never_descendant(_label: str) -> bool:
    return False


def test_register_macos_never_reloads_its_own_launchd_job(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registration cannot boot out the job containing its own process."""
    slug = "ava-t-cafe0123"
    label = f"com.ava.{slug}.health-probe"
    plist = _plant_plist(fake_home, label)
    plist.write_text("<old-plist/>")
    monkeypatch.setenv("XPC_SERVICE_NAME", label)
    monkeypatch.setattr(os_cron, "_home_slug", lambda: slug)
    monkeypatch.setattr(os_cron, "_launchd_plist_content", _desired_plist)
    calls = _record_launchctl(monkeypatch)

    assert os_cron._register_macos(300) == 0
    assert plist.read_text() == "<old-plist/>"
    assert calls == []


def test_register_macos_still_reloads_from_another_launchd_job(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The self-protection is label-specific: boot autostart also runs
    ``ava start`` under launchd and must still converge the health probe."""
    slug = "ava-t-cafe0123"
    health_label = f"com.ava.{slug}.health-probe"
    plist = _plant_plist(fake_home, health_label)
    plist.write_text("<old-plist/>")
    monkeypatch.setenv("XPC_SERVICE_NAME", f"com.ava.{slug}.autostart")
    monkeypatch.setattr(os_cron, "_home_slug", lambda: slug)
    monkeypatch.setattr(os_cron, "_launchd_plist_content", _desired_plist)
    monkeypatch.setattr(os_cron, "descends_from_launchd_job", _never_descendant)
    calls = _record_launchctl(monkeypatch)

    assert os_cron._register_macos(300) == 0
    assert plist.read_text() == "<desired-plist/>"
    assert [call[1] for call in calls] == ["bootout", "bootstrap"]


def test_register_linux_aborts_when_crontab_read_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `crontab -l` failure that is NOT the benign 'no crontab for <user>'
    (permissions, broken cron) must abort — proceeding would rewrite the user's
    real crontab from an empty read."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/crontab")  # pyright: ignore[reportUnknownArgumentType]

    def _run(cmd, **_kw):  # type: ignore[no-untyped-def]
        if cmd[:2] == ["crontab", "-l"]:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="permission denied")
        raise AssertionError(f"must not reach a crontab write: {cmd}")

    monkeypatch.setattr(os_cron.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]
    assert os_cron._register_linux(300) == 1
    assert "avoid clobbering" in capsys.readouterr().err


def test_register_linux_treats_no_crontab_as_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/crontab")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(os_cron, "ava_binary_path", lambda: "/x/ava")
    writes: dict[str, str] = {}

    def _run(cmd, **kw):  # type: ignore[no-untyped-def]
        if cmd[:2] == ["crontab", "-l"]:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="no crontab for u")
        if cmd == ["crontab", "-"]:
            writes["input"] = kw.get("input", "")  # pyright: ignore[reportUnknownMemberType]
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(os_cron.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]
    assert os_cron._register_linux(300) == 0
    assert "health-probe" in writes["input"]


def test_launchd_path_env_includes_brew_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    """launchd hands a job a minimal PATH without Homebrew's bin. Both LaunchAgents
    this repo writes (autostart, watchdog probe) shell out to the repo's tools, so the composed
    PATH must carry the brew prefix, the dir holding `ava`, and the system dirs."""
    import shutil

    monkeypatch.setattr(os_cron, "ava_binary_path", lambda: "/Users/x/.local/bin/ava")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: "/opt/homebrew/bin/brew" if name == "brew" else None,  # pyright: ignore[reportUnknownArgumentType]
    )
    path = os_cron.launchd_path_env()
    assert "/opt/homebrew/bin" in path  # brew bin (launchd omits it)
    assert "/Users/x/.local/bin" in path  # dir holding `ava`
    assert "/usr/bin" in path  # base system dirs


def test_launchd_path_env_falls_back_without_brew(monkeypatch: pytest.MonkeyPatch) -> None:
    """No brew on PATH (a fresh box, or launchd's own stripped env): fall back to
    the standard Apple-silicon / Intel prefixes rather than emitting no brew dir."""
    import shutil

    monkeypatch.setattr(os_cron, "ava_binary_path", lambda: "/Users/x/.local/bin/ava")
    monkeypatch.setattr(shutil, "which", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]
    path = os_cron.launchd_path_env()
    assert "/opt/homebrew/bin" in path
    assert "/usr/local/bin" in path


def test_launchd_path_env_deduplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ava` living in the brew prefix must not produce a doubled entry."""
    import shutil

    monkeypatch.setattr(os_cron, "ava_binary_path", lambda: "/opt/homebrew/bin/ava")
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: "/opt/homebrew/bin/brew" if name == "brew" else None,  # pyright: ignore[reportUnknownArgumentType]
    )
    assert os_cron.launchd_path_env().split(":").count("/opt/homebrew/bin") == 1


# ---------------------------------------------------------------------------
# Per-cluster scoping of the crontab entry
# ---------------------------------------------------------------------------
# The launchd label always carried the home slug; the crontab line carried
# nothing, so every cluster's health-probe entry looked identical and one
# cluster's register/unregister rewrote them all.


def _crontab_stub(
    monkeypatch: pytest.MonkeyPatch,
    existing: str,
    writes: dict[str, str],
    *,
    read_rc: int = 0,
    read_error: str = "",
    write_rc: int = 0,
    write_error: str = "",
) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/crontab")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(os_cron, "ava_binary_path", lambda: "/x/ava")

    def _run(cmd, **kw):  # type: ignore[no-untyped-def]
        if cmd[:2] == ["crontab", "-l"]:
            return types.SimpleNamespace(returncode=read_rc, stdout=existing, stderr=read_error)
        if cmd == ["crontab", "-"]:
            writes["input"] = kw.get("input", "")  # pyright: ignore[reportUnknownMemberType]
        return types.SimpleNamespace(returncode=write_rc, stdout="", stderr=write_error)

    monkeypatch.setattr(os_cron.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]


def test_register_linux_stamps_the_owning_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os_cron, "_home_slug", lambda: "ava-mine")
    writes: dict[str, str] = {}
    _crontab_stub(monkeypatch, "", writes)

    assert os_cron._register_linux(300) == 0
    assert "# ava-health-probe.ava-mine" in writes["input"]
    assert "--auto-rollback" not in writes["input"]
    assert "--threshold" not in writes["input"]


def test_register_linux_leaves_another_clusters_line_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two co-located clusters each own a health probe; registering one must not
    silently unregister the other."""
    monkeypatch.setattr(os_cron, "_home_slug", lambda: "ava-mine")
    theirs = "*/5 * * * * /y/ava cluster health-probe --auto-rollback --threshold 3 # ava-health-probe.ava-theirs"
    writes: dict[str, str] = {}
    _crontab_stub(monkeypatch, f"0 3 * * * backup\n{theirs}\n", writes)

    assert os_cron._register_linux(300) == 0
    body = writes["input"]
    assert theirs in body
    assert body.count("# ava-health-probe.ava-mine") == 1
    assert "0 3 * * * backup" in body


def test_register_linux_clears_both_unmarked_legacy_forms_and_reports_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(os_cron, "_home_slug", lambda: "ava-mine")
    legacy_command = "*/5 * * * * /old/ava cluster health-probe --threshold 2"
    legacy_script = "*/5 * * * * /old/health-probe-cron"
    foreign = "*/5 * * * * /other/ava cluster health-probe # ava-health-probe.ava-other"
    writes: dict[str, str] = {}
    _crontab_stub(monkeypatch, f"{legacy_command}\n{legacy_script}\n{foreign}\n", writes)

    assert os_cron._register_linux(300) == 0
    assert capsys.readouterr() == (
        "  . crontab entry added (every 5 min)\n",
        "",
    )
    assert legacy_command not in writes["input"]
    assert legacy_script not in writes["input"]
    assert foreign in writes["input"]
    assert writes["input"].count("# ava-health-probe.ava-mine") == 1


@pytest.mark.parametrize(
    ("read_rc", "read_error", "write_rc", "write_error", "expected_out", "expected_err"),
    [
        (
            1,
            "permission denied",
            0,
            "",
            "",
            "  * crontab -l failed (permission denied); skipping health-probe registration to avoid clobbering the crontab\n",
        ),
        (0, "", 1, "disk full", "", "  * crontab update failed: disk full\n"),
    ],
)
def test_register_linux_failure_messages_and_rc(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    read_rc: int,
    read_error: str,
    write_rc: int,
    write_error: str,
    expected_out: str,
    expected_err: str,
) -> None:
    writes: dict[str, str] = {}
    _crontab_stub(
        monkeypatch,
        "",
        writes,
        read_rc=read_rc,
        read_error=read_error,
        write_rc=write_rc,
        write_error=write_error,
    )

    assert os_cron._register_linux(300) == 1
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == (expected_out, expected_err)
    assert ("input" in writes) == (read_rc == 0)


def test_register_linux_missing_crontab_message_and_rc(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(os_cron.shutil, "which", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]

    assert os_cron._register_linux(300) == 0
    assert capsys.readouterr() == (
        "  ! health probe cron: crontab not installed on this host (skipping); cluster runs without a health-probe cron\n",
        "",
    )


def test_unregister_linux_removes_only_the_named_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ava cluster destroy --path <other>` must not take this host's own probe
    down with it."""
    mine = "*/5 * * * * /x/ava cluster health-probe --auto-rollback --threshold 3 # ava-health-probe.ava-mine"
    theirs = "*/5 * * * * /y/ava cluster health-probe --auto-rollback --threshold 3 # ava-health-probe.ava-theirs"
    writes: dict[str, str] = {}
    _crontab_stub(monkeypatch, f"{mine}\n{theirs}\n", writes)

    assert os_cron._unregister_linux("ava-theirs") == 0
    body = writes["input"]
    assert mine in body
    assert theirs not in body


def test_unregister_linux_still_clears_a_pre_marker_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Markers are new. A line written before them cannot be attributed to a
    cluster, but the old register path rewrote every health-probe line it found,
    so a host held at most one — leaving it behind would strand a job pointing at
    a home that may no longer have a cluster."""
    legacy = "*/5 * * * * /x/ava cluster health-probe --auto-rollback --threshold 3"
    writes: dict[str, str] = {}
    _crontab_stub(monkeypatch, f"{legacy}\n0 3 * * * backup\n", writes)

    assert os_cron._unregister_linux("ava-anything") == 0
    assert legacy not in writes["input"]
    assert "0 3 * * * backup" in writes["input"]


def test_unregister_linux_ignores_the_watchdog_probe_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watchdog probe writes its own crontab lines with its own marker; a
    health-probe unregister must not sweep them away."""
    probe_line = "*/1 * * * * /x/ava cluster watchdog-probe --role gateway  # ava-watchdog-probe.gateway.ava-mine"
    writes: dict[str, str] = {}
    _crontab_stub(monkeypatch, f"{probe_line}\n", writes)

    assert os_cron._unregister_linux("ava-mine") == 0
    assert writes == {}  # nothing matched, so the crontab was never rewritten


@pytest.mark.parametrize(
    ("read_rc", "existing", "write_rc", "expected_out", "should_write"),
    [
        (1, "", 0, "  . no crontab to unregister\n", False),
        (0, "0 3 * * * backup\n", 0, "  . no Ava health-probe entry found in crontab\n", False),
        (
            0,
            "*/5 * * * * /x/ava cluster health-probe # ava-health-probe.ava-mine\n",
            0,
            "  . crontab entry removed\n",
            True,
        ),
        (0, "*/5 * * * * /x/ava cluster health-probe # ava-health-probe.ava-mine\n", 1, "", True),
    ],
)
def test_unregister_linux_messages_and_success_gated_removal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    read_rc: int,
    existing: str,
    write_rc: int,
    expected_out: str,
    should_write: bool,
) -> None:
    writes: dict[str, str] = {}
    _crontab_stub(monkeypatch, existing, writes, read_rc=read_rc, write_rc=write_rc)

    assert os_cron._unregister_linux("ava-mine") == 0
    assert capsys.readouterr() == (expected_out, "")
    assert ("input" in writes) == should_write


def test_unregister_macos_removes_only_the_named_clusters_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Filesystem-level proof of the scoping: two clusters' plists are on disk,
    and unregistering one leaves the other's file untouched.

    This is the failure mode `ava cluster destroy --path <worktree>` had — run
    from the prod checkout it removed the plist of whichever home the PROCESS
    resolved, i.e. prod's.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    mine = agents / "com.ava.ava-mine.health-probe.plist"
    theirs = agents / "com.ava.ava-theirs.health-probe.plist"
    mine.write_text("<plist/>")
    theirs.write_text("<plist/>")

    booted: list[str] = []

    def _run(cmd, **_kw):  # type: ignore[no-untyped-def]
        booted.append(cmd[-1])  # pyright: ignore[reportUnknownArgumentType]
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(os_cron.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]

    assert os_cron._unregister_macos("ava-theirs") == 0
    assert not theirs.exists()
    assert mine.exists()
    # ...and the launchd job booted out was the target's, not this process's.
    assert booted == [f"gui/{os.getuid()}/com.ava.ava-theirs.health-probe"]


# ── registration guard: worktree checkout must not register prod's probe ──


def _fake_backend(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the platform backend's register_cron to record calls."""
    calls: list[str] = []

    class _FakeBackend:
        def register_cron(self, interval_s: int) -> None:  # type: ignore[no-untyped-def]
            calls.append(str(interval_s))

    monkeypatch.setattr("shared.platform_backend.get_backend", _FakeBackend)
    return calls


def test_register_refused_for_worktree_checkout_against_prod_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task #1025: a worktree process (prod home + non-prod checkout) must not
    register the prod health-probe plist — the 2026-08-07 accident where a
    worktree-venv debug script rewrote the plist and the probe auto-rolled-back
    the cluster."""
    prod_home = Path("~/.ava").expanduser()
    worktree = Path("~/Ava/.worktrees/ava-2890-r4").expanduser()
    monkeypatch.setattr("shared.paths.ava_home", lambda: Path(prod_home))
    monkeypatch.setattr("shared.paths.repo_root", lambda: Path(worktree))
    monkeypatch.setattr(os_cron, "os_jobs_enabled", lambda: True)
    calls = _fake_backend(monkeypatch)

    os_cron.register_os_cron()

    assert calls == []  # backend never called; registration refused


def test_register_allowed_from_prod_anchored_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prod home's own anchored checkout registers normally."""
    prod_home = Path("~/.ava").expanduser()
    prod_source = Path("~/.ava/source").expanduser()
    monkeypatch.setattr("shared.paths.ava_home", lambda: Path(prod_home))
    monkeypatch.setattr("shared.paths.repo_root", lambda: Path(prod_source))
    monkeypatch.setattr(os_cron, "os_jobs_enabled", lambda: True)
    calls = _fake_backend(monkeypatch)

    os_cron.register_os_cron()

    assert calls == ["300"]


def test_register_allowed_for_non_prod_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dev cluster's own home + checkout is its own business — allowed."""
    dev_home = tmp_path / ".ava-dev"
    monkeypatch.setattr("shared.paths.ava_home", lambda: dev_home)
    monkeypatch.setattr("shared.paths.repo_root", lambda: tmp_path / "dev-src")
    monkeypatch.setattr(os_cron, "os_jobs_enabled", lambda: True)
    calls = _fake_backend(monkeypatch)

    os_cron.register_os_cron()

    assert calls == ["300"]


def test_register_macos_defers_when_ancestry_proves_the_job(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On current macOS an exec'd descendant reads XPC_SERVICE_NAME="0", not the
    label (2026-09-17 recurrence), so the live process-tree check must defer
    even when the environment value is "0"."""
    slug = "ava-t-cafe0123"
    label = f"com.ava.{slug}.health-probe"
    plist = _plant_plist(fake_home, label)
    plist.write_text("<old-plist/>")
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")
    monkeypatch.setattr(os_cron, "_home_slug", lambda: slug)
    monkeypatch.setattr(os_cron, "_launchd_plist_content", _desired_plist)

    def _is_current(candidate: str) -> bool:
        return candidate == label

    monkeypatch.setattr(os_cron, "descends_from_launchd_job", _is_current)
    calls = _record_launchctl(monkeypatch)

    assert os_cron._register_macos(300) == 0
    assert plist.read_text() == "<old-plist/>"
    assert calls == []


def test_register_macos_reloads_when_env_reads_zero_but_tree_is_external(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The "0" descendant reading must not block a legitimate external converge
    from applying a pending spec change."""
    slug = "ava-t-cafe0123"
    label = f"com.ava.{slug}.health-probe"
    plist = _plant_plist(fake_home, label)
    plist.write_text("<old-plist/>")
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")
    monkeypatch.setattr(os_cron, "_home_slug", lambda: slug)
    monkeypatch.setattr(os_cron, "_launchd_plist_content", _desired_plist)
    monkeypatch.setattr(os_cron, "descends_from_launchd_job", _never_descendant)
    calls = _record_launchctl(monkeypatch)

    assert os_cron._register_macos(300) == 0
    assert plist.read_text() == "<desired-plist/>"
    assert [call[1] for call in calls] == ["bootout", "bootstrap"]
