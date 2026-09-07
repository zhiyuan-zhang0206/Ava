"""The always-run cleanup and reporting tail of gateway rollout orchestration."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from cli.commands._update_recover import RolloutOutcome
from shared.rollout_telemetry import RolloutTelemetry


def _unpause_local_via_tree(repo: Path) -> None:
    """Run compensating local unpause through the current tree's interpreter.

    `repo/.venv/bin/python` executes the code the rollout just deployed — the
    admission-aware unpause — rather than this orchestration process's launch-time
    modules. Falls back to the in-process unpause when the tree venv is absent
    (abort before the local leg, or tests with a dummy repo). Never raises: it
    runs in a `finally` tail that must not mask the rollout outcome.
    """
    python = repo / ".venv" / "bin" / "python"
    if python.exists():
        try:
            from cli.commands._update_dryrun import _target_environment

            result = subprocess.run(
                [
                    str(python),
                    "-c",
                    "from ops.cluster_pause import unpause_local_cluster; "
                    "unpause_local_cluster(); from ops.cluster_pause import "
                    "finalize_pause_owner_journal; finalize_pause_owner_journal()",
                ],
                cwd=repo,
                env=_target_environment(),
                timeout=60,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                return
            print(
                "  warning: deployed-tree compensating local unpause failed "
                f"(rc={result.returncode}); falling back to in-process code",
                file=sys.stderr,
            )
        except Exception as exc:
            print(
                "  warning: deployed-tree compensating local unpause raised "
                f"{type(exc).__name__}; falling back to in-process code",
                file=sys.stderr,
            )

    try:
        from ops.cluster import unpause_local_cluster
        from ops.cluster_pause import finalize_pause_owner_journal

        unpause_local_cluster()
        finalize_pause_owner_journal()
    except Exception as exc:
        print(
            f"  warning: in-process compensating local unpause raised {type(exc).__name__}",
            file=sys.stderr,
        )


def finalize_orchestration(
    *,
    hosts_to_resume: list[tuple[str, str | None]],
    phase_a_started: bool = True,
    fan_out: Callable[..., object],
    phase_a_timeout_s: float,
    outcome: RolloutOutcome,
    deploy_capability: object,
    pin_advanced: bool,
    failing_step: str | None,
    recovered: bool,
    local_launch_failures: list[str],
    telemetry: RolloutTelemetry,
    refresh_settings: Callable[[], None],
    finalize_rollout_runner: Callable[..., None],
    finalize_commit_telemetry: Callable[[RolloutTelemetry], None],
    spawn_offsite_upload: Callable[[Path, Path | None], None],
    repo: Path,
    pull_recover: tuple[str, set[str], Path | None] | None,
) -> None:
    """Resume hosts, close the record, and emit only clean commit telemetry.

    The local update can rotate data-plane credentials, so the settings refresh
    precedes every compensating write. The best-effort recovery finalizer remains
    in this `finally` tail, while only a fully clean commit is eligible to seed
    future maintenance-window estimates.
    """
    refresh_settings()
    if phase_a_started:
        _unpause_local_via_tree(repo)
    else:
        print("no Phase A pause occurred; skipping compensating local unpause", file=sys.stderr)
    finalize_rollout_runner(
        hosts_to_resume,
        fan_out,
        phase_a_timeout_s,
        outcome=outcome,
        deploy_capability=deploy_capability,
        pin_advanced=pin_advanced,
        failing_step=failing_step,
        recovered=recovered,
        local_launch_failures=local_launch_failures,
    )
    if outcome is RolloutOutcome.CLEAN:
        finalize_commit_telemetry(telemetry)
    spawn_offsite_upload(repo, pull_recover[2] if pull_recover is not None else None)
    telemetry.print_summary()
