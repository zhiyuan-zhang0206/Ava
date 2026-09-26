"""The finite job runs only the live home helper's own stably signed binary."""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from services.permissions_helper import client, finite_artifact, lifecycle


def _app(tmp_path: Path) -> Path:
    executable = tmp_path.resolve() / "AvaPermissionsHelper.app/Contents/MacOS/AvaPermissionsHelper"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"signed helper bytes")
    executable.chmod(0o755)
    return executable


def test_signed_artifact_binds_bytes_and_stable_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _app(tmp_path)
    checked: list[Path] = []

    def verified(app: Path) -> str:
        checked.append(app)
        return 'identifier "com.ava.permissions-helper" and certificate leaf = H"00"'

    monkeypatch.setattr(lifecycle, "verified_signed_requirement", verified)
    artifact = finite_artifact.signed_artifact(executable)
    assert checked == [executable.parents[2]]
    assert artifact.sha256 == hashlib.sha256(b"signed helper bytes").hexdigest()
    assert artifact.executable == str(executable)


def test_signed_artifact_refuses_noncanonical_or_writable_binaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle, "verified_signed_requirement", lambda _app: "requirement")
    executable = _app(tmp_path)
    executable.chmod(0o775)
    with pytest.raises(RuntimeError, match="canonical owned app binary"):
        finite_artifact.signed_artifact(executable)
    executable.chmod(0o755)
    alias = executable.with_name("Alias")
    alias.symlink_to(executable)
    with pytest.raises(RuntimeError, match="canonical owned app binary"):
        finite_artifact.signed_artifact(alias)
    loose = tmp_path.resolve() / "bin/x/AvaPermissionsHelper"
    loose.parent.mkdir(parents=True)
    loose.write_bytes(b"unsigned copy")
    with pytest.raises(RuntimeError, match="canonical owned app binary"):
        finite_artifact.signed_artifact(loose)


def test_signature_must_verify_as_the_stable_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path).parents[2]

    def probe(returncode: int) -> Callable[..., subprocess.CompletedProcess[bytes]]:
        def run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            assert cmd == ["codesign", "--verify", "--strict", str(app)]
            return subprocess.CompletedProcess(cmd, returncode, b"", b"")

        return run

    monkeypatch.setattr(lifecycle, "_probe", probe(1))
    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="does not verify"):
        lifecycle.verified_signed_requirement(app)
    monkeypatch.setattr(lifecycle, "_probe", probe(0))
    monkeypatch.setattr(lifecycle, "_read_dr", lambda _app: 'identifier "x" and cdhash H"1"')
    monkeypatch.setattr(lifecycle, "_expected_dr", lambda: 'identifier "x" and leaf H"2"')
    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="requirement drift"):
        lifecycle.verified_signed_requirement(app)
    monkeypatch.setattr(lifecycle, "_expected_dr", lambda: 'identifier "x" and cdhash H"1"')
    assert lifecycle.verified_signed_requirement(app) == 'identifier "x" and cdhash H"1"'


_PROTOCOLS = {"finite_executor_v1": True, "root_stop_intent_v1": True, "helper_shutdown_v1": True}


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        # A real, observable process without the finite protocol refuses on protocol.
        (
            {
                "pong": True,
                "pid": os.getpid(),
                "root_stop_intent_v1": True,
                "helper_shutdown_v1": True,
            },
            "finite executor protocols",
        ),
        ({"pong": True, "pid": True, **_PROTOCOLS}, "native process"),
        ({"pong": True, **_PROTOCOLS}, "native process"),
        ({"pong": True, "pid": 2**22 + 7, **_PROTOCOLS}, "not observable"),
        ({"pong": True, "pid": os.getpid(), **_PROTOCOLS}, "not a live launchd job"),
    ],
)
def test_home_helper_must_be_a_live_launchd_job_with_the_finite_protocol(
    monkeypatch: pytest.MonkeyPatch, reply: dict[str, object], reason: str
) -> None:
    monkeypatch.setattr(client, "ping", lambda: reply)
    with pytest.raises(RuntimeError, match=reason):
        finite_artifact.home_helper_executable()
