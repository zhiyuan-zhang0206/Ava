"""Prepare-phase checks and maintenance-window estimates for cluster rollouts."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

from cli.commands import _update_uv_sync
from shared.gitenv import git_env
from shared.paths import ava_home
from shared.proc import run_bounded
from shared.rollout_telemetry import RolloutTelemetry, record_bytes, stage

_BASELINE_FILE = "update-baseline.json"
_BASELINE_WINDOW = 10
_NO_BASELINE_MARGIN_S = 25.0
_DRY_RUN_GIT_TIMEOUT_S = 30.0
_DRY_RUN_IMPORT_TIMEOUT_S = 30.0
_DRY_RUN_UV_SYNC_TIMEOUT_S = 180.0
_RUNNER_PROBE_TIMEOUT_S = 5.0
_OFFSITE_PROBE_TIMEOUT_S = 5.0
_WINDOW_STAGES = ("stop_the_world", "local_leg", "readiness", "phase_b")
_SEED_STAGE_DURATIONS = {
    "stop_the_world": 8.0,
    "local_leg": 30.0,
    "readiness": 2.0,
    "phase_b": 45.0,
}


@dataclass(frozen=True)
class PrepareResult:
    """All evidence needed to decide whether a rollout may enter commit."""

    pull_recover: tuple[str, set[str], Path | None] | None
    failures: list[str]
    estimate_s: float


def _baseline_path() -> Path:
    return ava_home() / _BASELINE_FILE


def _seed_baseline() -> dict[str, object]:
    return {
        "stages": {name: [duration] for name, duration in _SEED_STAGE_DURATIONS.items()},
        "n": 0,
    }


def _load_maintenance_baseline(*, persist_seed: bool = True) -> dict[str, object]:
    path = _baseline_path()
    if not path.exists():
        baseline = _seed_baseline()
        if persist_seed:
            _write_maintenance_baseline(baseline)
        return baseline
    raw = cast(object, json.loads(path.read_text()))
    if not isinstance(raw, dict) or "stages" not in raw or not isinstance(raw["stages"], dict):
        raise TypeError(f"invalid maintenance baseline: {path}")
    stages = cast(dict[str, object], raw["stages"])
    for name in _WINDOW_STAGES:
        if name not in stages:
            raise TypeError(f"invalid maintenance baseline stage {name!r}: {path}")
        durations = stages[name]
        if not isinstance(durations, list) or not all(
            isinstance(duration, (int, float)) for duration in durations
        ):
            raise TypeError(f"invalid maintenance baseline stage {name!r}: {path}")
    if "n" not in raw:
        raise TypeError(f"invalid maintenance baseline count: {path}")
    n = raw["n"]
    if not isinstance(n, int) or n < 0:
        raise TypeError(f"invalid maintenance baseline count: {path}")
    return cast(dict[str, object], raw)


def _write_maintenance_baseline(baseline: Mapping[str, object]) -> None:
    path = _baseline_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline, sort_keys=True) + "\n")


def _p95(durations: list[float]) -> float:
    if not durations:
        raise RuntimeError("maintenance baseline has no durations")
    ordered = sorted(durations)
    return ordered[math.ceil(len(ordered) * 0.95) - 1]


def maintenance_window_breakdown(*, persist_seed: bool = True) -> dict[str, float]:
    """Return the p95 durations for exactly the commit-window stages."""
    baseline = _load_maintenance_baseline(persist_seed=persist_seed)
    stages = cast(dict[str, list[float]], baseline["stages"])
    return {name: _p95([float(value) for value in stages[name]]) for name in _WINDOW_STAGES}


def estimate_maintenance_window(*, persist_seed: bool = True) -> float:
    """Estimate commit duration from p95 telemetry, conservatively before the first run."""
    baseline = _load_maintenance_baseline(persist_seed=persist_seed)
    stages = cast(dict[str, list[float]], baseline["stages"])
    breakdown = {name: _p95([float(value) for value in stages[name]]) for name in _WINDOW_STAGES}
    estimate = sum(breakdown.values())
    n = cast(int, baseline["n"])
    return estimate + _NO_BASELINE_MARGIN_S if n == 0 else estimate


def maintenance_window_estimate_note(*, persist_seed: bool = True) -> str | None:
    """Explain the conservative margin when no completed commit has been observed yet."""
    baseline = _load_maintenance_baseline(persist_seed=persist_seed)
    return "no baseline — seeded + 25s margin" if cast(int, baseline["n"]) == 0 else None


def append_maintenance_baseline(stages: Mapping[str, float]) -> None:
    """Record one rollout's observed commit-stage durations, retaining the recent window."""
    baseline = _load_maintenance_baseline()
    baseline_stages = cast(dict[str, list[float]], baseline["stages"])
    n = cast(int, baseline["n"])
    for name in _WINDOW_STAGES:
        values = baseline_stages[name]
        duration: float | None = None
        if name in stages:
            duration = stages[name]
        if n == 0 and duration is not None:
            values.clear()
        if duration is not None:
            values.append(float(duration))
        del values[:-_BASELINE_WINDOW]
    baseline["n"] = min(n + 1, _BASELINE_WINDOW)
    _write_maintenance_baseline(baseline)


