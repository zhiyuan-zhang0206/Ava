"""Phase A drains every runner's hosted agents before any schema migration.

The local and remote acknowledgements use one deploy generation. Each proves
ordinary restart application, final checkpoint flush and actual continuation
completion. Any missing acknowledgement aborts while dependency APIs remain up.
"""

from __future__ import annotations

import sys

from cli.commands._update_fanout import (
    _PHASE_A_TIMEOUT_S,
    ClusterOpPayload,
    _print_fan_out_results,
)
from shared.rollout_telemetry import stage as _stage_telemetry


def _run_phase_a(
    agent_runners: list[tuple[str, str | None]],
    *,
    deploy_capability: ClusterOpPayload,
) -> set[str] | None:
    """Phase A: pause every agent-runner. Returns the names that **acked**, or None
    when a host answered 5xx and the rollout must abort (nothing has migrated yet, so
    the cluster can still recover; the caller's `finally` resumes anyone paused).

    The acked set is the answer to "which hosts did this rollout put into a
    transition?", and it is the only such answer available before Phase B hands out
    acks of its own — which is what the gateway-readiness bail-out needs to decide who
    the deploy lease should be held for.
    """
    import cli.commands as _ns

    if not agent_runners:
        print("\n→ Phase A: no agent-runner registered, single-host path")
        return set()
    print(f"\n→ Phase A: pause {len(agent_runners)} agent-runner(s)")
    from shared.config import settings

    results = _ns._fan_out(
        agent_runners,
        "/api/cluster/stop",
        max(_PHASE_A_TIMEOUT_S, settings.gateway.update_quiesce_timeout_seconds + 10),
        deploy_capability,
    )
    fatal = _print_fan_out_results("pause", results)
    acknowledged = {name for name, status, _ in results if status == "ok"}
    if fatal or acknowledged != {name for name, _ in agent_runners}:
        print(
            "\n✗ Phase A did not confirm every runner drained, aborting (schema untouched, cluster can still recover)",
            file=sys.stderr,
        )
        return None
    return {name for name, status, _ in results if status == "ok"}


def _stop_the_world(
    agent_runners: list[tuple[str, str | None]],
    *,
    mode: str = "smooth",
    deploy_capability: ClusterOpPayload,
) -> tuple[set[str] | None, bool]:
    """Drain every participating unit before allowing schema migration.

    Every acknowledgement includes actual flush and hosted continuation exit. Failure
    aborts; no timeout is converted to force authorization.
    """
    import cli.commands as _ns
    from ops.cluster import pause_local_cluster

    # Bind the local drain to the same generation as the fan-out (whose roster
    # can include this combined gateway/runner unit).
    with _stage_telemetry("phase_a_pause"):
        from datetime import datetime

        from shared import pause_owner

        if (
            "deploy_holder" not in deploy_capability
            or "deploy_acquired_at" not in deploy_capability
        ):
            raise ValueError("cluster drain requires the validated deploy generation")
        pause_owner.mark_paused(
            deploy_capability["deploy_holder"],
            datetime.fromisoformat(deploy_capability["deploy_acquired_at"]),
        )
        pause_local_cluster()
        paused_names = _run_phase_a(agent_runners, deploy_capability=deploy_capability)
    if paused_names is None:
        return None, False

    # Each remote acknowledgement includes final flush and continuation exit.
    # Re-verify the local hold before any source/schema change.
    from cli.commands import update as _up_mod

    with _stage_telemetry("quiesce_drain"):
        all_quiesced = _ns._quiesce_all_agents(timeout_s=_up_mod._quiesce_timeout_s(mode))
    return paused_names, all_quiesced
