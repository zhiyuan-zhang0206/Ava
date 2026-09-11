"""
CLI entry for `ava cluster update` — a thin POST client to the gateway.

Every CLI — gateway-capable host, pure agent-runner, or neither — POSTs to
the gateway's URL (user ruling 2026-08-21, issue #216): the gateway is the
only place a cluster update can be orchestrated, and it is reachable from
every enrolled host by construction. There is deliberately NO machine_role()
read on this path (the allowlist lint in scripts/lint_code_structure.py
enforces it) — routing an operation by role is exactly what the ruling bans.

- default        -> POST /api/cluster/rollout  (origin / mode / force)
- --restart-only -> POST /api/cluster/restart  (origin / mode)
- --local        -> in-process orchestration, no POST — the escape hatch the
                    detached rollout/restart sessions themselves run
                    (`spawn_rollout` uses `--local`; `spawn_restart` uses
                    `--local --restart-only`), and the debugging path. An
                    explicit flag, not a role branch: the user asked for the
                    foreground leg on whatever host they are on.

Re-imported by `cli/commands/update.py` (and re-exported through `cli.commands`)
so `cli.commands(.update).cmd_update` keeps resolving.
"""

from __future__ import annotations

import sys

from shared.deploy_timing import CLUSTER_DISPATCH_TIMEOUT_S
from shared.proc import hosting_supervised_session


def cmd_update(
    *,
    restart_only: bool = False,
    local: bool = False,
    force: bool = False,
    dry_run: bool = False,
    origin: str | None = None,
    rollout_log: str | None = None,
    mode: str = "smooth",
    target: str | None = None,
    target_sha: str | None = None,
) -> int:
    """`ava cluster update` — POST the operation to the gateway, from any host.

    Default: POST /api/cluster/rollout, which spawns the detached
    `ava-rollout` session running the full three-phase orchestration (pause
    agent-runners -> gateway local pull/sync/migrate/restart -> fan out the
    agent-runner self-updates) and returns 202 immediately. The response
    names the orchestration session + log; poll `ava cluster status` for the
    cluster to come back.

    `restart_only=True` (`--restart-only`) POSTs /api/cluster/restart instead —
    bounce every service on the current code with no pull / sync / migration.

    `local=True` (`--local`) runs the in-process orchestration in this
    foreground process instead of POSTing. This is what the detached
    `ava-rollout` and `ava-cluster-restart` sessions run (so neither re-POSTs
    and recurses), and it is the debugging path. Refused with exit 2 when this
    process is hosted inside a supervised session — the stop leg would kill
    its own orchestration (`shared.proc.hosting_supervised_session`); the
    detached orchestration sessions themselves are exempt.

    `origin` (`--origin <who>`) names the trigger; recorded in the rollout
    log and the cluster pin's `updated_by`. Defaults to `cli:<machine>`.

    `rollout_log` (`--rollout-log <path>`) is internal metadata from the
    detached rollout session. The local orchestration stamps it onto the
    last-update record so status surfaces can identify this run's log.

    `force=True` (`--force`) starts the rollout even though a deploy is in
    flight somewhere in the cluster (overrides the deploy-window check only —
    a crashed rollout still holds cluster_update_lock, which `--force` does
    not clear; that is `ava cluster recover`'s job).

    `mode` (`--mode smooth|force`) is the agent-drain policy before the
    rollout restarts processes.

    `target` (`--target <machine>`) triggers ONE machine's per-host update
    instead of the whole-cluster rollout: POSTs the gateway's
    `/api/cluster/update` relay, whose op spawns that machine's detached
    updater (pause-first; no cluster-wide lease — the cluster pin does not
    move). For bootstrap/convergence chases in mixed-version windows, where a
    cluster-wide rollout must not run. Mutually exclusive with
    `--restart-only`, `--local`, `--force`, `--dry-run`, and an explicit
    `--mode force` (the default `smooth` cannot be told apart from an explicit
    one); `target_sha` (`--target-sha`) pins the updater's force-checkout
    commit.

    `dry_run=True` (`--dry-run`) also prints a read-only per-target cohort
    readiness report before dispatching (`cli/commands/_update_cohort.py`).
    """
    from shared import maintenance

    if target is None and target_sha is not None:
        print("✗ --target-sha requires --target", file=sys.stderr)
        return 2
    maintenance.require_released("cluster update")
    if dry_run and target is None:
        from cli.commands._update_cohort import print_cohort_readiness

        print_cohort_readiness()
    if target is not None:
        return _dispatch_target(
            target=target,
            target_sha=target_sha,
            restart_only=restart_only,
            local=local,
            force=force,
            dry_run=dry_run,
            mode=mode,
            origin=origin,
        )
    if local:
        # --local wins over --restart-only (their historical combination was the
        # in-process restart-only orchestration).
        return _run_in_process(
            restart_only=restart_only,
            origin=origin,
            rollout_log=rollout_log,
            mode=mode,
            dry_run=dry_run,
        )
    if restart_only:
        return _post_cluster_restart(origin=origin, mode=mode)
    return _post_cluster_rollout(origin=origin, mode=mode, force=force, dry_run=dry_run)


