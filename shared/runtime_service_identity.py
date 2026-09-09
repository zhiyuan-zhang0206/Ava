"""Normal service self-observation, not a publication or startup permission.

The existing health endpoint reports the interpreter/module that actually loaded.
The updater must also match the native PID/birth, parent session and listener;
JSON from a reachable endpoint alone never proves ownership or readiness.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

import psutil

from shared.managed_writer_barrier import Digest, EvidenceModel
from shared.managed_writer_observation import ExpectedProcess
from shared.runtime_interpreter import WHEEL_RUNTIME, runtime_venv
from shared.runtime_release import ReleaseRejectedError, file_sha256
from shared.session_record import pid_starttime_ticks


class NormalRuntimeIdentity(EvidenceModel):
    process: ExpectedProcess
    home: str
    artifact_digest: Digest
    manifest_digest: Digest
    module_name: str
    module_path: str
    executable: str


@lru_cache(maxsize=8)
def _identity(home: str, pid: int) -> NormalRuntimeIdentity:
    prefix = runtime_venv()
    root = prefix.parent
    expected_home = root.parent.parent
    if (
        root.parent.name != "releases"
        or expected_home != Path(home)
        or expected_home.resolve(strict=True) != expected_home
    ):
        raise ReleaseRejectedError("normal service image and unit home differ")
    main = sys.modules["__main__"]
    spec = main.__spec__
    if spec is None or main.__file__ is None:
        raise ReleaseRejectedError("normal service has no actual module entry point")
    module = Path(main.__file__).resolve(strict=True)
    process = psutil.Process(pid)
    executable = Path(process.exe()).resolve(strict=True)
    if not module.is_relative_to(prefix) or not executable.is_relative_to(root):
        raise ReleaseRejectedError("normal service module/interpreter escapes its image")
    return NormalRuntimeIdentity(
        process=ExpectedProcess(
            pid=pid, create_time=process.create_time(), starttime=pid_starttime_ticks(pid)
        ),
        home=home,
        artifact_digest=root.name,
        manifest_digest=file_sha256(root / "manifest.json"),
        module_name=spec.name,
        module_path=str(module),
        executable=str(executable),
    )


def normal_runtime_identity(home: str) -> dict[str, object] | None:
    """Legacy/development health stays unchanged; installed identity fails closed."""
    if not WHEEL_RUNTIME:
        return None
    return _identity(home, psutil.Process().pid).model_dump(mode="json")
