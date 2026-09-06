#!/usr/bin/env python3
"""Check or regenerate visual baselines in the current runner environment."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Pytest reserves exit codes 0-5, so the workflow can distinguish drift from
# every native pytest outcome without inspecting prose itself.
VISUAL_DRIFT_EXIT_CODE = 10

_REPO_ROOT = Path(__file__).resolve().parents[1]
_VISUAL_TEST_MODULE = "tests/e2e/test_visual_regression.py"
_VISUAL_TEST_COMMAND = ["uv", "run", "pytest", _VISUAL_TEST_MODULE]
_GIT_STATUS_COMMAND = ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"]
_BASELINE_PATHS_BY_TEST = {
    "test_home_visual_regression": Path(
        "tests/e2e/__snapshots__/test_visual_regression/test_home_visual_regression/home.png"
    ),
    "test_fleet_visual_regression": Path(
        "tests/e2e/__snapshots__/test_visual_regression/test_fleet_visual_regression/fleet.png"
    ),
    "test_mobile_visual_regression": Path(
        "tests/e2e/__snapshots__/test_visual_regression/test_mobile_visual_regression/mobile.png"
    ),
}
_FAILED_SUMMARY = re.compile(r"^FAILED\s+(?P<nodeid>\S+)\s+-\s+(?P<reason>.*)$")
_ERROR_SUMMARY = re.compile(r"^ERROR\s+")
_PIXEL_DRIFT_REASON = re.compile(
    r"^Failed: Visual regression: \d+/\d+ pixels changed \([^)]+\); "
    r"allowed ratio is [^)]+\.$"
)
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class BaselineRefreshError(Exception):
    """A refresh failed without producing a safe, complete baseline set."""

    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _capture(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv contains no untrusted input
        command,
        cwd=_REPO_ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _run_visual_tests() -> subprocess.CompletedProcess[str]:
    result = _capture(_VISUAL_TEST_COMMAND)
    if result.stdout:
        sys.stdout.write(result.stdout)
    return result


def _failure_summaries(output: str) -> tuple[list[tuple[str, str]], bool]:
    failures: list[tuple[str, str]] = []
    has_error = False
    for raw_line in output.splitlines():
        line = _ANSI_ESCAPE.sub("", raw_line)
        if match := _FAILED_SUMMARY.match(line):
            failures.append((match["nodeid"], match["reason"]))
        elif _ERROR_SUMMARY.match(line):
            has_error = True
    return failures, has_error


def _is_pixel_drift(result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode != 1:
        return False
    failures, has_error = _failure_summaries(result.stdout or "")
    expected_nodeids = {
        f"{_VISUAL_TEST_MODULE}::{test_name}" for test_name in _BASELINE_PATHS_BY_TEST
    }
    return (
        bool(failures)
        and not has_error
        and all(
            nodeid in expected_nodeids and _PIXEL_DRIFT_REASON.fullmatch(reason)
            for nodeid, reason in failures
        )
    )


def _check_only() -> int:
    result = _run_visual_tests()
    if result.returncode == 0:
        return 0
    if _is_pixel_drift(result):
        return VISUAL_DRIFT_EXIT_CODE
    return result.returncode


def _candidate_generation_succeeded(result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode != 1:
        return False
    output = _ANSI_ESCAPE.sub("", result.stdout or "")
    failures, has_error = _failure_summaries(output)
    expected_nodeids = {
        f"{_VISUAL_TEST_MODULE}::{test_name}" for test_name in _BASELINE_PATHS_BY_TEST
    }
    if has_error or {nodeid for nodeid, _reason in failures} != expected_nodeids:
        return False
    if len(failures) != len(expected_nodeids):
        return False
    return all(
        (
            f"Generated visual baseline candidate at {_REPO_ROOT / relative_path}. "
            "Review and commit it before rerunning the test."
        )
        in output
        for relative_path in _BASELINE_PATHS_BY_TEST.values()
    )


def _changed_paths(porcelain: str) -> list[Path]:
    fields = porcelain.split("\0")
    changed: list[Path] = []
    index = 0
    while index < len(fields) and fields[index]:
        entry = fields[index]
        if len(entry) < 4 or entry[2] != " ":
            raise BaselineRefreshError(f"could not parse git status entry: {entry!r}")
        status = entry[:2]
        changed.append(Path(entry[3:]))
        index += 1
        if "R" in status or "C" in status:
            if index >= len(fields) or not fields[index]:
                raise BaselineRefreshError("git status ended inside a rename or copy entry")
            changed.append(Path(fields[index]))
            index += 1
    return changed


def _restore_originals(backups: dict[Path, Path]) -> None:
    for reference, backup in backups.items():
        if reference.exists():
            reference.unlink()
        reference.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(backup), str(reference))


def _require_complete_candidate_generation(
    result: subprocess.CompletedProcess[str], references: list[Path]
) -> None:
    if not _candidate_generation_succeeded(result):
        exit_code = result.returncode if result.returncode != 0 else 1
        raise BaselineRefreshError(
            "visual tests did not fail exclusively with the missing-reference candidate signature",
            exit_code=exit_code,
        )

    missing_candidates = [path for path in references if not path.is_file()]
    if missing_candidates:
        missing = ", ".join(str(path.relative_to(_REPO_ROOT)) for path in missing_candidates)
        raise BaselineRefreshError(f"visual tests did not write every candidate: {missing}")


def _require_png_only_status(status: subprocess.CompletedProcess[str]) -> None:
    if status.returncode != 0:
        if status.stdout:
            sys.stderr.write(status.stdout)
        raise BaselineRefreshError(
            "git status failed after visual baseline generation",
            exit_code=status.returncode,
        )
    non_png_paths = [
        path for path in _changed_paths(status.stdout or "") if path.suffix.lower() != ".png"
    ]
    if non_png_paths:
        changed = ", ".join(str(path) for path in sorted(non_png_paths))
        raise BaselineRefreshError(f"non-PNG paths changed: {changed}")


def _regenerate() -> int:
    references = [_REPO_ROOT / path for path in _BASELINE_PATHS_BY_TEST.values()]
    missing_originals = [path for path in references if not path.is_file()]
    if missing_originals:
        missing = ", ".join(str(path.relative_to(_REPO_ROOT)) for path in missing_originals)
        raise BaselineRefreshError(f"cannot regenerate without committed references: {missing}")

    with tempfile.TemporaryDirectory(prefix="ava-visual-baselines-") as temporary_directory:
        backup_root = Path(temporary_directory)
        backups: dict[Path, Path] = {}
        try:
            for reference in references:
                backup = backup_root / reference.name
                shutil.move(str(reference), str(backup))
                backups[reference] = backup

            result = _run_visual_tests()
            _require_complete_candidate_generation(result, references)
            _require_png_only_status(_capture(_GIT_STATUS_COMMAND))
        except BaseException:
            _restore_originals(backups)
            raise

    print("Regenerated all visual baselines; only PNG paths changed.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check-only", action="store_true")
    mode.add_argument("--regenerate", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.check_only:
            return _check_only()
        return _regenerate()
    except BaselineRefreshError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
