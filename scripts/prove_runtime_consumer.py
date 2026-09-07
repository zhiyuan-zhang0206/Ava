"""CI-only installed-runtime consumer proof; no service or cluster is started."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from ops.spec import build_services
from shared.cluster import frontend_service_cmd
from shared.migrations import required_migration_set
from shared.platform_backend import get_backend
from shared.runtime_interpreter import WHEEL_RUNTIME, runtime_python, runtime_venv
from shared.session_env import forward_env_dict, venv_activation_prefix


def require(condition: bool, detail: str) -> None:  # noqa: FBT001 — assertion predicate, not a mode flag.
    if not condition:
        raise AssertionError(detail)


def prove_frontend_config_rejection(python: Path, root: Path) -> None:
    probe = """
from shared.config import settings
from shared.cluster import frontend_service_cmd
assert settings.gateway.gateway_port == 8001, 'negative config was not applied'
try:
    frontend_service_cmd(43871)
except RuntimeError as exc:
    assert 'public configuration differs' in str(exc), str(exc)
else:
    raise AssertionError('mismatched public build configuration returned a launch command')
"""
    subprocess.run(  # noqa: S603 — same retained wheel, wrong public port, no service launch.
        [str(python), "-I", "-B", "-c", probe],
        cwd=root,
        env=os.environ.copy() | {"AVA_GATEWAY_PORT": "8001"},
        check=True,
        timeout=30,
    )


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    prefix = Path(sys.prefix).resolve()
    alias = Path(sys.argv[2])
    require(alias.is_symlink() and alias.resolve() == prefix, "entry alias did not select A")
    require(
        Path(sys.prefix).absolute() == alias.absolute(),
        "interpreter canonicalized the entry alias; this is not a moving-prefix proof",
    )
    require(WHEEL_RUNTIME, "proof did not load the installed wheel")
    frontend_command = frontend_service_cmd(43871)
    require(
        "npm" not in frontend_command and "build" not in frontend_command,
        "release frontend rebuilds",
    )
    require(
        str(prefix.parent / "frontend/node") in frontend_command,
        "frontend escaped loaded generation",
    )
    require(bool(required_migration_set()), "installed read-only schema inventory is unavailable")
    require(runtime_venv().resolve() == prefix, "venv escaped current prefix")
    python = runtime_python()
    prove_frontend_config_rejection(python, root)
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
        require(frontend_service_cmd(43871) == frontend_command, "selector changed frontend image")
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
    print(
        json.dumps(
            {
                "late_child_generation": str(prefix),
                "entry_prefix_was_alias": True,
                "missing_home_rejected": True,
            }
        )
    )


if __name__ == "__main__":
    main()
