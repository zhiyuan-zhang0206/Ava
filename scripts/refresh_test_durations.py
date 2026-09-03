#!/usr/bin/env python3
"""Refresh .test_durations for pytest-split duration-balanced sharding.

CI fans the backend pytest suite over 16 shards (see ci.yml backend-shard matrix)
and tests/e2e over 4 shards;
pytest-split uses its least_duration (LPT) algorithm to balance them by per-test
durations loaded from the repo-root `.test_durations`. That file goes stale as
the suite grows (new tests are costed at the average), which is what produced
the ~20% shard skew measured
in the 2026-08-30 CI investigation. This script re-measures BOTH suites the
same way CI runs them and rewrites the file:

* backend: `pytest tests/ --ignore=tests/e2e -m "not flaky" -n 4` plus CI's
  `--cov=agent --cov=ava ...` module list (coverage tracing is part of the
  CI shard environment, so durations measured without it run systematically
  faster and the split under-estimates);
* e2e: `pytest tests/e2e/ -v -n 2` (CI's e2e job carries no --cov).

The nightly workflow invokes `measure` once per CI-shaped shard (12 backend,
four e2e), each on its own runner. A measurement retries its isolated shard
three times, reseeding its temporary duration input before every attempt, so a
failed attempt cannot affect selection or leak partial measurements into its
retry. Every successful measurement uses `--store-durations --clean-durations`;
therefore its artifact contains only the tests that ran in that shard.
`merge` requires all 16 artifacts, combines them, drops entries below 0.2s,
rounds values to three decimals, and atomically rewrites `.test_durations` in
the committed compact format (sorted keys, no indent, trailing newline). An
interrupted run can never leave a truncated file behind.

Run it manually after a significant test-suite change, or let the scheduled
`.github/workflows/refresh-test-durations.yml` do it nightly:

    uv run python scripts/refresh_test_durations.py

`refresh` (the default when no command is given) preserves the prior local,
single-runner workflow for manual use. The nightly workflow instead uses the
isolated `measure` + `merge` path. A missing or corrupt `.test_durations`
reads as empty (and is healed by a successful merge); an unexpected JSON shape
is an error, not silently rebuilt.

Note: the first refresh after this feature lands rewrites the whole file
(the committed one was last written by a one-off script), after which each
refresh only touches the values that changed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_MIN_DURATION_SECONDS = 0.2
_BACKEND_WORKERS = 4  # mirrors the backend-shard job (-n 4)
_E2E_WORKERS = 2  # mirrors the e2e-shard job (-n 2)
_BACKEND_SHARDS = 12
_E2E_SHARDS = 4
_MEASUREMENT_ATTEMPTS = 3
# The exact --cov module list of the backend-shard job (ci.yml): the refresh
# carries it so the measured durations include the same tracing overhead the
# shards will pay.
_BACKEND_COVERAGE_ARGS = [
    "--cov=agent",
    "--cov=ava",
    "--cov=cli",
    "--cov=gateway",
    "--cov=shared",
    "--cov=ui",
    "--cov=ops",
    "--cov=services",
    "--cov=ava_builtins",
]
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DURATIONS_PATH = _REPO_ROOT / ".test_durations"


def _load_durations(path: Path) -> dict[str, float]:
    """Load a pytest-split durations file; missing or corrupt -> empty dict."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        print(
            f"warning: {path} is unreadable ({exc}); treating it as empty",
            file=sys.stderr,
        )
        return {}
    if not isinstance(data, dict):
        raise SystemExit(f"unexpected durations shape in {path}: {type(data).__name__}")
    if not all(
        isinstance(value, (int, float)) and not isinstance(value, bool) for value in data.values()
    ):
        raise SystemExit(f"non-numeric duration in {path}")
    return {str(nodeid): float(value) for nodeid, value in data.items()}


