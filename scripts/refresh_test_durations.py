#!/usr/bin/env python3
"""Refresh .test_durations for pytest-split duration-balanced sharding.

CI fans the backend pytest suite over 12 shards and tests/e2e over 4 shards;
pytest-split balances them by per-test durations loaded from the repo-root
`.test_durations`. That file goes stale as the suite grows (new tests are
costed at the average), which is what produced the ~20% shard skew measured
in the 2026-08-30 CI investigation. This script re-measures BOTH suites the
same way CI runs them and rewrites the file:

* backend: `pytest tests/ --ignore=tests/e2e -m "not flaky" -n 4` plus CI's
  `--cov=agent --cov=ava ...` module list (coverage tracing is part of the
  CI shard environment, so durations measured without it run systematically
  faster and the split under-estimates);
* e2e: `pytest tests/e2e/ -n 2` (CI's e2e job carries no --cov).

Each suite runs with `--store-durations --clean-durations` into a temp file,
so only tests that actually ran this refresh are kept (no stale entries for
deleted or renamed tests). The two temp files are merged, entries under
0.2s are dropped (they carry ~13% of runtime and only add noise), values are
rounded to 3 decimals, and `.test_durations` is rewritten in the committed
compact format (sorted keys, no indent, trailing newline) — atomically, via
tmp + rename, so an interrupted run can never leave a truncated file behind.

Run it manually after a significant test-suite change, or let the scheduled
`.github/workflows/refresh-test-durations.yml` do it nightly:

    uv run python scripts/refresh_test_durations.py

The script never writes the committed file in place: the suites write temp
files, and the final atomic write happens only after the backend run
succeeded. A failed e2e run still contributes whatever durations it recorded
(the pytest-split cache plugin writes on session finish even for a failed
run); if it recorded nothing, the previous e2e entries are kept. A missing
or corrupt `.test_durations` reads as empty (and is healed by the write);
an unexpected JSON shape is an error, not silently rebuilt.

Note: the first refresh after this feature lands rewrites the whole file
(the committed one was last written by a one-off script), after which each
refresh only touches the values that changed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_MIN_DURATION_SECONDS = 0.2
_BACKEND_WORKERS = 4  # mirrors the backend-shard job (-n 4)
_E2E_WORKERS = 2  # mirrors the e2e-shard job (-n 2)
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
    if not all(isinstance(value, (int, float)) for value in data.values()):
        raise SystemExit(f"non-numeric duration in {path}")
    return {str(nodeid): float(value) for nodeid, value in data.items()}


def _run_suite(
    targets: list[str],
    workers: int,
    durations_path: Path,
    *,
    coverage: bool,
) -> int:
    """Run one suite with duration recording; return the pytest exit code.

    The suite is invoked exactly as CI runs it (same worker count, marker
    filter, e2e exclusion and, for the backend suite, the same --cov module
    list), so the measured durations match what shard jobs will experience.
    Durations go to `durations_path`, never the committed file;
    `--clean-durations` replaces that target with this run's tests only.
    """
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *targets,
        "-n",
        str(workers),
        "--store-durations",
        "--clean-durations",
        f"--durations-path={durations_path}",
        "-q",
    ]
    if coverage:
        cmd.extend(_BACKEND_COVERAGE_ARGS)
    print(f"  running: {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=_REPO_ROOT, check=False).returncode  # noqa: S603 - fixed argv built from constants, no untrusted input


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


def main() -> int:
    previous = _load_durations(_DURATIONS_PATH)
    print(f"refreshing .test_durations (previous: {len(previous)} entries)")

    with tempfile.TemporaryDirectory(prefix="durations-") as tmp:
        tmp_dir = Path(tmp)
        backend_path = tmp_dir / "backend.json"
        e2e_path = tmp_dir / "e2e.json"

        backend_rc = _run_suite(
            ["tests/", "--ignore=tests/e2e", "-m", "not flaky"],
            _BACKEND_WORKERS,
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

        e2e_rc = _run_suite(["tests/e2e/"], _E2E_WORKERS, e2e_path, coverage=False)
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


if __name__ == "__main__":
    raise SystemExit(main())
