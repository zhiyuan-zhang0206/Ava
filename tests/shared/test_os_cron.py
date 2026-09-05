"""shared.os_cron — home-slug labels, legacy-label cleanup, crontab safety.

The cleanup path is transitional (path-only cutover): a pre-cutover install left
`com.ava.<cluster-name>.<kind>` launchd jobs; the register paths boot them out +
delete the plists, scoped to THIS home's own legacy tokens so a co-located
cluster's job is never touched.
"""

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


def _desired_plist(_interval_s: int, _threshold: int) -> str:
    return "<desired-plist/>"


def test_legacy_tokens_cover_convention_and_retired_cluster_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tokens = the `~/.ava-<x>` convention name PLUS whatever the retired
    `$AVA_HOME/cluster` file says (an explicitly-named pre-cutover home labeled
    its jobs by that name, not the convention)."""
    home = tmp_path / ".ava-t"
    home.mkdir()
    (home / "cluster").write_text("customname\n")
    monkeypatch.setattr("shared.paths.ava_home", lambda: home)
    assert os_cron._legacy_label_tokens() == ["t", "customname"]


def test_cleanup_boots_out_and_removes_legacy_plist(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ACTIVE cleanup path: a legacy plist present → launchctl bootout with
    the legacy label + the plist unlinked."""
    monkeypatch.setattr(os_cron, "_legacy_label_tokens", lambda: ["t"])
    plist = _plant_plist(fake_home, "com.ava.t.health-probe")
    calls: list[list[str]] = []

    def _run(cmd, **_kw):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))  # pyright: ignore[reportUnknownArgumentType]
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(os_cron.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]
    os_cron.cleanup_legacy_macos_job("health-probe")

    assert not plist.exists()
    assert any("bootout" in c and c[-1].endswith("com.ava.t.health-probe") for c in calls)
    assert "removed legacy launchd job" in capsys.readouterr().out


def test_cleanup_bootout_failure_never_claims_success(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(os_cron, "_legacy_label_tokens", lambda: ["t"])
    plist = _plant_plist(fake_home, "com.ava.t.autostart")
    monkeypatch.setattr(
        os_cron.subprocess,
        "run",
        lambda *_a, **_k: types.SimpleNamespace(returncode=3, stdout="", stderr="boom"),  # pyright: ignore[reportUnknownArgumentType]
    )
    os_cron.cleanup_legacy_macos_job("autostart")

    assert not plist.exists()  # the durable part still happens
    captured = capsys.readouterr()
    assert "removed legacy launchd job" not in captured.out  # no success claim
    assert "bootout rc=3" in captured.err and "boom" in captured.err


def test_cleanup_noop_without_legacy_plist(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os_cron, "_legacy_label_tokens", lambda: ["t"])
    called: list[object] = []
    monkeypatch.setattr(os_cron.subprocess, "run", lambda *a, **_k: called.append(a))  # pyright: ignore[reportUnknownArgumentType]
    os_cron.cleanup_legacy_macos_job("health-probe")
    assert called == []


def test_register_macos_never_reloads_its_own_launchd_job(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A health-probe-triggered rollback runs ``ava start`` below the probe's
    launchd job. Reloading that label would terminate the rollback itself before
    it can clear the update lease and resume the cluster."""
    slug = "ava-t-cafe0123"
    label = f"com.ava.{slug}.health-probe"
    plist = _plant_plist(fake_home, label)
    plist.write_text("<old-plist/>")
    monkeypatch.setenv("XPC_SERVICE_NAME", label)
    monkeypatch.setattr(os_cron, "_home_slug", lambda: slug)
    monkeypatch.setattr(os_cron, "_launchd_plist_content", _desired_plist)
    calls = _record_launchctl(monkeypatch)

    assert os_cron._register_macos(300, 3) == 0
    assert plist.read_text() == "<old-plist/>"
    assert calls == []


def test_register_macos_never_relabels_its_own_legacy_launchd_job(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-path-only probe can execute upgraded code. Its relabel cleanup is
    still a self-bootout and must wait for an external converge."""
    slug = "ava-t-cafe0123"
    legacy_label = "com.ava.t.health-probe"
    legacy_plist = _plant_plist(fake_home, legacy_label)
    desired_plist = fake_home / "Library" / "LaunchAgents" / f"com.ava.{slug}.health-probe.plist"
    monkeypatch.setenv("XPC_SERVICE_NAME", legacy_label)
    monkeypatch.setattr(os_cron, "_home_slug", lambda: slug)
    monkeypatch.setattr(os_cron, "_legacy_label_tokens", lambda: ["t"])
    monkeypatch.setattr(os_cron, "_launchd_plist_content", _desired_plist)
    calls = _record_launchctl(monkeypatch)

    assert os_cron._register_macos(300, 3) == 0
    assert legacy_plist.exists()
    assert not desired_plist.exists()
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
    calls = _record_launchctl(monkeypatch)

    assert os_cron._register_macos(300, 3) == 0
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
    assert os_cron._register_linux(300, 3) == 1
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
    assert os_cron._register_linux(300, 3) == 0
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


def _crontab_stub(monkeypatch: pytest.MonkeyPatch, existing: str, writes: dict[str, str]) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/crontab")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(os_cron, "ava_binary_path", lambda: "/x/ava")

    def _run(cmd, **kw):  # type: ignore[no-untyped-def]
        if cmd[:2] == ["crontab", "-l"]:
            return types.SimpleNamespace(returncode=0, stdout=existing, stderr="")
        if cmd == ["crontab", "-"]:
            writes["input"] = kw.get("input", "")  # pyright: ignore[reportUnknownMemberType]
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(os_cron.subprocess, "run", _run)  # pyright: ignore[reportUnknownArgumentType]


def test_register_linux_stamps_the_owning_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os_cron, "_home_slug", lambda: "ava-mine")
    writes: dict[str, str] = {}
    _crontab_stub(monkeypatch, "", writes)

    assert os_cron._register_linux(300, 3) == 0
    assert "# ava-health-probe.ava-mine" in writes["input"]


def test_register_linux_leaves_another_clusters_line_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two co-located clusters each own a health probe; registering one must not
    silently unregister the other."""
    monkeypatch.setattr(os_cron, "_home_slug", lambda: "ava-mine")
    theirs = "*/5 * * * * /y/ava cluster health-probe --auto-rollback --threshold 3 # ava-health-probe.ava-theirs"
    writes: dict[str, str] = {}
    _crontab_stub(monkeypatch, f"0 3 * * * backup\n{theirs}\n", writes)

    assert os_cron._register_linux(300, 3) == 0
    body = writes["input"]
    assert theirs in body
    assert body.count("# ava-health-probe.ava-mine") == 1
    assert "0 3 * * * backup" in body


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
        def register_cron(self, interval_s: int, threshold: int) -> None:  # type: ignore[no-untyped-def]
            calls.append(f"{interval_s}/{threshold}")

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

    assert calls == ["300/3"]


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

    assert calls == ["300/3"]
