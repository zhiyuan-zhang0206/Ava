"""
CLI entry + detached-session spawn for `ava cluster update`.

Split out of `cli/commands/update.py` (which keeps the gateway orchestration
core) to keep that module within the file-size budget. This is the dispatch
surface `cmd_update` runs on every `ava` CLI invocation:
- `cmd_update` — auto-dispatch by this host's machine_role: gateway -> detached
  `ava-rollout` session (default) or in-process orchestration (`--local` /
  `--restart-only`); agent-runner -> the in-process local self-update.
- `_spawn_gateway_rollout` — spawn the detached `ava-rollout` session via
  `ops.cluster.spawn_rollout`, translating its two ordinary refusals
  (deploy-window conflict, nothing-to-update) into clean CLI exits.

Re-imported by `cli/commands/update.py` (and re-exported through `cli.commands`)
so `cli.commands(.update).cmd_update` / `._spawn_gateway_rollout` keep resolving.

"""

from __future__ import annotations

import sys

from cli.commands._update_preflight import _refuse_target_sha_on_gateway
from shared.proc import hosting_supervised_session


def cmd_update(
    *,
    restart_only: bool = False,
    local: bool = False,
    target_sha: str | None = None,
    origin: str | None = None,
    rollout_log: str | None = None,
    force: bool = False,
    mode: str = "smooth",
) -> int:
    """`ava cluster update` — auto-dispatch by this host's machine_role.

    On the **gateway** the default spawns the full three-phase
    orchestration in a detached `ava-rollout` session (a
    clean login shell, decoupled from this process's env) and returns
    immediately. This host runs the HTTP gateway, so it spawns
    the session directly (via `gateway.cluster.spawn_rollout`) rather than POSTing
    its own `/api/cluster/rollout`: the rollout must outlive the request, so it
    runs in a detached session. That endpoint stays for the frontend Update button; both paths converge on
    `spawn_rollout`. The orchestration itself is unchanged — it just runs in the
    detached session (via `ava cluster update --local`) instead of this foreground process.

    On an **agent-runner** it runs the local self-update (git pull / uv sync /
    stop / start, no schema changes) in-process — that path is already light, so
    it is not routed through the gateway.

    `local=True` (`ava cluster update --local`) forces the in-process orchestration /
    self-update on either host: the escape hatch the detached `ava-rollout`
    session uses (so it does not re-POST and recurse), and for debugging.
    Every in-process leg (this, `--restart-only`, and the agent-runner
    self-update) is refused with exit 2 when this process is hosted inside a
    supervised session — the stop leg would kill its own orchestration
    (`shared.proc.hosting_supervised_session`); the detached orchestration
    sessions themselves are exempt.

    `restart_only=True` (`ava cluster update --restart-only`) bounces services on the
    current code with no checkout / uv sync / migration; in-process, spawned by
    the `POST /api/cluster/restart` endpoint.

    `target_sha` (`ava cluster update --target-sha <sha>`) is the pinned rollout commit
    threaded by Phase B to each agent-runner's `ava cluster update --local`, and it feeds
    ONLY that agent-runner self-update. A gateway-capable host **refuses it and
    returns 2**: the orchestration resolves the pin itself, once,
    and fans that one commit out to every node — so accepting a second way to
    express the target would let an operator believe the rollout was pinned while
    `_resolve_rollout_target` was resolving `origin/main` live underneath them.
    Silently discarding it is worse than not having it: a missing flag errors, an
    ignored one misinforms.

    `origin` (`ava cluster update --origin <who>`) names the trigger — threaded by
    `spawn_rollout` into the detached session so the cluster pin records who
    moved it. Defaults to `cli:<machine>` (a human running `ava cluster update`).

    `rollout_log` (`ava cluster update --rollout-log <path>`) is threaded the same way and
    for the same reason: `spawn_rollout` creates the log file this session tees
    into, and only it knows the path. The orchestration stamps it onto the
    last-update record so the status surfaces can name the log to read. A
    foreground `ava cluster update --local` passes nothing and records nothing — it has no
    log of its own, and pointing at some earlier rollout's would be worse than
    pointing at nothing.

    `force=True` (`ava cluster update --force`) starts a rollout even though a deploy is
    already in flight somewhere in the cluster. The refusal it overrides is the
    2026-07-28 fix (two concurrent deploys each pin their own commit and quiesce
    into the other's window); the override exists because a deploy signal can
    outlive the deploy — a host powered off mid-update leaves evidence this code
    cannot distinguish from a live one.
    """
    # Dynamic lookup for the orchestration / self-update helpers so tests
    # can monkeypatch `cli.commands._run_gateway_orchestration` /
    # `cli.commands._run_agent_runner_self_update`.
    import cli.commands as _ns
    from shared.machine import machine_name, machine_role
    from shared.platform import raise_fd_limit

    raise_fd_limit(65536)  # the orchestration this dispatches inherits the ceiling
    roles = machine_role()
    # Refuse `--target-sha` on a gateway-capable host BEFORE any dispatch. Every
    # gateway route ignores it — the detached-rollout default (`_spawn_gateway_rollout`)
    # and `--local` (`_run_gateway_orchestration`, which resolves its own pin via
    # `_resolve_rollout_target`) both take no target_sha at all. Accepting it there
    # tells the operator the rollout is pinned to their sha while origin/main is
    # resolved live underneath them; the flag's only honest answer on a gateway is
    # to refuse and name the verb that does pin a commit.
    if target_sha is not None and "gateway" in roles:
        return _refuse_target_sha_on_gateway(target_sha)
    if origin is None:
        origin = f"cli:{machine_name()}"
    # A gateway-capable host (incl. a single-box gateway,agent-runner) runs the
    # orchestrated rollout; its local pull/sync/migrate/restart covers its own
    # agent-runner capability too. A pure agent-runner runs the lighter local
    # self-update. Gateway foreground default → spawn the detached rollout
    # locally; `--local` / `--restart-only` force in-process.
    if "gateway" in roles and not local and not restart_only:
        return _spawn_gateway_rollout(origin, force=force, mode=mode)

    # The in-process legs below stop this host's services (and quiesce/reap its
    # agents + shells). A run hosted inside one of those process trees is killed
    # by its own stop leg mid-flight — 2026-08-12: an agent's pty-hosted shell ran
    # `ava cluster update --local`, the stop leg force-killed ava-pty-supervisor's whole
    # tree (rollout included), and the cluster stranded paused with services down.
    # The detached orchestration sessions (`spawn_rollout`/`spawn_update`/
    # `spawn_restart`) are exempt inside `hosting_supervised_session` — they are
    # the auto-updater shape: the trigger returns immediately and a process that
    # outlives every stopped service does the stop/update/start.
    hosting = hosting_supervised_session()
    if hosting is not None:
        print(
            f"\n✗ in-process update refused: this process runs inside supervised session "
            f"{hosting!r}, which this host's own stop leg kills — tree included, this "
            "update with it, stranding the host mid-transition. Use the detached form: "
            "`ava cluster update` (no --local) on a gateway host spawns the detached "
            "ava-rollout session; otherwise run this from a shell no ava session hosts "
            "(e.g. a plain ssh/login shell).",
            file=sys.stderr,
        )
        return 2

    from cli.commands import update as _up_mod

    repo = _up_mod._repo_root()
    home = _up_mod.ava_home()
    record = _up_mod.get_record(home)
    registry = f", registry: {record.name}" if record else ", registry: (unregistered)"
    print(
        f"[ava cluster update] cwd = {repo}, cluster home = {home}{registry}"
        f"{' (restart-only)' if restart_only else ''}"
    )
    if "gateway" in roles:
        return _ns._run_gateway_orchestration(
            repo,
            restart_only=restart_only,
            origin=origin,
            rollout_log=rollout_log,
            mode=mode,
        )
    return _ns._run_agent_runner_self_update(
        repo, target_sha=target_sha, restart_only=restart_only, mode=mode
    )


