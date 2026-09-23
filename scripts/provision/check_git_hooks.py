"""Warn about missing pre-push hooks or ephemeral interpreter pointers; never install."""

import os
import shlex
import subprocess
from pathlib import Path


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()  # noqa: S603 — fixed git calls only


def hook_problem(hook: Path, main_clone: Path) -> str:
    if not hook.is_file() or not os.access(hook, os.X_OK):
        return f"missing or non-executable {hook}"
    source = hook.read_text()
    if "# ID: 138fd403232d2ddd5efb44317e38bf03" not in source:
        return f"{hook} is not pre-commit-managed"
    if "--hook-type=pre-push" not in source:
        return f"{hook} does not dispatch the pre-push stage"
    assignments = [line for line in source.splitlines() if line.startswith("INSTALL_PYTHON=")]
    if len(assignments) != 1:
        return f"{hook} has no unique INSTALL_PYTHON pointer"
    values = shlex.split(assignments[0].removeprefix("INSTALL_PYTHON="))
    if len(values) != 1 or not values[0]:
        return f"{hook} has an empty INSTALL_PYTHON pointer"
    interpreter = Path(values[0])
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        return f"INSTALL_PYTHON is missing or non-executable: {interpreter}"
    # Do not resolve the executable symlink: venv Python normally links outside
    # the clone. Its lexical parent is the install location we need to protect.
    if not interpreter.is_absolute() or interpreter.parent != main_clone / ".venv/bin":
        return f"INSTALL_PYTHON drifts outside the main clone's stable .venv: {interpreter}"
    return ""


def main() -> None:
    try:
        common_dir = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir"))
        main_clone = Path(
            git("worktree", "list", "--porcelain").splitlines()[0].removeprefix("worktree ")
        )
        override = subprocess.run(
            ["git", "config", "--get", "core.hooksPath"],
            capture_output=True,
            text=True,
            check=False,
        )
        problem = (
            "core.hooksPath overrides the shared hooks directory; review and remove that override"
            if override.returncode == 0
            else hook_problem(common_dir / "hooks/pre-push", main_clone)
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        problem = f"cannot inspect pre-push installation: {exc}"
        location = "the main clone"
    else:
        location = shlex.quote(str(main_clone))
    if problem:
        print(f"WARNING: {problem}")
        print(
            f"From {location} (never an ephemeral worktree), run: "
            ".venv/bin/pre-commit install --hook-type pre-commit --hook-type pre-push"
        )
        print("Warn-only during hook rollout; independent CI checks remain the merge gate.")


if __name__ == "__main__":
    main()
