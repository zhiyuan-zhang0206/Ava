"""Boot-time autostart registration (`base.host.system.autostart`).

Pins the properties that matter for reboot survival without recursion:
- the macOS plist runs a bare `ava start` at load (RunAtLoad — identity is the
  home path, no name flag exists) with a PATH that includes Homebrew's bin
  (launchd's minimal PATH omits it, which is what made a bare `ava start` fail
  to find the repo's tools at boot);
- registration writes the plist but NEVER `launchctl bootstrap`s it -- bootstrap
  on a RunAtLoad job runs it immediately, and this runs inside `ava start`, so
  bootstrapping would spawn a second concurrent `ava start`.
Linux systemd delegation is tested at the platform backend; this file also
covers native boot jobs.

The retry block (`test_the_job_retries_*`) is the one that earns its keep: a
fire-once boot job left an agent-runner down for 6.5 hours after its `ava start`
raced the VPN interface at boot, and each platform states the same policy
(`base/host/system/boot_policy.py`) in its own scheduler's terms.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from base.host.system import autostart, cron
from base.host.system.boot_policy import BOOT_RETRY_INTERVAL_S


@pytest.fixture(autouse=True)
def _stub(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    monkeypatch.setattr(autostart, "ava_binary_path", lambda: "/Users/x/.local/bin/ava")


def test_plist_runs_ava_start_at_load() -> None:
    xml = autostart._autostart_plist_content()
    assert "<string>com.ava.autostart</string>" in xml
    assert "<key>RunAtLoad</key>" in xml and "<true/>" in xml
    # ProgramArguments == a bare `ava start` — no name flag exists (path-only).
    for token in ("/Users/x/.local/bin/ava", "<string>start</string>"):
        assert token in xml
    assert "--cluster" not in xml


def test_plist_embeds_a_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The plist must carry an explicit PATH — launchd's minimal PATH omits
    Homebrew's bin, which is what made a bare `ava start` fail to find its tools at
    boot. The PATH composition itself is `base.host.system.cron.launchd_path_env`
    (shared with the watchdog probe's plist) and is unit-tested there."""
    monkeypatch.setattr(cron, "launchd_path_env", lambda: "/opt/homebrew/bin:/usr/bin")
    xml = autostart._autostart_plist_content()
    assert "<key>PATH</key>" in xml
    assert "/opt/homebrew/bin" in xml


def test_register_macos_writes_plist_but_never_bootstraps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    called: list = []
    monkeypatch.setattr(autostart.subprocess, "run", lambda *a, **_k: called.append(a))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    rc = autostart._register_macos()
    assert rc == 0
    plist = tmp_path / "Library" / "LaunchAgents" / "com.ava.autostart.plist"
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
    autostart._register_macos()
    capsys.readouterr()  # drop first-write output  # pyright: ignore[reportUnknownMemberType]
    rc = autostart._register_macos()  # second call, identical content
    assert rc == 0
    assert (
        "wrote" not in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    )  # unchanged -> not rewritten


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
    xml = autostart._autostart_plist_content()
    assert "<key>KeepAlive</key>" in xml
    keep_alive = xml.split("<key>KeepAlive</key>", 1)[1].split("</dict>", 1)[0]
    assert "<key>SuccessfulExit</key>" in keep_alive
    assert "<false/>" in keep_alive
    assert "<true/>" not in keep_alive  # a true here = respawn after success


def test_the_job_retries_on_macos_at_the_shared_interval() -> None:
    """ThrottleInterval overrides launchd's 10s respawn floor; without it a
    failing start would be retried six times a minute."""
    xml = autostart._autostart_plist_content()
    assert "<key>ThrottleInterval</key>" in xml
    assert f"<integer>{BOOT_RETRY_INTERVAL_S}</integer>" in xml