def _run_suite(
    pytest_args: list[str],
    durations_path: Path,
    *,
    coverage: bool,
) -> int:
    """Run one suite with duration recording; return the pytest exit code.

    The suite is invoked exactly as CI runs it (same worker count, marker
    filter, e2e exclusion, least_duration algorithm and, for the backend suite,
    the same --cov module list), so the measured durations match what shard jobs
    will experience.
    Durations go to `durations_path`, never the committed file;
    `--clean-durations` replaces that target with this run's tests only.
    """
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *pytest_args,
    ]
    if coverage:
        cmd.extend(_BACKEND_COVERAGE_ARGS)
    cmd.extend(
        [
            "--store-durations",
            "--clean-durations",
            f"--durations-path={durations_path}",
        ]
    )
    print(f"  running: {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=_REPO_ROOT, check=False).returncode  # noqa: S603 - fixed argv built from constants, no untrusted input


def _seed_shard_durations(path: Path) -> None:
    """Give each shard CI's current timing model without sharing a writable file."""
    source = _load_durations(_DURATIONS_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(source, sort_keys=True, separators=(",", ":")) + "\n")


def _measure_shard(suite: str, group: int, output_path: Path) -> int:
    """Record one CI-equivalent shard, retrying only that isolated measurement."""
    if suite == "backend":
        groups = _BACKEND_SHARDS
        pytest_args = [
            "tests/",
            "-q",
            "--ignore=tests/e2e",
            "-m",
            "not flaky",
            "-n",
            str(_BACKEND_WORKERS),
            "--splits",
            str(groups),
            "--group",
            str(group),
            "--splitting-algorithm",
            "least_duration",
        ]
        coverage = True
    else:
        groups = _E2E_SHARDS
        pytest_args = [
            "tests/e2e/",
            "-v",
            "-n",
            str(_E2E_WORKERS),
            "--splits",
            str(groups),
            "--group",
            str(group),
            "--splitting-algorithm",
            "least_duration",
        ]
        coverage = False

    if not 1 <= group <= groups:
        print(f"{suite} group must be between 1 and {groups}, got {group}", file=sys.stderr)
        return 1
    if output_path.resolve() == _DURATIONS_PATH.resolve():
        print("shard output must not overwrite .test_durations", file=sys.stderr)
        return 1

    for attempt in range(1, _MEASUREMENT_ATTEMPTS + 1):
        _seed_shard_durations(output_path)
        result = _run_suite(pytest_args, output_path, coverage=coverage)
        if result == 0:
            return 0
        print(
            f"{suite} shard {group}/{groups} failed (exit {result}; "
            f"attempt {attempt}/{_MEASUREMENT_ATTEMPTS})",
            file=sys.stderr,
        )
    return 1


def _expected_measurement_paths(durations_dir: Path) -> list[Path] | None:
    """Return all required artifacts, or fail before an incomplete timing model publishes."""
    paths = [
        *(durations_dir / f"backend-{group}.json" for group in range(1, _BACKEND_SHARDS + 1)),
        *(durations_dir / f"e2e-{group}.json" for group in range(1, _E2E_SHARDS + 1)),
    ]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        print(f"missing shard measurements: {', '.join(missing)}", file=sys.stderr)
        return None
    return paths


def _merge_shard_measurements(durations_dir: Path) -> int:
    """Merge complete shard artifacts into the committed timings file atomically."""
    paths = _expected_measurement_paths(durations_dir)
    if paths is None:
        return 1

    merged: dict[str, float] = {}
    for path in paths:
        measured = _load_durations(path)
        if not measured:
            print(f"empty shard measurement: {path}", file=sys.stderr)
            return 1
        duplicate_nodeids = merged.keys() & measured.keys()
        if duplicate_nodeids:
            print(
                f"duplicate test durations in shard measurements: {sorted(duplicate_nodeids)}",
                file=sys.stderr,
            )
            return 1
        merged.update(measured)

    refreshed = _write_durations(merged)
    print(f"merged {len(paths)} shard measurements into {len(refreshed)} duration entries")
    return 0


def _write_durations(durations: dict[str, float]) -> dict[str, float]:
    """Atomically write the canonical compact JSON format + trailing newline."""
    trimmed = {
        nodeid: round(value, 3)
        for nodeid, value in durations.items()
        if round(value, 3) >= _MIN_DURATION_SECONDS
    }
    content = json.dumps(trimmed, sort_keys=True, separators=(",", ":")) + "\n"
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=_DURATIONS_PATH.parent,
            prefix=".test_durations.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = tmp.name
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        Path(tmp_path).replace(_DURATIONS_PATH)
        tmp_path = None
    finally:
        if tmp_path is not None:
            Path(tmp_path).unlink(missing_ok=True)
    return trimmed


def _refresh_all() -> int:
    previous = _load_durations(_DURATIONS_PATH)
    print(f"refreshing .test_durations (previous: {len(previous)} entries)")

    with tempfile.TemporaryDirectory(prefix="durations-") as tmp:
        tmp_dir = Path(tmp)
        backend_path = tmp_dir / "backend.json"
        e2e_path = tmp_dir / "e2e.json"

        backend_rc = _run_suite(
            [
                "tests/",
                "-q",
                "--ignore=tests/e2e",
                "-m",
                "not flaky",
                "-n",
                str(_BACKEND_WORKERS),
            ],
            backend_path,
            coverage=True,
        )
        if backend_rc != 0:
            print(
                f"backend suite failed (exit {backend_rc}); "
                "aborting without touching .test_durations",
                file=sys.stderr,
            )
            return 1

        e2e_rc = _run_suite(
            ["tests/e2e/", "-v", "-n", str(_E2E_WORKERS)],
            e2e_path,
            coverage=False,
        )
        if e2e_rc != 0:
            print(
                f"warning: e2e suite failed (exit {e2e_rc}); using whatever durations it recorded",
                file=sys.stderr,
            )

        merged = _load_durations(backend_path)
        merged.update(_load_durations(e2e_path))
        if all(not nodeid.startswith("tests/e2e/") for nodeid in merged):
            # e2e recorded nothing (e.g. collection error); keep the previous
            # e2e entries rather than dropping them from the split.
            merged.update(
                {
                    nodeid: value
                    for nodeid, value in previous.items()
                    if nodeid.startswith("tests/e2e/")
                }
            )

        refreshed = _write_durations(merged)

    e2e_count = sum(1 for nodeid in refreshed if nodeid.startswith("tests/e2e/"))
    print(
        f"wrote {len(refreshed)} entries to {_DURATIONS_PATH.name} "
        f"({len(refreshed) - e2e_count} backend, {e2e_count} e2e; "
        f"estimated total serial: {sum(refreshed.values()):.0f}s; "
        f"previous: {len(previous)} entries)"
    )
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("refresh", help="run both suites in one local process (default)")

    measure = commands.add_parser("measure", help="record one isolated CI-shaped shard")
    measure.add_argument("suite", choices=("backend", "e2e"))
    measure.add_argument("--group", type=int, required=True)
    measure.add_argument("--output", type=Path, required=True)

    merge = commands.add_parser("merge", help="merge all isolated shard measurements")
    merge.add_argument("--durations-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Dispatch the legacy local refresh or the nightly isolation commands."""
    args = _parse_args([] if argv is None else argv)
    if args.command in (None, "refresh"):
        return _refresh_all()
    if args.command == "measure":
        return _measure_shard(args.suite, args.group, args.output)
    if args.command == "merge":
        return _merge_shard_measurements(args.durations_dir)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
