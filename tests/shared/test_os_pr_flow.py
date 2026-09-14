"""PR-flow sampler OS job — registration contract, gating, and definitions."""

from __future__ import annotations

import types
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from shared import os_pr_flow as job


@pytest.fixture(autouse=True)
def _configure_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from shared import os_cron

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(os_cron, "_home_slug", lambda: "ava-deadbeef")
    monkeypatch.setattr(os_cron, "ava_binary_path", lambda: "/work tree/.venv/bin/ava")
    monkeypatch.setattr(os_cron, "job_home", lambda: str(tmp_path / ".ava"))
    monkeypatch.setattr(os_cron, "launchd_path_env", lambda: "/work tree/.venv/bin:/usr/bin")
    monkeypatch.setattr("shared.paths.repo_root", lambda: Path("/check/out"))


def _ok() -> types.SimpleNamespace:
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def _which_gh(_name: str) -> str | None:
    return "/opt/homebrew/bin/gh"


def _which_missing(_name: str) -> str | None:
    return None


def _fake_slug(_home: object) -> str:
    return "ava-deadbeef"


def test_launchd_plist_schedules_the_daily_sampler(tmp_path: Path) -> None:
    content = job._launchd_plist_content()
    root = ET.fromstring(content)  # noqa: S314 — self-generated plist
    values = [element.text for element in root.findall("./dict/array/string")]

    assert "com.ava.ava-deadbeef.pr-flow" in content
    assert values[0:2] == ["/bin/sh", "-c"]
    command = values[2]
    assert command is not None
    assert "'/work tree/.venv/bin/python'" in command
    assert "/check/out/scripts/pr_flow_export.py" in command
    assert (
        "<key>StartCalendarInterval</key>\n    <dict>\n"
        "            <key>Hour</key>\n            <integer>0</integer>\n"
        "            <key>Minute</key>\n            <integer>25</integer>\n"
    ) in content
    assert "<key>RunAtLoad</key>\n    <false/>" in content
    assert f"{tmp_path}/.ava/logs/pr-flow.out.log" in content
    assert f"<string>{tmp_path}/.ava</string>" in content  # AVA_HOME pin


def test_macos_reregistration_rewrites_and_reloads_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> types.SimpleNamespace:
        calls.append(argv)
        return _ok()

    monkeypatch.setattr(job.subprocess, "run", run)

    assert job._register_macos() == 0
    plist = job._launchd_plist_path("ava-deadbeef")
    first = plist.read_text(encoding="utf-8")
    assert job._register_macos() == 0

    assert plist.read_text(encoding="utf-8") == first
    assert [call[1] for call in calls] == ["bootout", "bootstrap", "bootout", "bootstrap"]


def test_linux_registration_replaces_only_this_clusters_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    other = "25 0 * * * /other/ava  # ava-pr-flow.ava-other-cafefeed"
    old = "25 0 * * * /old/ava  # ava-pr-flow.ava-deadbeef"
    written: dict[str, str] = {}

    def which(_name: str) -> str:
        return "/usr/bin/crontab"

    monkeypatch.setattr(job.shutil, "which", which)

    def run(argv: list[str], **kwargs: object) -> types.SimpleNamespace:
        if argv == ["crontab", "-l"]:
            return types.SimpleNamespace(returncode=0, stdout=f"{other}\n{old}\n", stderr="")
        written["body"] = str(kwargs["input"])
        return _ok()

    monkeypatch.setattr(job.subprocess, "run", run)

    assert job._register_linux() == 0
    assert other in written["body"]
    assert old not in written["body"]
    assert written["body"].count("# ava-pr-flow.ava-deadbeef") == 1
    assert "25 0 * * *" in written["body"]
    assert "pr_flow_export.py" in written["body"]
    assert str(tmp_path / ".ava" / "logs" / "pr-flow.out.log") in written["body"]


def _passing_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("shared.observability.production_identity", lambda: True)
    monkeypatch.setattr(job.shutil, "which", _which_gh)
    token = tmp_path / ".trunk" / "api-token"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("tok\n", encoding="utf-8")


def test_credential_blocker_names_each_missing_piece(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("shared.observability.production_identity", lambda: False)
    assert job.credential_blocker() == "not the registered production home"

    monkeypatch.setattr("shared.observability.production_identity", lambda: True)
    monkeypatch.setattr(job.shutil, "which", _which_missing)
    assert job.credential_blocker() == "gh CLI not on PATH"

    monkeypatch.setattr(job.shutil, "which", _which_gh)
    assert job.credential_blocker() == f"no Trunk API token at {tmp_path}/.trunk/api-token"

    token = tmp_path / ".trunk" / "api-token"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("   \n", encoding="utf-8")
    assert "no Trunk API token" in (job.credential_blocker() or "")

    token.write_text("tok\n", encoding="utf-8")
    assert job.credential_blocker() is None


def test_register_skips_when_os_jobs_are_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared import os_cron

    skipped: list[str] = []
    monkeypatch.setattr(os_cron, "os_jobs_enabled", lambda: False)
    monkeypatch.setattr(os_cron, "skip_os_job", skipped.append)

    def no_backend():
        raise AssertionError("backend must not be touched with OS jobs off")

    monkeypatch.setattr("shared.platform_backend.get_backend", no_backend)
    job.register_pr_flow_job()
    assert skipped == ["pr flow"]


def test_register_skips_without_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from shared import os_cron

    monkeypatch.setattr(os_cron, "os_jobs_enabled", lambda: True)
    monkeypatch.setattr("shared.observability.production_identity", lambda: True)
    monkeypatch.setattr(job.shutil, "which", _which_missing)  # no gh

    def no_backend():
        raise AssertionError("backend must not be touched without credentials")

    monkeypatch.setattr("shared.platform_backend.get_backend", no_backend)
    job.register_pr_flow_job()


def test_register_delegates_when_credentials_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from shared import os_cron

    monkeypatch.setattr(os_cron, "os_jobs_enabled", lambda: True)
    _passing_credentials(monkeypatch, tmp_path)
    calls: list[str] = []
    fake_backend = types.SimpleNamespace(
        register_pr_flow_job=lambda: calls.append("register"),
        unregister_pr_flow_job=lambda slug: calls.append(f"unregister:{slug}"),
    )
    monkeypatch.setattr("shared.platform_backend.get_backend", lambda: fake_backend)

    job.register_pr_flow_job()
    assert calls == ["register"]

    monkeypatch.setattr("shared.cluster.slug_for_home", _fake_slug)
    job.unregister_pr_flow_job()
    assert calls == ["register", "unregister:ava-deadbeef"]


def test_windows_backend_is_a_documented_noop() -> None:
    from shared.platform_backend import WindowsPlatformBackend

    backend = WindowsPlatformBackend()
    backend.register_pr_flow_job()  # logs only — must not raise
    backend.unregister_pr_flow_job("ava-deadbeef")