def _dispatch_target(
    *,
    target: str,
    target_sha: str | None,
    restart_only: bool,
    local: bool,
    force: bool,
    dry_run: bool,
    mode: str,
    origin: str | None,
) -> int:
    """`--target` routing: validate the flag combination, then POST the per-host relay."""
    conflicts = [
        flag
        for flag, present in (
            ("--restart-only", restart_only),
            ("--local", local),
            ("--force", force),
            ("--dry-run", dry_run),
            ("--mode", mode != "smooth"),
        )
        if present
    ]
    if conflicts:
        print(
            "✗ --target triggers a single machine's per-host update via "
            "POST /api/cluster/update and cannot be combined with "
            f"{', '.join(conflicts)}; drop the flag(s) or drop --target for a "
            "whole-cluster rollout.",
            file=sys.stderr,
        )
        return 2
    if origin is not None:
        print(
            "note: --origin is not transmitted by the per-host (--target) path; ignored",
            file=sys.stderr,
        )
    return _post_cluster_update_target(target=target, target_sha=target_sha)


def _run_in_process(
    *,
    restart_only: bool,
    origin: str | None,
    rollout_log: str | None,
    mode: str,
    dry_run: bool,
) -> int:
    """The foreground orchestration leg (`--local`, optionally combined with
    `--restart-only`). No role check: the user explicitly asked for the local
    leg on whatever host they are on; a host that cannot run the orchestration
    fails loudly inside it."""
    from cli.commands import update as _up_mod

    repo = _up_mod._repo_root()
    home = _up_mod.ava_home()
    record = _up_mod.get_record(home)
    registry = f", registry: {record.name}" if record else ", registry: (unregistered)"
    print(
        f"[ava cluster update] cwd = {repo}, cluster home = {home}{registry}"
        f"{' (restart-only)' if restart_only else ''}"
    )
    # In-process legs stop this host's services (and quiesce/reap its agents +
    # shells). A run hosted inside one of those process trees is killed by its
    # own stop leg mid-flight — 2026-08-12: an agent's pty-hosted shell ran
    # `ava cluster update --local`, the stop leg force-killed
    # ava-pty-supervisor's whole tree (rollout included), and the cluster
    # stranded paused with services down. The detached orchestration sessions
    # (`spawn_rollout`/`spawn_update`/`spawn_restart`) are exempt inside
    # `hosting_supervised_session` — they are the auto-updater shape: the
    # trigger returns immediately and a process that outlives every stopped
    # service does the stop/update/start.
    hosting = hosting_supervised_session()
    if hosting is not None:
        print(
            f"\n✗ in-process update refused: this process runs inside supervised session "
            f"{hosting!r}, which this host's own stop leg kills — tree included, this "
            "update with it, stranding the host mid-transition. Use the default "
            "`ava cluster update` (POSTs the gateway, which runs the rollout in a "
            "detached session), or run from a shell no ava session hosts (e.g. a plain "
            "ssh/login shell).",
            file=sys.stderr,
        )
        return 2

    import cli.commands as _ns
    from shared.machine import machine_name

    return _ns._run_gateway_orchestration(
        repo,
        restart_only=restart_only,
        origin=origin or f"cli:{machine_name()}",
        rollout_log=rollout_log,
        mode=mode,
        dry_run=dry_run,
    )


