"""The signed artifact a finite release-executor job runs: the home helper's own binary.

A finite launchd job per release attempt runs ``--finite-executor`` of the same
stably signed helper that owns this home's ava-root. The executable comes from
the live helper process that answers this home's socket with the finite
protocol, and its signature must verify as the stable identity. A path, a
build-state file or an unsigned copy is never accepted in its place.
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


def home_helper_executable() -> Path:
    """The executable of this home's live helper, which also births ava-root."""
    from services.permissions_helper import client

    reply = client.ping()
    if reply["pong"] is not True or any(reply.get(key) is not True for key in FINITE_PROTOCOLS):
        raise RuntimeError(f"home helper lacks the finite executor protocols {FINITE_PROTOCOLS}")
    pid = reply.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise RuntimeError("home helper omitted its native process")
    try:
        process = psutil.Process(pid)
        owner = OwnedProcess.capture(process)
        executable = Path(process.exe())
        parent = process.ppid()
    except psutil.Error as exc:
        raise RuntimeError("home helper process is not observable") from exc
    if parent != 1 or not owner.live():
        raise RuntimeError("home helper is not a live launchd job")
    return executable


def signed_artifact(executable: Path) -> HelperArtifact:
    """Admit only a canonical owned app binary that verifies as the stable identity."""
    from services.permissions_helper import lifecycle

    app = executable.parents[2]
    info = executable.lstat()
    if (
        app.suffix != ".app"
        or executable != app / _EXECUTABLE
        or executable.resolve(strict=True) != executable
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
    ):
        raise RuntimeError("finite helper must be the home helper's canonical owned app binary")
    return HelperArtifact(
        app=str(app),
        executable=str(executable),
        sha256=executable_sha256(executable),
        requirement=lifecycle.verified_signed_requirement(app),
    )


def capture() -> HelperArtifact:
    """Verify the stably signed artifact the persistent home helper runs."""
    return signed_artifact(home_helper_executable())
