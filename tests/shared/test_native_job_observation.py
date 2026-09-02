"""Native query/parser boundaries; mocked states are not platform support proof."""

from __future__ import annotations

import hashlib
import plistlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from shared import native_job_observation as jobs


def deadline() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=10)


def test_crontab_definition_bound_to_home_and_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    binary = home / "releases" / ("a" * 64) / "venv" / "bin" / "ava"
    binary.parent.mkdir(parents=True)
    binary.touch()
    line = f"@reboot AVA_HOME={home} {binary} start # ava-autostart.test"
    digest = hashlib.sha256(line.encode()).hexdigest()
    monkeypatch.setattr(jobs, "read_crontab", lambda _until: (line + "\n").encode())
    result = jobs.observe_crontab(digest, digest, home, "a" * 64, deadline())
    assert result.definition == "match"
    assert result.enabled is True
    assert result.declared_home == "match"
    assert result.declared_image == "prepared"
    assert result.loaded is None
    assert result.loaded_image == "unknown"
    assert (
        jobs.observe_crontab(digest, digest, home / "other", "a" * 64, deadline()).declared_home
        == "mismatch"
    )


def test_crontab_drift_and_missing_are_different(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reads = iter((b"", b"new job\n"))
    monkeypatch.setattr(jobs, "read_crontab", lambda _until: next(reads))
    with pytest.raises(jobs.NativeReadUnavailableError, match="changed"):
        jobs.observe_crontab("a" * 64, "a" * 64, tmp_path, "b" * 64, deadline())
    monkeypatch.setattr(jobs, "read_crontab", lambda _until: b"")
    result = jobs.observe_crontab("a" * 64, "a" * 64, tmp_path, "b" * 64, deadline())
    assert result.definition == "absent"
    assert result.enabled is False
    assert result.loaded is None


def test_launchd_disk_binding_does_not_claim_loaded_image_or_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = plistlib.dumps(
        {
            "Label": "com.ava.test",
            "ProgramArguments": ["/bin/echo", "hello"],
            "EnvironmentVariables": {"AVA_HOME": str(tmp_path)},
            "Disabled": True,
        }
    )
    monkeypatch.setattr(jobs, "read_launchd_definition", lambda _label: raw)
    monkeypatch.setattr(jobs, "launchd_loaded", lambda _label, _until: True)
    result = jobs.observe_launchd(
        "com.ava.test", hashlib.sha256(raw).hexdigest(), tmp_path, "a" * 64, deadline()
    )
    assert result.definition == "match"
    assert result.declared_home == "match"
    assert result.declared_image == "other"
    assert result.loaded is True
    assert result.enabled is None  # plist Disabled is not the effective override
    assert result.loaded_image == "unknown"


def test_launchd_loaded_state_drift_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "read_launchd_definition", lambda _label: b"same")
    reads = iter((True, None))
    monkeypatch.setattr(jobs, "launchd_loaded", lambda _label, _until: next(reads))
    with pytest.raises(jobs.NativeReadUnavailableError, match="changed"):
        jobs.observe_launchd("com.ava.test", "a" * 64, tmp_path, "b" * 64, deadline())


def test_expired_native_query_never_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("expired query executed")

    monkeypatch.setattr(jobs.subprocess, "run", unexpected)
    with pytest.raises(jobs.NativeReadUnavailableError, match="expired"):
        jobs.native_read(("/usr/bin/crontab", "-l"), datetime.now(UTC) - timedelta(seconds=1))