def _post_cluster_rollout(*, origin: str | None, mode: str, force: bool, dry_run: bool) -> int:
    """POST /api/cluster/rollout to the gateway and translate the endpoint's
    ordinary answers into clean CLI exits (the two refusals a second operator
    is most likely to see — deploy-window conflict and nothing-to-update —
    were previously delivered as raw tracebacks)."""
    import httpx

    from shared.http_dial import post as dial_post
    from shared.machine import (
        GatewayApiBaseMissing,
        gateway_api_base,
        gateway_auth_headers,
        machine_name,
    )

    try:
        url = f"{gateway_api_base()}/api/cluster/rollout"
    except GatewayApiBaseMissing as exc:
        print(f"✗ cannot resolve gateway URL: {exc}", file=sys.stderr)
        return 1
    try:
        resp = dial_post(
            url,
            timeout=CLUSTER_DISPATCH_TIMEOUT_S,
            headers=gateway_auth_headers(),
            json={
                "origin": origin or f"cli:{machine_name()}",
                "mode": mode,
                "force": force,
                "dry_run": dry_run,
            },
        )
    except httpx.TimeoutException as exc:
        print(
            f"✗ gateway at {url} did not respond within {CLUSTER_DISPATCH_TIMEOUT_S:g}s: {exc}",
            file=sys.stderr,
        )
        return 1
    except httpx.TransportError as exc:
        print(f"✗ gateway unreachable at {url}: {exc}", file=sys.stderr)
        return 1
    if resp.status_code == 409:
        print(f"✗ {resp.json().get('detail', 'update already in flight')}", file=sys.stderr)
        return 1
    if resp.status_code == 422:
        # Not a failure: the cluster is already on the latest code. Exit 0 so a
        # scripted `ava cluster update` in a chain is not tripped by a no-op.
        print(f"· {resp.json().get('detail', 'nothing to update')}", file=sys.stderr)
        return 0
    if resp.status_code == 400:
        print(
            f"✗ {resp.json().get('detail', 'rollout refused')} "
            "(the gateway you POST to must be gateway-capable)",
            file=sys.stderr,
        )
        return 1
    if resp.status_code == 503:
        print(
            f"✗ {resp.json().get('detail', 'could not start the orchestration session')}",
            file=sys.stderr,
        )
        return 1
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        print(f"✗ gateway returned HTTP {resp.status_code} for {url}: {exc}", file=sys.stderr)
        return 1
    body = resp.json()
    print(f"  ✓ dispatched: session={body.get('session')} log={body.get('log')}")
    if body.get("needs_replay") is True:
        print("  ⚠ half-deployed state — replaying the rollout to reconcile installed code")
    if dry_run:
        print(
            "  dry-run dispatched — prepare-check PASS/FAIL and the informational estimate "
            "are in the rollout log."
        )
        return 0
    print("  poll `ava cluster status` for the cluster to return.")
    return 0


