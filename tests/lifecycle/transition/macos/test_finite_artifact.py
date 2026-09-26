"""The finite job runs only the live home helper's own stably signed binary."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.permissions_helper import client, finite_artifact, hardened_runtime, lifecycle
from shared import paths
from shared.native_process.ownership import OwnedProcess

_STABLE = (
    'identifier "com.ava.permissions-helper" and certificate leaf = '
    'H"82109deba414c340272640ab32671d1f464174da"'
)
_CS_VALID = 0x1
_RUNTIME = 0x10000  # CS_RUNTIME: signed with the hardened runtime


def _home(tmp_path: Path) -> Path:
    home = tmp_path.resolve() / "home"
    home.mkdir(mode=0o700)
    return home


def _app(home: Path) -> Path:
    executable = home / "helper/AvaPermissionsHelper.app/Contents/MacOS/AvaPermissionsHelper"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"signed helper bytes")
    executable.chmod(0o755)
    return executable


def test_signed_artifact_binds_bytes_and_stable_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    executable = _app(home)
    checked: list[Path] = []

    def verified(app: Path) -> str:
        checked.append(app)
        return _STABLE

    monkeypatch.setattr(lifecycle, "verified_signed_requirement", verified)
    artifact = finite_artifact.signed_artifact(executable, home)
    assert checked == [executable.parents[2]]
    assert artifact.sha256 == hashlib.sha256(b"signed helper bytes").hexdigest()
    assert artifact.executable == str(executable)
    assert artifact.requirement == _STABLE


def test_signed_artifact_refuses_noncanonical_foreign_or_writable_binaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle, "verified_signed_requirement", lambda _app: _STABLE)
    home = _home(tmp_path)
    executable = _app(home)
    executable.chmod(0o775)
    with pytest.raises(RuntimeError, match="canonical owned app binary"):
        finite_artifact.signed_artifact(executable, home)
    executable.chmod(0o755)
    alias = executable.with_name("Alias")
    alias.symlink_to(executable)
    with pytest.raises(RuntimeError, match="canonical owned app binary"):
        finite_artifact.signed_artifact(alias, home)
    alias.unlink()
    loose = tmp_path.resolve() / "bin/x/y/AvaPermissionsHelper"
    loose.parent.mkdir(parents=True)
    loose.write_bytes(b"unsigned copy")
    with pytest.raises(RuntimeError, match="canonical owned app binary"):
        finite_artifact.signed_artifact(loose, home)
    # Another home's bundle is not this home's helper artifact.
    (tmp_path / "other").mkdir()
    other = _home(tmp_path / "other")
    with pytest.raises(RuntimeError, match="canonical owned app binary"):
        finite_artifact.signed_artifact(executable, other)
    # A second name for the binary could be rewritten behind the verified path.
    twin = tmp_path.resolve() / "twin"
    os.link(executable, twin)
    with pytest.raises(RuntimeError, match="canonical owned app binary"):
        finite_artifact.signed_artifact(executable, home)
    twin.unlink()
    # Every directory on the bundle path must be owner-controlled.
    for directory in (executable.parent, executable.parents[2], home / "helper"):
        directory.chmod(0o775)
        with pytest.raises(RuntimeError, match="not owner-controlled"):
            finite_artifact.signed_artifact(executable, home)
        directory.chmod(0o755)
    assert finite_artifact.signed_artifact(executable, home).requirement == _STABLE


def test_hash_and_signature_must_describe_the_same_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    executable = _app(home)

    def swapped(_app: Path) -> str:
        # A replacement between hashing and codesign verification.
        replacement = executable.with_name("replacement")
        replacement.write_bytes(b"other bytes")
        replacement.chmod(0o755)
        replacement.replace(executable)
        return _STABLE

    monkeypatch.setattr(lifecycle, "verified_signed_requirement", swapped)
    with pytest.raises(RuntimeError, match="changed during signature verification"):
        finite_artifact.signed_artifact(executable, home)

    def rewritten(_app: Path) -> str:
        executable.write_bytes(b"signed helper bytez")
        return _STABLE

    monkeypatch.setattr(lifecycle, "verified_signed_requirement", rewritten)
    with pytest.raises(RuntimeError, match="changed during signature verification"):
        finite_artifact.signed_artifact(executable, home)


def _probe(
    expected: list[str], returncode: int
) -> Callable[..., subprocess.CompletedProcess[bytes]]:
    def run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert cmd == expected
        return subprocess.CompletedProcess(cmd, returncode, b"", b"")

    return run


def test_signature_must_satisfy_the_expected_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(_home(tmp_path)).parents[2]
    monkeypatch.setattr(lifecycle, "_expected_dr", lambda: _STABLE)
    # The requirement tested is the expected one, never the bundle's own claim.
    verify = ["codesign", "--verify", "--strict", f"-R={_STABLE}", str(app)]
    monkeypatch.setattr(lifecycle, "_probe", _probe(verify, 3))
    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="does not satisfy"):
        lifecycle.verified_signed_requirement(app)
    monkeypatch.setattr(lifecycle, "_probe", _probe(verify, 0))
    monkeypatch.setattr(lifecycle, "_signed_code_flags", lambda _app: _RUNTIME)
    monkeypatch.setattr(lifecycle, "_read_dr", lambda _app: 'identifier "x" and cdhash H"1"')
    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="requirement drift"):
        lifecycle.verified_signed_requirement(app)
    monkeypatch.setattr(lifecycle, "_read_dr", lambda _app: _STABLE)
    assert lifecycle.verified_signed_requirement(app) == _STABLE


def test_running_process_must_satisfy_its_requirement(monkeypatch: pytest.MonkeyPatch) -> None:
    verify = ["codesign", "--verify", f"-R={_STABLE}", "4242"]
    monkeypatch.setattr(hardened_runtime, "running_code_flags", lambda _pid: _CS_VALID | _RUNTIME)
    monkeypatch.setattr(lifecycle, "_probe", _probe(verify, 3))
    with pytest.raises(RuntimeError, match="does not satisfy its signed identity"):
        finite_artifact.require_running_identity(4242, _STABLE)
    monkeypatch.setattr(lifecycle, "_probe", _probe(verify, 0))
    finite_artifact.require_running_identity(4242, _STABLE)


@pytest.mark.parametrize(
    "flags",
    [
        0x2,  # ad hoc, not valid
        _CS_VALID,  # valid without the hardened runtime: DYLD_* was honoured at start
        _RUNTIME,  # hardened runtime whose kernel status is no longer valid
    ],
)
def test_running_process_must_be_a_valid_hardened_runtime_image(
    monkeypatch: pytest.MonkeyPatch, flags: int
) -> None:
    """Review P2-A: an injected image still satisfies `codesign -R`; its flags do not."""

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("the requirement must not be tested for an unhardened running image")

    monkeypatch.setattr(hardened_runtime, "running_code_flags", lambda _pid: flags)
    monkeypatch.setattr(lifecycle, "_probe", unexpected)
    with pytest.raises(RuntimeError, match="lacks a valid hardened runtime"):
        finite_artifact.require_running_identity(4242, _STABLE)


@pytest.mark.skipif(sys.platform != "darwin", reason="csops is a macOS kernel call")
def test_running_code_flags_are_the_kernel_status_of_that_process() -> None:
    assert hardened_runtime.running_code_flags(os.getpid()) & _CS_VALID
    with pytest.raises(RuntimeError, match="code-signing status"):
        hardened_runtime.running_code_flags(2**31 - 7)


def test_signed_file_must_carry_the_hardened_runtime_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(_home(tmp_path)).parents[2]
    monkeypatch.setattr(lifecycle, "_expected_dr", lambda: _STABLE)
    monkeypatch.setattr(lifecycle, "_read_dr", lambda _app: _STABLE)
    verify = ["codesign", "--verify", "--strict", f"-R={_STABLE}", str(app)]
    display = ["codesign", "--display", "--verbose=2", str(app)]

    def probe(flags: str) -> Callable[..., subprocess.CompletedProcess[bytes]]:
        def run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            assert cmd in (verify, display)
            shown = f"Executable={app}\nCodeDirectory v=20500 size=9 flags={flags} hashes=1\n"
            return subprocess.CompletedProcess(cmd, 0, b"", shown.encode())

        return run

    monkeypatch.setattr(lifecycle, "_probe", probe("0x0(none)"))
    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="lacks the hardened runtime"):
        lifecycle.verified_signed_requirement(app)
    monkeypatch.setattr(lifecycle, "_probe", probe("0x10002(adhoc,runtime)"))
    assert lifecycle.verified_signed_requirement(app) == _STABLE


_PROTOCOLS = {
    "finite_executor_v1": True,
    "root_stop_intent_v1": True,
    "helper_shutdown_v1": True,
    "root_seed_report_v1": True,
}


@pytest.mark.parametrize(
    ("reply", "peer", "reason"),
    [
        # A real, observable process without the finite protocol refuses on protocol.
        (
            {
                "pong": True,
                "pid": os.getpid(),
                "root_stop_intent_v1": True,
                "helper_shutdown_v1": True,
            },
            os.getpid(),
            "finite executor protocols",
        ),
        # A helper that cannot report its retained root seed refuses before any
        # release work: the macOS start proves the pinned seed through it.
        (
            {"pong": True, "pid": os.getpid(), **_PROTOCOLS, "root_seed_report_v1": False},
            os.getpid(),
            "finite executor protocols",
        ),
        ({"pong": True, "pid": True, **_PROTOCOLS}, 1, "not its socket peer"),
        ({"pong": True, **_PROTOCOLS}, os.getpid(), "not its socket peer"),
        # A self-reported PID other than the kernel's socket peer is never trusted.
        ({"pong": True, "pid": os.getppid(), **_PROTOCOLS}, os.getpid(), "not its socket peer"),
        ({"pong": True, "pid": 2**22 + 7, **_PROTOCOLS}, 2**22 + 7, "not observable"),
        ({"pong": True, "pid": os.getpid(), **_PROTOCOLS}, os.getpid(), "not a live launchd job"),
    ],
    # Explicit ids: the default would embed this worker's PID, and xdist
    # refuses a collection that differs between workers.
    ids=[
        "missing-finite-protocol",
        "missing-root-seed-report",
        "boolean-pid",
        "missing-pid",
        "self-reported-other-pid",
        "unobservable-pid",
        "not-a-launchd-job",
    ],
)
def test_home_helper_is_the_kernel_socket_peer_with_the_finite_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reply: dict[str, object],
    peer: int,
    reason: str,
) -> None:
    home = _home(tmp_path)
    monkeypatch.setattr(paths, "ava_home", lambda: home)
    monkeypatch.setattr(paths, "permissions_helper_socket", lambda: home / "run/helper.sock")

    def ping_peer(*, sock_path: Path) -> tuple[dict[str, object], int]:
        assert sock_path == home / "run/helper.sock"
        return reply, peer

    monkeypatch.setattr(client, "ping_peer", ping_peer)
    with pytest.raises(RuntimeError, match=reason):
        finite_artifact.home_helper_executable(home)


def test_home_helper_socket_must_belong_to_the_requested_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path.resolve() / "ambient")

    def unexpected(**_kwargs: object) -> None:
        pytest.fail("a foreign home's socket must not be dialled")

    monkeypatch.setattr(client, "ping_peer", unexpected)
    with pytest.raises(RuntimeError, match="different home"):
        finite_artifact.home_helper_executable(_home(tmp_path))


@pytest.mark.native_permissions_helper
@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("codesign") is None,
    reason="requires macOS codesign",
)
def test_forged_ad_hoc_bundle_claiming_the_stable_requirement_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review P1-1: an ad-hoc bundle that embeds the stable DR text must not pass."""
    app = tmp_path.resolve() / "Fake.app"
    executable = app / "Contents/MacOS/AvaPermissionsHelper"
    executable.parent.mkdir(parents=True)
    shutil.copyfile(lifecycle._INFO_PLIST, app / "Contents/Info.plist")
    shutil.copyfile("/usr/bin/true", executable)
    executable.chmod(0o755)
    identity = ["--identifier", lifecycle.HELPER_BUNDLE_ID]
    forged = [
        "codesign",
        "--force",
        "--sign",
        "-",
        *identity,
        "--requirements",
        f"=designated => {_STABLE}",
        str(app),
    ]
    subprocess.run(forged, check=True, capture_output=True, timeout=60)  # noqa: S603 — fixed tool, disposable bundle
    monkeypatch.setattr(lifecycle, "_expected_dr", lambda: _STABLE)
    # The embedded text equals the stable requirement; the code does not satisfy it.
    assert lifecycle._read_dr(app) == _STABLE
    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="does not satisfy"):
        lifecycle.verified_signed_requirement(app)
    # The same ad-hoc code does satisfy a requirement it can meet (control),
    # once it is signed with the hardened runtime admission requires.
    ad_hoc = f'identifier "{lifecycle.HELPER_BUNDLE_ID}"'
    honest = [*forged[:-3], "--requirements", f"=designated => {ad_hoc}", str(app)]
    subprocess.run(honest, check=True, capture_output=True, timeout=60)  # noqa: S603 — fixed tool, disposable bundle
    monkeypatch.setattr(lifecycle, "_expected_dr", lambda: ad_hoc)
    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="lacks the hardened runtime"):
        lifecycle.verified_signed_requirement(app)
    hardened = [*honest[:4], *hardened_runtime.SIGNING_OPTIONS, *honest[4:]]
    subprocess.run(hardened, check=True, capture_output=True, timeout=60)  # noqa: S603 — fixed tool, disposable bundle
    assert lifecycle.verified_signed_requirement(app) == ad_hoc


