"""Native query/parser boundaries; mocked states are not platform support proof."""

from __future__ import annotations

import hashlib
import plistlib
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from shared import native_job_observation as jobs


def deadline() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=10)


def constant[T](value: T) -> Callable[..., T]:
    def read(*_args: object) -> T:
        return value

    return read


def sequence[T](*values: T) -> Callable[..., T]:
    entries = iter(values)

    def read(*_args: object) -> T:
        return next(entries)

    return read


def test_crontab_definition_bound_to_home_and_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    binary = home / "releases" / ("a" * 64) / "venv" / "bin" / "ava"
    binary.parent.mkdir(parents=True)
    binary.touch()
    line = f"@reboot AVA_HOME={home} {binary} start # ava-autostart.test"
    digest = hashlib.sha256(line.encode()).hexdigest()
    monkeypatch.setattr(jobs, "read_crontab", constant((line + "\n").encode()))
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
    monkeypatch.setattr(jobs, "read_crontab", sequence(b"", b"new job\n"))
    with pytest.raises(jobs.NativeReadUnavailableError, match="changed"):
        jobs.observe_crontab("a" * 64, "a" * 64, tmp_path, "b" * 64, deadline())
    monkeypatch.setattr(jobs, "read_crontab", constant(b""))
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
    monkeypatch.setattr(jobs, "read_launchd_definition", constant(raw))
    monkeypatch.setattr(jobs, "launchd_loaded", constant(True))
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
    monkeypatch.setattr(jobs, "read_launchd_definition", constant(b"same"))
    monkeypatch.setattr(jobs, "launchd_loaded", sequence(True, None))
    with pytest.raises(jobs.NativeReadUnavailableError, match="changed"):
        jobs.observe_launchd("com.ava.test", "a" * 64, tmp_path, "b" * 64, deadline())


def test_expired_native_query_never_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("expired query executed")

    monkeypatch.setattr(jobs.subprocess, "run", unexpected)
    with pytest.raises(jobs.NativeReadUnavailableError, match="expired"):
        jobs.native_read(("/usr/bin/crontab", "-l"), datetime.now(UTC) - timedelta(seconds=1))


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"different header\n",
        b"PID Status Label\n- bad job\n",
        b"PID Status Label\n- 0 x\n- 0 x\n",
    ],
)
def test_launchd_unknown_list_format_is_not_empty(body: bytes) -> None:
    with pytest.raises(jobs.NativeReadUnavailableError):
        jobs.parse_launchd_labels(body)


def test_launchd_documented_list_columns() -> None:
    assert jobs.parse_launchd_labels(
        b"PID\tStatus\tLabel\n-\t0\tcom.ava.test\n123\t-15\tcom.apple.test\n"
    ) == frozenset({"com.ava.test", "com.apple.test"})


@pytest.mark.parametrize("failure", ["uid", "domain", "uid_read", "domain_read"])
def test_launchd_wrong_or_unreadable_domain_never_lists(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs.sys, "platform", "darwin")
    calls: list[str] = []

    def read(argv: tuple[str, ...], _until: datetime) -> subprocess.CompletedProcess[bytes]:
        command = argv[-1]
        calls.append(command)
        assert command != "list", "an unverified GUI domain must not enumerate jobs"
        if command == "manageruid":
            body = str(jobs.os.getuid() + (1 if failure == "uid" else 0)).encode()
            return subprocess.CompletedProcess(argv, int(failure == "uid_read"), body, b"")
        assert command == "managername"
        body = b"Background" if failure == "domain" else b"Aqua"
        return subprocess.CompletedProcess(argv, int(failure == "domain_read"), body, b"")

    monkeypatch.setattr(jobs, "native_read", read)
    with pytest.raises(jobs.NativeReadUnavailableError, match="GUI domain"):
        jobs.read_launchd_labels(deadline())
    assert calls == ["manageruid", "managername"]


def test_failed_crontab_read_is_not_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs.sys, "platform", "linux")
    failed = subprocess.CompletedProcess(
        ("/usr/bin/crontab", "-l"), 1, b"", b"permission denied; no crontab for user\n"
    )
    monkeypatch.setattr(jobs, "native_read", constant(failed))
    with pytest.raises(jobs.NativeReadUnavailableError):
        jobs.read_crontab(deadline())


def test_inconsistent_crontab_identity_cannot_claim_absence(tmp_path: Path) -> None:
    with pytest.raises(jobs.NativeReadUnavailableError, match="identity"):
        jobs.observe_crontab("a" * 64, "b" * 64, tmp_path, "c" * 64, deadline())