def _post_cluster_restart(*, origin: str | None, mode: str) -> int:
    """POST /api/cluster/restart to the gateway — the `--restart-only` leg
    (bounce every service on the current code). Same thin-client shape as the
    rollout POST; the endpoint ignores `force`."""
    import httpx

    from shared.http_dial import post as dial_post
    from shared.machine import (
        GatewayApiBaseMissing,
        gateway_api_base,
        gateway_auth_headers,
        machine_name,
    )

    try:
        url = f"{gateway_api_base()}/api/cluster/restart"
    except GatewayApiBaseMissing as exc:
        print(f"✗ cannot resolve gateway URL: {exc}", file=sys.stderr)
        return 1
    try:
        resp = dial_post(
            url,
            timeout=CLUSTER_DISPATCH_TIMEOUT_S,
            headers=gateway_auth_headers(),
            json={"origin": origin or f"cli:{machine_name()}", "mode": mode},
        )
    except httpx.TimeoutException as exc:
        print(
            f"✗ gateway at {url} did not respond within {CLUSTER_DISPATCH_TIMEOUT_S:g}s: {exc}",
            file=sys.stderr,
        )
        return 1
    except httpx.TransportError as exc:
        print(f"✗ gateway unreachable at {url}: {exc}", file=sys.stderr)
        return 1
    if resp.status_code == 409:
        print(f"✗ {resp.json().get('detail', 'restart already in flight')}", file=sys.stderr)
        return 1
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        print(f"✗ gateway returned HTTP {resp.status_code} for {url}: {exc}", file=sys.stderr)
        return 1
    body = resp.json()
    print(f"  ✓ dispatched: session={body.get('session')} log={body.get('log')}")
    print("  poll `ava cluster status` for the cluster to return.")
    return 0


def _post_cluster_update_target(*, target: str, target_sha: str | None) -> int:
    """`ava cluster update --target` — trigger ONE machine's detached updater via
    the gateway's per-host primitive (POST /api/cluster/update).

    The same relay a Phase-B fan-out and a watchdog off-pin self-heal dial: the
    gateway POSTs a `cluster_update` op to the target's ops server, which spawns
    the detached updater (pause-first, no cluster-wide lease — the cluster pin
    does not move). 202 lands the updater session's name + log; 503 means the
    target's ops server was unreachable; 502 means the op ran but reported
    failure (an updater may already be in flight there).
    """
    import httpx

    from shared.http_dial import post as dial_post
    from shared.machine import (
        GatewayApiBaseMissing,
        gateway_api_base,
        gateway_auth_headers,
    )

    try:
        url = f"{gateway_api_base()}/api/cluster/update"
    except GatewayApiBaseMissing as exc:
        print(f"✗ cannot resolve gateway URL: {exc}", file=sys.stderr)
        return 1
    params = {"target": target}
    if target_sha is not None:
        params["target_sha"] = target_sha
    try:
        resp = dial_post(
            url,
            params=params,
            timeout=CLUSTER_DISPATCH_TIMEOUT_S,
            headers=gateway_auth_headers(),
        )
    except httpx.TimeoutException as exc:
        print(
            f"✗ gateway at {url} did not respond within {CLUSTER_DISPATCH_TIMEOUT_S:g}s: {exc}",
            file=sys.stderr,
        )
        return 1
    except httpx.TransportError as exc:
        print(f"✗ gateway unreachable at {url}: {exc}", file=sys.stderr)
        return 1
    if resp.status_code in (200, 202):
        body = resp.json()
        print(
            f"  ✓ dispatched: target={target} session={body.get('session')} log={body.get('log')}"
        )
        if target_sha is not None:
            print(f"    pinned: {target_sha}")
        print("  poll `ava cluster status` for the target to converge.")
        return 0
    if resp.status_code == 503:
        print(
            f"✗ {resp.json().get('detail', f'machine {target!r} ops server unreachable')}",
            file=sys.stderr,
        )
        return 1
    if resp.status_code == 502:
        print(
            f"✗ {resp.json().get('detail', f'machine {target!r} cluster_update failed')} — "
            "an updater may already be in flight there; retry after it settles",
            file=sys.stderr,
        )
        return 1
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        print(f"✗ gateway returned HTTP {resp.status_code} for {url}: {exc}", file=sys.stderr)
        return 1
    body = resp.json()
    print(f"  ✓ accepted: target={target} session={body.get('session')} log={body.get('log')}")
    return 0
