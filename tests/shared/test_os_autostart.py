"""Boot-time autostart registration (`shared.os_autostart`).

Pins the properties that matter for reboot survival without recursion:
- the macOS plist runs a bare `ava start` at load (RunAtLoad — identity is the
  home path, no name flag exists) with a PATH that includes Homebrew's bin
  (launchd's minimal PATH omits it, which is what made a bare `ava start` fail
  to find the repo's tools at boot);
- registration writes the plist but NEVER `launchctl bootstrap`s it -- bootstrap
  on a RunAtLoad job runs it immediately, and this runs inside `ava start`, so
  bootstrapping would spawn a second concurrent `ava start`.
Plus the Linux `@reboot` crontab entry and the Windows ONLOGON task.

The retry block (`test_the_job_retries_*`) is the one that earns its keep: a
fire-once boot job left an agent-runner down for 6.5 hours after its `ava start`
raced the VPN interface at boot, and each platform states the same policy
(`shared/boot_policy.py`) in its own scheduler's terms.
"""

from __future__ import annotations

import re
import types
from pathlib import Path

import pytest

from shared import os_autostart, os_cron
from shared.boot_policy import BOOT_RETRY_INTERVAL_S


@pytest.fixture(autouse=True)
def _stub(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(os_autostart.settings.general, "ava_home", str(tmp_path))
    monkeypatch.setattr(os_autostart, "ava_binary_path", lambda: "/Users/x/.local/bin/ava")
    # Pin the home-path slug (label token) + this home's legacy-cleanup tokens.
    monkeypatch.setattr(os_autostart, "_home_slug", lambda: "ava-t-cafe0123")
    monkeypatch.setattr(os_autostart, "_legacy_tokens", lambda: ["t"])


def test_plist_runs_ava_start_at_load() -> None:
    xml = os_autostart._autostart_plist_content()
    assert "<string>com.ava.ava-t-cafe0123.autostart</string>" in xml
    assert "<key>RunAtLoad</key>" in xml and "<true/>" in xml
    # ProgramArguments == a bare `ava start` — no name flag exists (path-only).
    for token in ("/Users/x/.local/bin/ava", "<string>start</string>"):
        assert token in xml
    assert "--cluster" not in xml


def test_plist_embeds_a_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The plist must carry an explicit PATH — launchd's minimal PATH omits
    Homebrew's bin, which is what made a bare `ava start` fail to find its tools at
    boot. The PATH composition itself is `shared.os_cron.launchd_path_env`
    (shared with the watchdog probe's plist) and is unit-tested there."""
    monkeypatch.setattr(os_cron, "launchd_path_env", lambda: "/opt/homebrew/bin:/usr/bin")
    xml = os_autostart._autostart_plist_content()
    assert "<key>PATH</key>" in xml
    assert "/opt/homebrew/bin" in xml


def test_register_macos_writes_plist_but_never_bootstraps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    called: list = []
    monkeypatch.setattr(os_autostart.subprocess, "run", lambda *a, **_k: called.append(a))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    rc = os_autostart._register_macos()
    assert rc == 0
    plist = tmp_path / "Library" / "LaunchAgents" / "com.ava.ava-t-cafe0123.autostart.plist"
    assert plist.exists()
    # The whole point: registration must not launchctl-bootstrap (would recurse
    # RunAtLoad -> a second `ava start`). No subprocess at all is issued.
    assert called == []


def test_register_macos_idempotent_no_rewrite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    os_autostart._register_macos()
    capsys.readouterr()  # drop first-write output  # pyright: ignore[reportUnknownMemberType]
    rc = os_autostart._register_macos()  # second call, identical content
    assert rc == 0
    assert (
        "wrote" not in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    )  # unchanged -> not rewritten


def test_register_linux_adds_reboot_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(cmd, **kw):
        if cmd[:2] == ["crontab", "-l"]:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="no crontab for u")
        if cmd == ["crontab", "-"]:
            captured["input"] = kw.get("input")  # pyright: ignore[reportUnknownMemberType]
            return types.SimpleNamespace(returncode=0, stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(os_autostart.shutil, "which", lambda _name: "/usr/bin/crontab")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(os_autostart.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    rc = os_autostart._register_linux()
    assert rc == 0
    assert "@reboot" in captured["input"]
    # A bare `ava boot`, marker-tagged with the home slug; no --cluster flag.
    assert "boot  # ava-autostart.ava-t-cafe0123" in captured["input"]
    assert "--cluster" not in captured["input"]


def test_register_linux_replaces_legacy_cluster_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-path-only crontab entry (marker token = the old cluster name this
    home mapped to) is removed when the slug-marked entry is written."""
    captured: dict = {}
    legacy_line = "@reboot /old/ava start --cluster t  # ava-autostart.t"

    def fake_run(cmd, **kw):
        if cmd[:2] == ["crontab", "-l"]:
            return types.SimpleNamespace(returncode=0, stdout=legacy_line + "\n")
        if cmd == ["crontab", "-"]:
            captured["input"] = kw.get("input")  # pyright: ignore[reportUnknownMemberType]
            return types.SimpleNamespace(returncode=0, stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(os_autostart.shutil, "which", lambda _name: "/usr/bin/crontab")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(os_autostart.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    assert os_autostart._register_linux() == 0
    assert "ava-autostart.ava-t-cafe0123" in captured["input"]
    assert legacy_line not in captured["input"]


def test_register_linux_skips_when_no_crontab(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os_autostart.shutil, "which", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]
    # No crontab binary -> degrade to a warning, not a failure.
    assert os_autostart._register_linux() == 0


# --- the retry policy, per platform ---------------------------------------
#
# One behaviour -- re-run `ava start` every BOOT_RETRY_INTERVAL_S seconds until
# it exits 0 -- stated three ways, because only launchd can retry a boot job for
# us. A boot job that gives up after one failure is what the 2026-07-28 incident
# was: `ava start` raced the VPN interface, exited 1, and nothing tried again.


def test_the_job_retries_on_macos_only_while_it_fails() -> None:
    """launchd.plist(5): `SuccessfulExit: false` restarts the job "in the
    inverse condition" of a zero exit -- i.e. while it keeps failing, and never
    once it succeeds. Getting this key backwards (or passing a bare
    `KeepAlive: true`) would respawn `ava start` forever, including after a
    deliberate `ava stop`."""
    xml = os_autostart._autostart_plist_content()
    assert "<key>KeepAlive</key>" in xml
    keep_alive = xml.split("<key>KeepAlive</key>", 1)[1].split("</dict>", 1)[0]
    assert "<key>SuccessfulExit</key>" in keep_alive
    assert "<false/>" in keep_alive
    assert "<true/>" not in keep_alive  # a true here = respawn after success


def test_the_job_retries_on_macos_at_the_shared_interval() -> None:
    """ThrottleInterval overrides launchd's 10s respawn floor; without it a
    failing start would be retried six times a minute."""
    xml = os_autostart._autostart_plist_content()
    assert "<key>ThrottleInterval</key>" in xml
    assert f"<integer>{BOOT_RETRY_INTERVAL_S}</integer>" in xml


def test_the_job_retries_on_linux_via_ava_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """cron fires a `@reboot` line exactly once, so the loop is ours.

    Pins the whole command SHAPE, not just the verb: `AVA_HOME=<home>` has to
    come first (cron runs the line through /bin/sh, and an assignment only
    scopes to the command it precedes), then the checkout's `ava`, then `boot`.
    Both halves of that line arrived from different branches — the env prefix
    from `os_cron.cron_env_prefix`, the verb from this change — and nothing else
    in the suite asserts their order.
    """
    captured: dict = {}

    def fake_run(cmd, **kw):
        if cmd[:2] == ["crontab", "-l"]:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="no crontab for u")
        captured["input"] = kw.get("input")  # pyright: ignore[reportUnknownMemberType]
        return types.SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(os_autostart.shutil, "which", lambda _name: "/usr/bin/crontab")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(os_autostart.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    assert os_autostart._register_linux() == 0
    assert re.search(
        r"^@reboot AVA_HOME=\S+ /Users/x/\.local/bin/ava boot\s+# ava-autostart\.",
        captured["input"],  # pyright: ignore[reportUnknownArgumentType]
        re.MULTILINE,
    ), captured["input"]


def test_the_job_retries_on_windows_via_ava_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """`schtasks /RI` is documented as not applicable to ONLOGON, and a cmd.exe
    retry wrapper would flash a console window -- so Windows runs the same
    `ava boot` loop Linux does."""
    from shared import os_schtasks

    seen: list[tuple[str, tuple[str, ...], int]] = []
    monkeypatch.setattr(
        os_schtasks,
        "create_logon_task",
        lambda kind, args, *, time_limit_s: seen.append((kind, tuple(args), time_limit_s)),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert os_autostart._register_windows() is None
    # Unbounded on purpose: `ava boot` retries with no attempt cap, matching what
    # launchd and cron do, so a scheduler-imposed runtime limit would be a
    # Windows-only cap on the one job nothing else recovers.
    assert seen == [("autostart", ("boot",), os_schtasks.NO_TIME_LIMIT_S)]


# --- the GUI-domain relaunch (the browser context heal's primitive) ---


def _fake_launchctl(
    monkeypatch: pytest.MonkeyPatch, results: dict[str, types.SimpleNamespace]
) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kw: object) -> types.SimpleNamespace:
        calls.append(cmd)
        # Keyed by the launchctl subcommand (cmd[1]); unknown ones succeed.
        for sub, result in results.items():
            if len(cmd) > 1 and cmd[1] == sub:
                return result
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(os_autostart.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    return calls


def test_relaunch_via_gui_domain_bootstraps_when_missing_then_kicks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The documented manual step, automated: an unloaded job is bootstrapped
    into gui/<uid> first, then kickstarted so its `ava start` runs there."""
    monkeypatch.setenv("HOME", str(tmp_path))
    plist = tmp_path / "Library" / "LaunchAgents" / "com.ava.ava-t-cafe0123.autostart.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("plist")
    import os as _os

    domain = f"gui/{_os.getuid()}"
    calls = _fake_launchctl(
        monkeypatch,
        {"print": types.SimpleNamespace(returncode=1, stdout="", stderr="Could not find")},
    )
    ok, detail = os_autostart.relaunch_via_gui_domain()
    assert ok is True
    assert "com.ava.ava-t-cafe0123.autostart" in detail
    assert calls == [
        ["launchctl", "print", f"{domain}/com.ava.ava-t-cafe0123.autostart"],
        ["launchctl", "bootstrap", domain, str(plist)],
        ["launchctl", "kickstart", "-k", f"{domain}/com.ava.ava-t-cafe0123.autostart"],
    ]


def test_relaunch_via_gui_domain_skips_bootstrap_when_loaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An already-loaded job is not re-bootstrapped (that errors); it is only
    kickstarted, which re-runs `ava start` even after a clean exit."""
    monkeypatch.setenv("HOME", str(tmp_path))
    plist = tmp_path / "Library" / "LaunchAgents" / "com.ava.ava-t-cafe0123.autostart.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("plist")
    calls = _fake_launchctl(
        monkeypatch,
        {"print": types.SimpleNamespace(returncode=0, stdout="", stderr="")},
    )
    ok, _detail = os_autostart.relaunch_via_gui_domain()
    assert ok is True
    assert [cmd[1] for cmd in calls] == ["print", "kickstart"]


def test_relaunch_via_gui_domain_reports_a_missing_plist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing registered on this host: say so instead of running launchctl
    against a label that does not exist (the caller logs the manual recipe)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = _fake_launchctl(monkeypatch, {})
    ok, detail = os_autostart.relaunch_via_gui_domain()
    assert ok is False
    assert "autostart plist" in detail
    assert calls == []


def test_relaunch_via_gui_domain_reports_a_failed_kick(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    plist = tmp_path / "Library" / "LaunchAgents" / "com.ava.ava-t-cafe0123.autostart.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("plist")
    _fake_launchctl(
        monkeypatch,
        {
            "print": types.SimpleNamespace(returncode=0, stdout="", stderr=""),
            "kickstart": types.SimpleNamespace(
                returncode=113, stdout="", stderr="Bootstrap failed"
            ),
        },
    )
    ok, detail = os_autostart.relaunch_via_gui_domain()
    assert ok is False
    assert "Bootstrap failed" in detail


def test_relaunch_via_gui_domain_refuses_off_macos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The GUI domain is a macOS concept; elsewhere the caller must not expect a
    relaunch (the heal is gated on IS_MACOS before it gets here)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(os_autostart, "IS_MACOS", False)
    calls = _fake_launchctl(monkeypatch, {})
    ok, detail = os_autostart.relaunch_via_gui_domain()
    assert ok is False
    assert "macOS" in detail
    assert calls == []
