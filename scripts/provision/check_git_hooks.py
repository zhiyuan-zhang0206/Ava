"""Warn about missing Git hooks or ephemeral interpreter pointers; never install."""

import os
import shlex
import subprocess
from pathlib import Path


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()  # noqa: S603 — fixed git calls only


def hook_problem(hook: Path) -> str:
    if not hook.is_file() or not os.access(hook, os.X_OK):
        return f"missing or non-executable {hook}"
    source = hook.read_text()
    if "# ID: 138fd403232d2ddd5efb44317e38bf03" not in source:
        return f"{hook} is not pre-commit-managed"
    if f"--hook-type={hook.name}" not in source:
        return f"{hook} does not dispatch the {hook.name} stage"
    assignments = [line for line in source.splitlines() if line.startswith("INSTALL_PYTHON=")]
    if len(assignments) != 1:
        return f"{hook} has no unique INSTALL_PYTHON pointer"
    values = shlex.split(assignments[0].removeprefix("INSTALL_PYTHON="))
    if len(values) != 1 or not values[0]:
        return f"{hook} has an empty INSTALL_PYTHON pointer"
    interpreter = Path(values[0])
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        return f"{hook}: INSTALL_PYTHON is missing or non-executable: {interpreter}"
    # Do not resolve the executable symlink: venv Python normally links outside
    # the venv. Reject disposable worktree locations, allowing stable uv tools.
    if {"worktrees", ".worktrees"}.intersection(interpreter.parts):
        return f"{hook}: INSTALL_PYTHON points into a disposable worktree: {interpreter}"
    return ""


def main() -> None:
    problems = []
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
        if override.returncode == 0:
            problems.append(
                "core.hooksPath overrides the shared hooks directory; review and remove that override"
            )
        for hook_type in ("pre-commit", "pre-push"):
            hook = common_dir / "hooks" / hook_type
            try:
                problem = hook_problem(hook)
            except (OSError, ValueError) as exc:
                problem = f"cannot inspect {hook}: {exc}"
            if problem:
                problems.append(problem)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        problems.append(f"cannot inspect Git hook installation: {exc}")
        location = "the main clone"
    else:
        location = shlex.quote(str(main_clone))
    if problems:
        for problem in problems:
            print(f"WARNING: {problem}")
        print(
            f"From {location} (never an ephemeral worktree), run: "
            ".venv/bin/pre-commit install --hook-type pre-commit --hook-type pre-push"
        )
        print(
            "If the main clone's runtime venv is protected, use a stable user-level runner: "
            "env -u VIRTUAL_ENV uv tool install pre-commit; then from the main clone run "
            "~/.local/bin/pre-commit install --hook-type pre-commit --hook-type pre-push"
        )
        print("Warn-only during hook rollout; independent CI checks remain the merge gate.")


if __name__ == "__main__":
    main()
