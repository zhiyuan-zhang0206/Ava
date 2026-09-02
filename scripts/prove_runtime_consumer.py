"""CI-only installed-runtime consumer proof; no service or cluster is started."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from ops.agent_launch import _agent_interpreter
from ops.spec import build_services
from shared.platform_backend import get_backend
from shared.runtime_interpreter import INSTALLED_RUNTIME, runtime_python, runtime_venv
from shared.session_env import forward_env_dict, venv_activation_prefix


def require(condition: bool, detail: str) -> None:  # noqa: FBT001 — assertion predicate, not a mode flag.
    if not condition:
        raise AssertionError(detail)


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    prefix = Path(sys.prefix).resolve()
    alias = Path(sys.argv[2])
    require(alias.is_symlink() and alias.resolve() == prefix, "entry alias did not select A")
    require(INSTALLED_RUNTIME, "proof did not load the installed wheel")
    require(runtime_venv().resolve() == prefix, "venv escaped current prefix")
    python = runtime_python()
    require(Path(_agent_interpreter()[0]) == python, "agent interpreter mismatch")
    require(
        Path(get_backend().venv_python()).parent == python.parent, "platform interpreter mismatch"
    )
    require(Path(forward_env_dict()["VIRTUAL_ENV"]).resolve() == prefix, "session env mismatch")
    require(str(prefix) in venv_activation_prefix(), "shell activation mismatch")
    for spec in build_services():
        if " -m " in spec.cmd:
            require(Path(shlex.split(spec.cmd)[0]).resolve().is_relative_to(prefix), spec.session)

    # A is already imported. Changing the selection hint to B must not retarget
    # A's delayed subprocesses. Neither consumer reads this mutable selector.
    pointer = prefix.parent.parent / "current-release"
    previous = pointer.read_bytes()
    other = root / "generation-B-not-started"
    other.mkdir()
    try:
        pointer.write_bytes(b"different-generation-B\n")
        alias.unlink()
        alias.symlink_to(other, target_is_directory=True)
        require(runtime_python() == python, "selector changed running interpreter")
        late = subprocess.run(  # noqa: S603 — exact current generation, no shell.
            [str(python), "-I", "-B", "-c", "import sys;print(sys.prefix)"],
            cwd=root,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        require(Path(late.stdout.strip()).resolve() == prefix, "late child escaped generation A")
        child = subprocess.run(  # noqa: S603 — entry-point guard, no exec request supplied.
            [str(python), "-I", "-B", "-m", "agent.exec_child"],
            cwd=root,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        require(
            child.returncode == 2 and "needs AVA_EXEC_REQUEST_FILE" in child.stderr,
            "exec child did not reach its request guard",
        )
    finally:
        pointer.write_bytes(previous)
        alias.unlink()
        alias.symlink_to(prefix, target_is_directory=True)
    no_home = os.environ.copy()
    no_home.pop("AVA_HOME", None)
    rejected = subprocess.run(  # noqa: S603 — fail-closed bootstrap probe, no data access.
        [str(python), "-I", "-B", "-c", "import shared.dotenv_boot"],
        cwd=root,
        env=no_home,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    require(
        rejected.returncode != 0 and "explicit absolute AVA_HOME" in rejected.stderr,
        "missing wheel home was not rejected",
    )
    print(json.dumps({"late_child_generation": str(prefix), "missing_home_rejected": True}))


if __name__ == "__main__":
    main()