def test_home_helper_running_image_must_satisfy_the_stable_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live helper is a launchd job of this user, but its image fails `codesign -R`."""
    home = _home(tmp_path)
    executable = _app(home)
    monkeypatch.setattr(paths, "ava_home", lambda: home)
    monkeypatch.setattr(paths, "permissions_helper_socket", lambda: home / "run/helper.sock")
    monkeypatch.setattr(
        client, "ping_peer", lambda **_kwargs: ({"pong": True, "pid": 4242, **_PROTOCOLS}, 4242)
    )
    job = SimpleNamespace(
        pid=4242,
        exe=lambda: str(executable),
        ppid=lambda: 1,
        uids=lambda: SimpleNamespace(real=os.getuid()),
    )
    monkeypatch.setattr(finite_artifact.psutil, "Process", lambda _pid: job)
    owner = OwnedProcess(4242, 1.5, None)
    monkeypatch.setattr(OwnedProcess, "capture", staticmethod(lambda _process: owner))
    monkeypatch.setattr(OwnedProcess, "live", lambda _self: True)
    monkeypatch.setattr(lifecycle, "_expected_dr", lambda: _STABLE)
    monkeypatch.setattr(hardened_runtime, "running_code_flags", lambda _pid: _CS_VALID | _RUNTIME)
    checked: list[list[str]] = []

    def probe(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        checked.append(cmd)
        return subprocess.CompletedProcess(cmd, 3, b"", b"")

    monkeypatch.setattr(lifecycle, "_probe", probe)
    with pytest.raises(RuntimeError, match="does not satisfy its signed identity"):
        finite_artifact.home_helper_executable(home)
    assert checked == [["codesign", "--verify", f"-R={_STABLE}", "4242"]]
    monkeypatch.setattr(lifecycle, "_probe", _probe(checked[0], 0))
    assert finite_artifact.home_helper_executable(home) == executable
    # The release start also binds the helper's kernel-authenticated birth.
    assert finite_artifact.home_helper(home) == (owner, executable)