def _spawn_gateway_rollout(origin: str, *, force: bool = False, mode: str = "smooth") -> int:
    """Spawn the detached `ava-rollout` session (gateway only).

    The caller gates on the gateway capability. This host runs the HTTP gateway,
    so it calls `gateway.cluster.spawn_rollout` directly instead of POSTing its
    own `/api/cluster/rollout`: the rollout must outlive the request, so it runs
    in a detached session. The `POST /api/cluster/rollout` endpoint
    stays for the frontend Update button; both
    paths converge on `spawn_rollout` — the same detached `ava cluster update --local`,
    fire-and-forget. Poll `ava cluster status` for the cluster to come back.

    Both of `spawn_rollout`'s refusals are caught here and reported as a non-zero
    exit carrying the reason. They are not exceptional conditions from the CLI's
    point of view — they are the two ordinary answers to `ava cluster update` — and neither
    was handled, so each reached the operator as a raw Python traceback.

    That matters most for the deploy-window refusal (`ops.deploy_window`), whose
    whole purpose is that a second operator SEES the conflict rather than
    reconstructing it from the wreckage. Its message names the host in the way, why
    two concurrent deploys defeat the rollout's own safety, and the `--force`
    override — and all of it was being delivered under a stack trace, which reads as
    "the tool broke", not "the tool refused". The refusal already knew what to say;
    this is the seam that lets it say it.
    """
    from ops.cluster import ClusterUpdateInProgress, NothingToUpdate, spawn_rollout

    try:
        body = spawn_rollout(origin, force=force, mode=mode)
    except ClusterUpdateInProgress as e:
        print(f"\n✗ {e}", file=sys.stderr)
        return 1
    except NothingToUpdate as e:
        # Not a failure: the cluster is already on the latest code. Exit 0 so a
        # scripted `ava cluster update` in a chain is not tripped by a no-op.
        print(f"\n· {e}", file=sys.stderr)
        return 0
    print(f"  ✓ dispatched: session={body['session']} log={body['log']}")
    print("  poll `ava cluster status` for the cluster to return.")
    return 0