def _runner_reachability_failures() -> list[str]:
    """Probe every remote runner's ops health endpoint without changing its posture."""
    from shared.http_dial import get
    from shared.machines import list_agent_runners

    failures: list[str] = []
    for name, url in list_agent_runners():
        if url is None:
            continue
        try:
            response = get(f"{url.rstrip('/')}/healthz", timeout=_RUNNER_PROBE_TIMEOUT_S)
            response.raise_for_status()
        except Exception as exc:
            failures.append(f"runner {name} is unreachable ({type(exc).__name__})")
    return failures


def _probe_offsite_store() -> str | None:
    """Probe offsite reachability without holding up a commit decision."""
    from services.pitr.store_factory import get_store_group

    started = time.monotonic()
    failure: list[Exception] = []

    def _stat_probe() -> None:
        try:
            group = get_store_group()
            group.restartable_streaming_object_store()
            group.object_store().stat(f"ava-logical/probe/{uuid4().hex}")
        except Exception as exc:
            failure.append(exc)

    probe = threading.Thread(target=_stat_probe, daemon=True)
    probe.start()
    probe.join(_OFFSITE_PROBE_TIMEOUT_S)
    if probe.is_alive():
        return f"offsite store probe exceeded {_OFFSITE_PROBE_TIMEOUT_S:.0f}s"
    if failure:
        return f"offsite store probe unavailable ({type(failure[0]).__name__})"
    return f"offsite store probe ready in {time.monotonic() - started:.1f}s"


def _offsite_probe_message() -> str | None:
    """Make the optional probe non-blocking for every caller of prepare checks."""
    try:
        return _probe_offsite_store()
    except Exception as exc:
        return f"offsite store probe unavailable ({type(exc).__name__})"


@contextmanager
def _staging_worktree(
    repo: Path, target_sha: str, staging_dir: Path
) -> Generator[Path, None, None]:
    """Materialize and always remove a detached target-tree worktree."""
    if staging_dir.exists():
        raise RuntimeError(f"dry-run staging directory already exists: {staging_dir}")
    staging_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        prune = run_bounded(
            ["git", "worktree", "prune"],
            cwd=repo,
            env=git_env(),
            capture_output=True,
            text=True,
            timeout=_DRY_RUN_GIT_TIMEOUT_S,
        )
        if prune.returncode != 0:
            detail = (prune.stderr or "git worktree prune failed").strip()
            raise RuntimeError(f"could not prune stale target staging worktrees: {detail}")
        result = run_bounded(
            ["git", "worktree", "add", "--detach", str(staging_dir), target_sha],
            cwd=repo,
            env=git_env(),
            capture_output=True,
            text=True,
            timeout=_DRY_RUN_GIT_TIMEOUT_S,
        )
        if result.returncode != 0:
            detail = (result.stderr or "git worktree add failed").strip()
            raise RuntimeError(f"could not create target staging worktree: {detail}")
        yield staging_dir
    finally:
        if staging_dir.exists():
            run_bounded(
                ["git", "worktree", "remove", "--force", str(staging_dir)],
                cwd=repo,
                env=git_env(),
                capture_output=True,
                text=True,
                timeout=_DRY_RUN_GIT_TIMEOUT_S,
            )


def _warm_staging_uv(staging: Path) -> str | None:
    result = _update_uv_sync.run_uv_sync(staging, timeout_s=_DRY_RUN_UV_SYNC_TIMEOUT_S)
    if result.returncode != 0:
        return f"target build uv sync failed (rc={result.returncode})"
    return None


def _target_environment() -> dict[str, str]:
    """Load the current unit's .env into a child without constructing Settings here."""
    from dotenv import dotenv_values

    environment = os.environ.copy()
    environment["AVA_HOME"] = str(ava_home())
    environment.pop("AVA_PROCESS_PROFILE", None)
    environment.pop("AVA_CONFIG_FETCH", None)
    for key, value in dotenv_values(ava_home() / ".env").items():
        if value is not None:
            environment[key] = value
    return environment


def _target_python(staging: Path) -> Path:
    return staging / ".venv" / "bin" / "python"


def _run_target_python(
    staging: Path, code: str, *, timeout_s: float
) -> subprocess.CompletedProcess[str]:
    return run_bounded(
        [str(_target_python(staging)), "-c", code],
        cwd=staging,
        env=_target_environment(),
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )


