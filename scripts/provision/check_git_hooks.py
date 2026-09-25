"""Warn about missing Git hooks or ephemeral interpreter pointers; never install.

Default mode inspects the repository at the current directory and stays
warn-only (exit 0) during hook rollout. ``--scan-machine`` sweeps every
conventional local checkout on this machine — both dev clones and each home's
``source`` tree — and exits 1 when any of them reports a problem; that is the
form the converge warning step runs.
"""

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(  # noqa: S603 — fixed git calls only
        ["git", *args], text=True, cwd=cwd
    ).strip()


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


def hooks_path_override(checkout: Path) -> str:
    override = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )
    if override.returncode == 0:
        return (
            "core.hooksPath overrides the shared hooks directory; review and remove that override"
        )
    return ""


def inspect_checkout(checkout: Path) -> tuple[list[str], str | None]:
    """Problems for one checkout, plus its main-clone path (None when unknown)."""
    problems: list[str] = []
    try:
        common_dir = Path(
            git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=checkout)
        )
        main_clone = (
            git("worktree", "list", "--porcelain", cwd=checkout)
            .splitlines()[0]
            .removeprefix("worktree ")
        )
        override = hooks_path_override(checkout)
        if override:
            problems.append(override)
        for hook_type in ("pre-commit", "pre-push"):
            hook = common_dir / "hooks" / hook_type
            try:
                problem = hook_problem(hook)
            except (OSError, ValueError) as exc:
                problem = f"cannot inspect {hook}: {exc}"
            if problem:
                problems.append(problem)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        return [f"cannot inspect Git hook installation: {exc}"], None
    return problems, main_clone


def machine_checkouts(home: Path) -> list[Path]:
    """Conventional local checkouts: both dev clones and each home's source tree.

    Deliberately shallow — a bounded listing of ``home``, never a deep walk,
    which would trip macOS TCC prompts on operator machines.
    """
    candidates = [home / "Ava", home / "MyAva"]
    for directory in sorted(home.glob(".ava*")):
        candidates.append(directory / "source")
        candidates.append(directory)
    checkouts: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            if not (candidate / ".git").exists():
                continue
            key = str(candidate.resolve())
        except OSError:
            continue
        if key not in seen:
            seen.add(key)
            checkouts.append(candidate)
    return checkouts


def scan_machine() -> int:
    """Check every conventional local checkout; exit 1 when any problem is found.

    Linked worktrees of one clone are inspected once (their shared hooks live
    in the same ``git-common-dir``); the first discovered checkout represents
    the clone in both the report and the count.
    """
    clones: dict[str, Path] = {}
    for checkout in machine_checkouts(Path.home()):
        try:
            key = str(
                Path(
                    git(
                        "rev-parse",
                        "--path-format=absolute",
                        "--git-common-dir",
                        cwd=checkout,
                    )
                ).resolve()
            )
        except (OSError, ValueError, subprocess.CalledProcessError):
            key = str(checkout)  # unreadable: inspect it standalone so it still reports
        clones.setdefault(key, checkout)
    problems_total = 0
    for checkout in clones.values():
        problems, _ = inspect_checkout(checkout)
        for problem in problems:
            print(f"WARNING: [{checkout}] {problem}")
            problems_total += 1
    if problems_total:
        print(f"hook check: {problems_total} problem(s) across {len(clones)} clone(s)")
        print(
            "Reinstall from each main clone's stable interpreter (never a worktree): "
            ".venv/bin/pre-commit install --hook-type pre-commit --hook-type pre-push"
        )
        return 1
    print(f"hook check: OK ({len(clones)} clone(s))")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Warn about missing Git hooks or ephemeral interpreter pointers."
    )
    parser.add_argument(
        "--scan-machine",
        action="store_true",
        help="check every conventional checkout under $HOME instead of the current repository",
    )
    args = parser.parse_args(argv)
    if args.scan_machine:
        return scan_machine()
    problems, main_clone = inspect_checkout(Path.cwd())
    if problems:
        location = shlex.quote(main_clone) if main_clone else "the main clone"
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
