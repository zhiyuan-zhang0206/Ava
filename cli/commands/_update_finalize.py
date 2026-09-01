"""The always-run cleanup and reporting tail of gateway rollout orchestration."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from cli.commands._update_recover import RolloutOutcome
from shared.rollout_telemetry import RolloutTelemetry


def finalize_orchestration(
    *,
    hosts_to_resume: list[tuple[str, str | None]],
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
    skipped: list[str],
) -> None:
    """Resume hosts, close the record, and emit only clean commit telemetry.

    The local update can rotate data-plane credentials, so the settings refresh
    precedes every compensating write. The best-effort recovery finalizer remains
    in this `finally` tail, while only a fully clean commit is eligible to seed
    future maintenance-window estimates.
    """
    from ops.cluster import unpause_local_cluster
    from ops.cluster_pause import finalize_pause_owner_journal

    refresh_settings()
    unpause_local_cluster()
    finalize_pause_owner_journal()
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
    if skipped:
        print(
            f"\n⚠ {len(skipped)} agent-runner(s) unreachable and skipped: "
            f"{', '.join(sorted(skipped))} — never paused or updated by this rollout; "
            "they converge at the next rollout, or when `ava cluster update` runs on "
            "that host (`ava cluster status` shows them off-pin until then).",
            file=sys.stderr,
        )
    telemetry.print_summary()