def _validate_target_settings(staging: Path) -> str | None:
    """Construct the target tree's complete Settings model against the live .env image."""
    try:
        result = _run_target_python(
            staging,
            "from shared.config import Settings; Settings()",
            timeout_s=_DRY_RUN_IMPORT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"target Settings validation failed ({type(exc).__name__})"
    if result.returncode != 0:
        return "target Settings validation failed"
    return None


def _candidate_daemon_modules(staging: Path) -> list[str]:
    code = """
import json
import shlex
from ops.spec import services_for_capabilities
from shared.config import Settings
from shared.machine import machine_role

Settings()
modules = []
for spec in services_for_capabilities(machine_role()):
    argv = shlex.split(spec.cmd)
    if "-m" in argv:
        modules.append(argv[argv.index("-m") + 1])
print(json.dumps(modules))
"""
    result = _run_target_python(staging, code, timeout_s=_DRY_RUN_IMPORT_TIMEOUT_S)
    if result.returncode != 0:
        raise RuntimeError("could not enumerate target daemon modules")
    modules = json.loads(result.stdout)
    if not isinstance(modules, list) or not all(isinstance(module, str) for module in modules):
        raise RuntimeError("target daemon module list is invalid")
    return modules


def _import_candidate_modules(staging: Path) -> list[str]:
    """Import every `-m` target daemon module in a bounded fresh interpreter.

    Script-form daemon commands are deliberately not imported: their process
    entrypoints are exercised by the target's real startup path instead.
    """
    try:
        modules = _candidate_daemon_modules(staging)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return [f"could not enumerate target daemon modules ({type(exc).__name__})"]
    failures: list[str] = []
    for module in modules:
        try:
            result = _run_target_python(
                staging,
                f"import importlib; importlib.import_module({module!r})",
                timeout_s=_DRY_RUN_IMPORT_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f"target daemon import {module} failed ({type(exc).__name__})")
            continue
        if result.returncode != 0:
            failures.append(f"target daemon import {module} failed")
    return failures


def dry_run_checks(repo: Path, target_sha: str, *, staging_dir: Path) -> list[str]:
    """Run all blocking prepare checks and return their failure descriptions."""
    failures = _runner_reachability_failures()
    print(f"  [{'pass' if not failures else 'fail'}] runner reachability")
    offsite = _offsite_probe_message()
    if offsite is not None:
        print(f"  · {offsite} (informational)")
    try:
        with _staging_worktree(repo, target_sha, staging_dir) as staging:
            for label, failure in (
                ("target uv sync", _warm_staging_uv(staging)),
                ("target Settings", _validate_target_settings(staging)),
            ):
                print(f"  [{'pass' if failure is None else 'fail'}] {label}")
                if failure is not None:
                    failures.append(failure)
            import_failures = _import_candidate_modules(staging)
            print(f"  [{'pass' if not import_failures else 'fail'}] target daemon imports")
            failures.extend(import_failures)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        failures.append(f"target staging failed ({type(exc).__name__})")
        print("  [fail] target staging")
    return failures


def prepare_commit(
    repo: Path,
    target_sha: str | None,
    *,
    pull: bool,
    snapshotter: Callable[..., tuple[str, set[str], Path | None] | None],
    check_runner: Callable[..., list[str]],
    estimate_runner: Callable[[], float],
) -> PrepareResult:
    """Create local recovery evidence, then run all pre-commit checks and estimate."""
    pull_recover: tuple[str, set[str], Path | None] | None = None
    if pull:
        if target_sha is None:
            raise ValueError("prepare pull requires a target_sha")
        with stage("snapshot"):
            pull_recover = snapshotter(pull=True, target_sha=target_sha)
        if pull_recover is not None and pull_recover[2] is not None:
            with suppress(OSError):
                record_bytes("snapshot", pull_recover[2].stat().st_size)
    if target_sha is None:
        return PrepareResult(pull_recover=pull_recover, failures=[], estimate_s=estimate_runner())
    staging_dir = ava_home() / "tmp" / f"update-dry-run-{target_sha[:12]}-{uuid4().hex}"
    return PrepareResult(
        pull_recover=pull_recover,
        failures=check_runner(repo, target_sha, staging_dir=staging_dir),
        estimate_s=estimate_runner(),
    )


def spawn_async_offsite_upload(repo: Path, dump_path: Path | None) -> None:
    """Detach remote backup publication after recovery has finished."""
    if dump_path is None:
        return
    log = ava_home() / "backups" / "db" / f"upload-{dump_path.name}.log"
    try:
        log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with log.open("ab") as stream:
            subprocess.Popen(
                [sys.executable, "-m", "services.backup", "--publish-offsite", str(dump_path)],
                cwd=repo,
                start_new_session=True,
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
    except OSError as exc:
        print(
            f"  warning: async offsite upload could not start ({exc}); local artifact retained",
            file=sys.stderr,
        )
        return
    print(f"→ async offsite upload: {dump_path} -> {log} (detached)")


def finalize_commit_telemetry(telemetry: RolloutTelemetry) -> None:
    """Persist measured commit stages without adding preparation or upload time."""
    try:
        summary = telemetry.summary()
        stages = cast(dict[str, float], summary["stages"])
        append_maintenance_baseline(
            {name: float(stages[name]) for name in _WINDOW_STAGES if name in stages}
        )
    except Exception as exc:
        print(f"  warning: could not record maintenance baseline ({exc})", file=sys.stderr)
