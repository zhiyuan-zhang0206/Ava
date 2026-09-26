"""The signed artifact a finite release-executor job runs: the home helper's own binary.

A finite launchd job per release attempt runs ``--finite-executor`` of the same
stably signed helper that owns this home's ava-root. The executable comes from
the live helper process the kernel reports as the listener on this home's
socket, whose running image satisfies the stable requirement. The artifact must
be this home's installed bundle in owner-controlled directories, and the file
that is hashed is the file whose signature satisfies the stable requirement
(``codesign -R``). A path, a build-state file or an unsigned or ad-hoc copy is
never accepted in its place.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import psutil
from pydantic import BaseModel, ConfigDict, Field

from shared.native_process.ownership import OwnedProcess
from shared.verified_file import regular_bytes

FINITE_PROTOCOLS = ("finite_executor_v1", "root_stop_intent_v1", "helper_shutdown_v1")
_APP = "AvaPermissionsHelper.app"
_EXECUTABLE = Path("Contents") / "MacOS" / "AvaPermissionsHelper"


class HelperArtifact(BaseModel):
    """Exact helper artifact identity captured before a finite job is planned."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    app: str
    executable: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requirement: str = Field(min_length=1)


def executable_sha256(executable: Path) -> str:
    return hashlib.sha256(regular_bytes(executable, max_bytes=256 * 1024 * 1024)).hexdigest()


def home_app(home: Path) -> Path:
    """This home's installed helper bundle: the explicit artifact dir or ``home/helper``."""
    from shared.config import settings

    override = settings.services.permissions_helper_artifact_dir
    return (home / "helper" if override is None else Path(override)) / _APP


def require_running_identity(pid: int, requirement: str) -> None:
    """The running image of ``pid`` satisfies ``requirement`` (kernel code-signing state)."""
    from services.permissions_helper import lifecycle

    try:
        lifecycle.verified_running_requirement(pid, requirement)
    except lifecycle.PermissionsHelperBuildError as exc:
        raise RuntimeError(str(exc)) from exc


def home_helper_executable(home: Path) -> Path:
    """The executable of this home's live helper, which also births ava-root.

    The helper is identified by the kernel's record of the socket listener, not
    by the PID it reports about itself, and its running image must satisfy the
    stable requirement.
    """
    from services.permissions_helper import client, lifecycle
    from shared import paths

    if paths.ava_home() != home:
        raise RuntimeError("helper socket configuration belongs to a different home")
    reply, peer = client.ping_peer(sock_path=paths.permissions_helper_socket())
    if reply.get("pong") is not True or any(reply.get(key) is not True for key in FINITE_PROTOCOLS):
        raise RuntimeError(f"home helper lacks the finite executor protocols {FINITE_PROTOCOLS}")
    reported = reply.get("pid")
    if isinstance(reported, bool) or reported != peer or peer <= 1:
        raise RuntimeError("home helper's reported process is not its socket peer")
    try:
        process = psutil.Process(peer)
        owner = OwnedProcess.capture(process)
        executable = Path(process.exe())
        parent = process.ppid()
        uid = process.uids().real
    except psutil.Error as exc:
        raise RuntimeError("home helper process is not observable") from exc
    if parent != 1 or uid != os.getuid() or not owner.live():
        raise RuntimeError("home helper is not a live launchd job of this user")
    require_running_identity(peer, lifecycle.expected_requirement())
    if not owner.live():
        raise RuntimeError("home helper changed during signature verification")
    return executable


def _require_owned_directories(app: Path) -> None:
    """The artifact directory and bundle path are canonical and only owner-writable."""
    for directory in (app.parent, app, app / "Contents", app / "Contents" / "MacOS"):
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or directory.resolve(strict=True) != directory
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
        ):
            raise RuntimeError(
                f"finite helper directory is not owner-controlled and canonical: {directory}"
            )


def _file_identity(path: Path) -> tuple[int, ...]:
    info = path.lstat()
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def signed_artifact(executable: Path, home: Path) -> HelperArtifact:
    """Admit only this home's canonical owned app binary that satisfies the stable identity."""
    from services.permissions_helper import lifecycle

    app = executable.parents[2]
    info = executable.lstat()
    if (
        app != home_app(home)
        or executable != app / _EXECUTABLE
        or executable.resolve(strict=True) != executable
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
        or info.st_nlink != 1
    ):
        raise RuntimeError("finite helper must be this home's canonical owned app binary")
    _require_owned_directories(app)
    # Hash and signature must describe one file: any replacement or in-place
    # write between them changes the inode or the kernel-maintained ctime.
    before = _file_identity(executable)
    digest = executable_sha256(executable)
    requirement = lifecycle.verified_signed_requirement(app)
    if _file_identity(executable) != before:
        raise RuntimeError("finite helper binary changed during signature verification")
    return HelperArtifact(
        app=str(app),
        executable=str(executable),
        sha256=digest,
        requirement=requirement,
    )


def capture(home: Path) -> HelperArtifact:
    """Verify the stably signed artifact the persistent home helper runs."""
    return signed_artifact(home_helper_executable(home), home)
