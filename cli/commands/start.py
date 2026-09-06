"""`ava start` — bring the cluster up (multi-machine aware).

`cmd_start` is the CLI entry; `_cmd_start_body` is the shared core, also driven by
`cmd_restart`. `ava start` needs no tty: the session PATH that once justified a tty
gate is now forwarded authoritatively per session (see `shared.session_env`), so start
runs identically from a terminal, cron, systemd, or a headless ssh.

## The exit code carries readiness

Three outcomes, not two, because "the start sequence ran" and "this host is serving"
are different facts a caller needs to tell apart:

- **0** — every step succeeded and every service launched passes its liveness probe.
- **`SERVICES_NOT_READY_EXIT_CODE`** — every step succeeded, but a launched service
  never passed its probe within `SERVICE_READY_TIMEOUT_S`, or a session could not be
  created at all (the backend refused it twice — see `_session_lifecycle.LaunchOutcome`).
  The status snapshot has already printed with its crosses and both sets of sessions
  are named after it, so the operator path loses nothing; the program path stops
  reading a half-up host as a healthy one.
- **1** — a step failed. The host may have no services at all.

Whatever would not launch is also written to `$AVA_HOME/last_launch_failures`
(`shared.launch_failures`), because the rollout runs this start in a child process
and needs the names, not just the code.

`readiness_gate=False` (`ava start --no-readiness-gate`) keeps the wait but drops the
verdict. Two callers pass it, each because a non-zero exit would do harm there:

1. **The boot job**, on all three platforms (`cli.boot_retry`, `shared.os_autostart`).
   It retries with **no attempt cap** by design (`shared/boot_policy.py`), so a
   non-zero exit is an unbounded retry — and one permanently-unready-but-not-gated
   service (a headed Chrome that will not launch on an otherwise-serving box) would
   mean a host that never finishes booting. What that loop exists to retry is a start
   that failed *before* launching services (the VPN-down `ENETUNREACH` incident that
   produced the policy), which still exits 1; reviving a launched service that died
   is the watchdog keepalive's job, and the watchdog is one of the launched services.
2. **The rollout's local gateway leg** (`cli.commands.update`), because one step later
   `_gateway_ready` asks the same question better — off-box and authenticated, against
   the address the runners dial. Nesting two waits on overlapping questions is the
   "two clocks" mistake `shared/deploy_timing.py` exists to prevent, and it would let
   a slow `milvus` send `_recover_rc` rolling the cluster back.

A flag only reaches here if the caller knows to pass it, and the second caller is the
one caller that structurally cannot on the rollout that matters — so a gateway-capable
host also waives the verdict whenever it observes a live update lease. `_readiness_waiver`
holds why.
"""

from __future__ import annotations

import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path

from cli.commands._probe import _probe_judges_a_fresh_launch
from cli.commands._repo import ServiceSpec, _repo_root, session_name
from cli.commands._session_lifecycle import _launch_roster, _launch_sessions
from cli.commands._setup import _print_missing_setup_error
from cli.commands._start_bookmarks import record_running_sha as _record_running_sha
from cli.commands._update_uv_sync import run_uv_sync
from cli.commands.migrations import cmd_migrations_apply
from cli.commands.status import _update_in_flight, cmd_status
from shared import start_serving
from shared.deploy_timing import SERVICE_READY_TIMEOUT_S
from shared.exit_codes import SERVICES_NOT_READY_EXIT_CODE
from shared.machine import MachineRoles
from shared.paths import prod_service_checkout_error
from shared.rollout_telemetry import updater_stage


def _consume_rollout_parent_handoff() -> bool:
    """Consume the fresh-child marker before any service environment is built."""
    from shared.rollout_handoff import consume_parent_credential_handoff

    return consume_parent_credential_handoff()


def _ensure_gateway_data_plane() -> int:
    """Bring up this cluster's data plane — local instance or remote probe.

    The implementation lives in `cli/commands/_data_plane.py` (this module's
    line budget); the wrapper keeps the name tests and callers patch.
    """
    from cli.commands._data_plane import ensure_gateway_data_plane

    return ensure_gateway_data_plane()


def _rollout_child_window(
    *, parent_handoff: bool, persist_services: bool, gateway_capable: bool
) -> bool:
    """Identify a fresh rollout child, or refuse a concurrent operator start.

    A v1 marker is proof from the surviving parent. An executing DB lease is the
    compatibility signal for a child launched by older code: internal starts
    converge but must defer credential mutation; operator starts are refused.
    Settle holds carry a note and have no active orchestrator.
    """
    # Only the gateway local leg survives a checkout in a parent and owns the
    # credential/restarter boundary.  A pure agent-runner's Phase-B updater
    # also starts internally under the cluster-wide executing lease, but that
    # child must finish by restoring posture=idle and its local restarter so the
    # gateway's Phase-B poll can observe convergence.
    if parent_handoff and gateway_capable:
        return True

    from shared.cluster_lock import read_update_lease

    lease = read_update_lease()
    if lease is None or lease.note is not None:
        return False
    if persist_services:
        raise RuntimeError(lease.refusal("ava start"))
    return gateway_capable


def _verify_source_integrity(repo: Path) -> int:
    """Guard: detect manual git operations that bypass `ava cluster update`.

    Compares HEAD against the last-fully-installed commit (recorded after
    uv sync + migrations). On mismatch — someone ran `git pull` / `git reset`
    / `git checkout` directly in the source tree — the start path auto-heals
    by running `uv sync` before bringing services up, so the venv and schema
    match the code.

    Returns 0 when the guard passes (or on a non-fatal skip), non-zero when
    auto-heal failed and the caller should abort the start.

    Best-effort: a non-git checkout or a transient git failure prints a warning
    and carries on — the guard must not block a start on a minor glitch.
    """
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if head.returncode != 0:
            print(
                f"  · source-integrity guard skipped: git rev-parse failed (rc={head.returncode})",
                file=sys.stderr,
            )
            return 0
        head_sha = head.stdout.strip()
    except Exception as exc:
        print(f"  · source-integrity guard skipped: {exc}", file=sys.stderr)
        return 0

    try:
        from shared.source_integrity import get as get_installed_sha
        from shared.source_integrity import set_installed

        installed = get_installed_sha()
    except Exception as exc:
        print(f"  · source-integrity guard skipped: {exc}", file=sys.stderr)
        return 0

    if installed is None:
        # First run — seed the bookmark from current HEAD so the guard
        # is armed for the next start.
        try:
            set_installed(head_sha)
            print(f"  · source-integrity guard armed: installed_sha = {head_sha[:7]}")
        except Exception as exc:
            print(f"  · source-integrity guard seed failed: {exc}", file=sys.stderr)
        return 0

    if head_sha == installed:
        # All good — the source tree hasn't been tampered with.
        return 0

    # Mismatch: someone changed the source tree without going through
    # `ava cluster update`. Auto-heal by running uv sync.
    print(
        f"\n⚠  SOURCE INTEGRITY VIOLATION\n"
        f"   HEAD        : {head_sha[:7]}\n"
        f"   installed   : {installed[:7]}\n"
        f"   The source tree changed outside of `ava cluster update` — the venv may be\n"
        f"   stale. Auto-healing: running `uv sync` now.\n",
        file=sys.stderr,
    )
    sync_result = run_uv_sync(repo)
    if sync_result.returncode != 0:
        print(
            "  ✗ uv sync failed — refusing to start with a mismatched venv.\n"
            "    Run `ava cluster update` to recover, or `ava start` again to retry.",
            file=sys.stderr,
        )
        return 1
    print("  ✓ uv sync complete", file=sys.stderr)
    try:
        set_installed(head_sha)
    except Exception as exc:
        print(f"  · installed_sha update failed (non-fatal): {exc}", file=sys.stderr)
    return 0


def _seed_known_good_if_null(roles: frozenset[str]) -> None:
    """Seed the cluster's `last_known_good_sha` to the current HEAD when it has
    never been set — the automatic-rollback floor for a cluster that has not yet
    completed a rollout (otherwise the rollback aborts with "no rollback target").

    Gateway-only: the cluster pin is a single central row, and the gateway's HEAD
    is authoritative for what the cluster runs (an agent-runner's own checkout may
    be at a different sha). Idempotent — seeds exactly once, then no-ops forever.
    Best-effort: a bookkeeping write must not fail an otherwise-successful start."""
    if "gateway" not in roles:
        return
    try:
        from cli.commands._update_git import git_head_sha
        from shared.cluster_pin import seed_last_known_good_sha_if_null

        head = git_head_sha()
        if seed_last_known_good_sha_if_null(head, set_by="seed-on-first-start"):
            print(f"  · seeded last_known_good_sha = {head[:7]} (first successful start)")
    except Exception as exc:
        print(f"  · last_known_good seed skipped ({exc})")


def _readiness_waiver(roles: MachineRoles, *, readiness_gate: bool) -> str | None:
    """Why an unready service must not become this start's exit code — or None to gate.

    Two waivers, and the second is not a second spelling of the first.
    `--no-readiness-gate` is a caller *declaring* it owns the readiness question; a
    live update lease is this host *observing* that a rollout owns it. Only the
    observation survives a version skew — and the rollout's local gateway leg, which
    is one of the two callers meant to declare it, is exactly where that skew is
    guaranteed.

    The orchestration is running from the interpreter it started in, which imported
    `cli.commands.update` *before* the checkout moved the tree. So on the rollout that
    first ships a change to the flags that leg passes, the parent is old code and
    cannot pass the new flag, while the child it spawns from `.venv/bin/ava` is new
    code and gates by default. The parent reads the child's non-zero as a failed start
    and hands it to `_recover_rc`, which rolls the whole cluster back to
    last-known-good. Prod's 2026-07-30 21:09 rollout is that run: `ava start` skipped
    an `ava-gateway` session it believed was already running, reported it unready, and
    the 7e571b4 orchestrator reverted a target whose gateway the watchdog had revived
    and serving within 60 s. A flag cannot fix the rollout that introduces it; reading
    the lease can, because the reading lives entirely in the new child.

    Scoped to gateway-capable hosts because that is the only leg whose exit code a
    caller turns into a cluster-wide revert. A pure agent-runner keeps the code's
    meaning: its updater ladder answers `SERVICES_NOT_READY_EXIT_CODE` with an
    idempotent `ava start`, which repairs that host and touches no other.
    """
    if not readiness_gate:
        return "--no-readiness-gate"
    if "gateway" in roles and _update_in_flight():
        return "cluster update in progress — the rollout's own gateway gate owns readiness"
    return None


def _refuse_occupied_health_ports(roster: tuple[ServiceSpec, ...]) -> int:
    """0 when every health port in `roster` is this unit's to bind, else 1 + why.

    The one thing no port scheme can arrange in advance. A block allocated at
    install keeps two clusters apart, and `--health-port-base` lets an operator
    separate two units by hand — but neither survives a *third* unit appearing
    later, and a WSL2 distro can bind whatever it likes on a loopback Windows
    also reaches. Detection is what does not depend on everyone having agreed
    beforehand, so `ava start` asks the port who is there before it launches
    anything onto it (issue #977).

    Exits 1 rather than the readiness code: nothing has been launched, so this is
    a step that failed, not a host that came up incomplete. That also keeps it
    outside `_readiness_waiver` — a rollout must not wave this through, because
    the leg would come up bound to nothing and report success.

    One occupied port refuses the WHOLE start, gateway and frontend included —
    there is no partial bring-up, because a `.env` is edited once and the whole
    block moves together, so degrading to "start the other six" would leave the
    mixed state the incident was made of. The escape hatch is
    `--disable-service <name>`, which drops the daemon from the roster this gate
    reads and is therefore the way to bring the rest of the unit up while the
    collision is being sorted out; the message says so.
    """
    # Via `_ns` so the autouse test guard's monkeypatch of
    # `cli.commands._occupied_health_ports` takes effect at this callsite.
    import cli.commands as _ns
    from shared.paths import ava_home

    occupied = _ns._occupied_health_ports(roster)
    if not occupied:
        return 0
    print("\n✗ another unit already answers on this unit's daemon health ports:", file=sys.stderr)
    for port in occupied:
        print(f"    {session_name(port.spec.session)}: {port.detail}", file=sys.stderr)
    print(
        "\n  Not starting — NOTHING was launched, including the gateway and the frontend. "
        "Launching onto a held port dies on 'address already in use'; launching onto a "
        "RELAYED one is worse, because the watchdog's probe is answered by the other unit "
        "and the failure reads as green.\n"
        "  A health port belongs to a unit, not to a cluster — two units on one machine "
        "(a second install, or a WSL2 distro whose loopback Windows can reach) need one of "
        "them moved. Give this unit its own block:\n"
        f"      ava enroll --gateway <url> --machine-name <name> --machine-host <host> "
        f"--health-port-base <N>\n"
        f"  or set the AVA_*_HEALTH_PORT keys in {ava_home() / '.env'} directly, then retry "
        "`ava start`.\n"
        "  To bring the rest of this unit up meanwhile, drop the listed daemons from this "
        "start: "
        + " ".join(f"--disable-service {port.spec.session}" for port in occupied)
        + "\n  (that daemon then does not run at all — it is a stopgap, not the fix).",
        file=sys.stderr,
    )
    return 1


def _cmd_start_body(  # noqa: PLR0915 — cohesive linear start sequence (converge -> infra -> services -> status); splitting hurts readability
    machine_name: str | None = None,
    serve_gateway: bool | None = None,  # noqa: FBT001 — tri-state capability flag, always passed by name
    serve_agent_runner: bool | None = None,  # noqa: FBT001 — tri-state capability flag, always passed by name
    serve_observability_station: bool | None = None,  # noqa: FBT001 — tri-state capability flag, always passed by name
    machine_description: str | None = None,
    memory_remote: str | None = None,
    gateway_url: str | None = None,
    disabled_services: tuple[str, ...] = (),
    *,
    persist_services: bool = True,
    readiness_gate: bool = True,
    updater_telemetry: bool = False,
    release_receipt: Path | None = None,
) -> int:
    """Core start logic, shared by cmd_start and cmd_restart.

    `persist_services` distinguishes an operator start (True — `--disable-service`
    is durable intent, rewrites the watchdog marker) from an internal restart
    (False — update / recovery / `ava restart`: read the durable marker, union
    this restart's transient disabled set, leave the marker unchanged). See
    `shared.disabled_services`.

    `readiness_gate` decides whether an unready service is an exit code or only a
    printed cross; the module docstring holds which callers turn it off and why.
    """
    # Dynamic namespace lookup preserves existing setup/converge/probe test seams.
    import cli.commands as _ns
    from shared import maintenance

    maintenance.require_start_allowed()

    # Receipt admission precedes every setup/converge/source-repair write.
    from cli.commands._release_candidate import admit_start_candidate

    release = admit_start_candidate(release_receipt)

    repo = _repo_root()
    print(f"[ava start] cwd = {repo}")
    parent_handoff = _consume_rollout_parent_handoff()

    # The prod home must not launch from a disposable development checkout.
    err = prod_service_checkout_error(repo)
    if err:
        print(f"\u2717 {err}", file=sys.stderr)
        return 1

    # Legacy checkout repair remains below admission, never a release fallback.
    from cli.commands._converge_source_tree import reset_prod_source_tree

    reset_prod_source_tree(repo)

    # 0a) source-integrity guard — detect manual git pull / reset / checkout
    #      in the source tree that bypass `ava cluster update` and auto-heal by running
    #      `uv sync` before the stale venv can crash the cluster.
    if _verify_source_integrity(repo) != 0:
        return 1

    # 0b) collect & validate setup fields (capability-aware filter)
    args: dict[str, str | bool | None] = {
        "machine_name": machine_name,
        "machine_serve_gateway": serve_gateway,
        "machine_serve_agent_runner": serve_agent_runner,
        "machine_serve_observability_station": serve_observability_station,
        "machine_description": machine_description,
        "memory_remote": memory_remote,
        "gateway_url": gateway_url,
    }
    try:
        resolved, missing = _ns._collect_setup_values(args)
    except ValueError as e:
        # validator failure (e.g. MachineRoleInvalid) — do not persist invalid
        # value, print error + exit.
        print(f"\n✗ {e}", file=sys.stderr)
        return 1
    if missing:
        _print_missing_setup_error(missing, resolved.get("machine_role"))
        return 1

    # reset identity holder so downstream shared.machine.machine_name() /
    # machine_role() see the just-written machine_serve_* files.
    from shared.machine import machine_role, reset_identity

    reset_identity()

    roles = machine_role()
    print(f"\n→ roles = {','.join(sorted(roles))}, machine = {resolved['machine_name']}")

    from shared.platform import raise_fd_limit

    raise_fd_limit(65536)  # every service spawned here inherits the raised ceiling

    # 1) converge host state (symlink / PATH / $AVA_HOME dirs / plugin config
    # images). Memory initialization is explicit (`ava memory init`) and never
    # runs during start or rollback. `ava cluster update` inherits this via its
    # trailing cmd_start, so one gateway update converges the whole fleet.
    try:
        _ns.converge_host(repo, roles)
    except Exception as e:
        print(f"  ✗ converge failed: {e}", file=sys.stderr)
        return 1

    # 2) gateway brings up this cluster's own pg/redis instance (under its
    #    $AVA_HOME, on its per-cluster ports); a runner-only host skips (uses the
    #    central node's DB/Redis). macOS: brew binaries via pg_ctl + redis-server;
    #    Linux: pg_ctl + redis-server. No docker on any POSIX platform.
    if "gateway" in roles:
        rc = _ensure_gateway_data_plane()
        if rc != 0:
            return rc
    else:
        print("\n→ local services: skipped (agent-runner uses central node's DB/Redis/Milvus)")

    # A killed credential split can leave native services accepting only the
    # journaled target. Bring-up above recognizes that target; finish its env +
    # in-process adoption before the first migration or lease dial.
    if "gateway" in roles:
        from cli.commands._data_plane_admin_secrets import (
            resume_pending_data_plane_admin_secrets,
        )

        try:
            resume_pending_data_plane_admin_secrets()
        except Exception as e:
            print(f"  ✗ data-plane credential transition replay failed: {e}", file=sys.stderr)
            return 1

    # Detect the process boundary before migrations, grant refresh, or service
    # intent can mutate rollout state. An unreadable lease fails closed.
    try:
        rollout_child = _rollout_child_window(
            parent_handoff=parent_handoff,
            persist_services=persist_services,
            gateway_capable="gateway" in roles,
        )
    except Exception as e:
        print(f"  ✗ cannot start while checking the rollout boundary: {e}", file=sys.stderr)
        return 1

    # 2.5) migrations apply (idempotent) — pg is ready; schema must be in place
    # before the service sessions start (gateway connects to agents_meta / register_self
    # writes machines / etc.). On prod restart schema is already applied ->
    # applied 0; first-time bench run -> applies the full set.
    print("\n→ apply pending migrations")
    try:
        with updater_stage("migration") if updater_telemetry else nullcontext():
            applied = (
                cmd_migrations_apply() if release is None else cmd_migrations_apply(release=release)
            )
    except Exception as e:
        print(f"  ✗ migrations apply failed: {e}", file=sys.stderr)
        return 1

    # 2.6) schema-current assertion — applied vs required must match exactly.
    # On an agent-runner that runs `ava start` while the gateway is one
    # migration ahead, `apply_pending_migrations` writes nothing (the file
    # isn't in this checkout's migrations/) but the central DB's
    # schema_migrations holds the newer version. Without this check the ops
    # server would happily start against a schema it doesn't understand and
    # produce confusing wire-level errors.
    print("\n→ verify schema version")
    rc = _ns._assert_schema_current_or_die()
    if rc != 0:
        return rc

    # Pre-create the pgvector extension with the bootstrap-superuser socket
    # connection while the data plane is up: pgvector's control file is not
    # `trusted`, so the NOSUPERUSER runtime roles cannot install it themselves,
    # and an existing cluster picks it up here (install birth covers fresh
    # ones). A silent no-op when this Postgres lacks the extension binaries —
    # the backend preflight probe owns that failure path. Local instances
    # only: a remote-managed plane has no local admin socket, and its
    # extension provisioning belongs to its owner.
    from shared.config import settings

    if "gateway" in roles and not settings.data_plane.is_remote:
        from cli.commands._cluster_instance import pg_admin_url
        from shared.cluster import db_identity, get_record
        from shared.cluster.provision import ensure_pgvector_extension
        from shared.paths import ava_home

        rec = get_record(ava_home())
        if rec is not None:
            ensure_pgvector_extension(
                db_identity(), base_admin_url=pg_admin_url(rec.ports["postgres"])
            )

    # 2.65) a migration that CREATED a table left the ava_runner read grant
    # behind it: `GRANT SELECT ON ALL TABLES` is a point-in-time loop over what
    # existed at install birth, so a pure agent-runner — which dials as
    # ava_runner — gets `permission denied` on anything added since. Re-affirm
    # the grants at the one moment the schema is known to have grown, rather
    # than on every start. Gateway-only: the admin credential lives in the
    # gateway's .env, and a runner has no business touching roles.
    if applied and "gateway" in roles:
        from cli.commands.ensure_db_role import refresh_runner_grants_after_migration

        refresh_runner_grants_after_migration()

    # 2.66) Existing authenticated clusters still carry the historical bearer
    # as every data-plane password. Migrate that state only after migrations,
    # before any service session can inherit the owner URLs. Fresh installs
    # already minted the independent values at birth, so this is a no-op there.
    if "gateway" in roles:
        from cli.commands._data_plane_admin_secrets import ensure_data_plane_admin_secrets

        try:
            ensure_data_plane_admin_secrets(
                allow_legacy_upgrade=not rollout_child or parent_handoff
            )
        except Exception as e:
            print(f"  ✗ data-plane credential split failed: {e}", file=sys.stderr)
            return 1

    # 2.7) land the cluster's installed extensions on this machine. AFTER the
    # schema check on purpose: converge (step 1) runs before this cluster's
    # Postgres is even up (step 2) and before migrations (step 2.5), so a
    # converge step could not read the registry on a single box at all. Here the
    # data plane is up and known-current, which is the precondition
    # materialization actually has. Reports and continues on failure — a machine
    # that is behind catches up on the next start.
    from cli.commands._converge_extensions import (
        adopt_local_extensions,
        materialize_cluster_extensions,
    )

    # Adopt first: a name this machine installed before the registry existed is
    # invisible to the materializer until it has a row, and sweeping first means
    # one pass leaves machine and cluster agreeing rather than two.
    adopt_local_extensions()
    materialize_cluster_extensions()

    # 3) UPSERT this host into the machines table. The table is informational
    # for ops (`ava cluster status`) + drives agent-runner self-update orchestration;
    # gateway→agent-runner RPC dials the host's ops URL stored in this row, so
    # a missing/NULL row means the cluster cannot reach it. Failing here means
    # this host is invisible to the cluster — fatal on both roles, because an
    # agent-runner will also fail every subsequent `ava cluster status` and
    # `ava cluster update` orchestration.
    print("\n→ register machine in central DB")
    rc = _ns._register_machine_or_die(resolved, roles)
    if rc != 0:
        return rc

    # 3.5) pure-runner only: probe the gateway over the private network before
    # bringing the host up. A co-located gateway,agent-runner box IS the gateway,
    # so it skips this self-probe. The host reaches the gateway this way for
    # self-heal updates + cluster status; a broken private-network path would only
    # surface later during a self-heal. Catching at start-time means the failure
    # is on this stdout and the host fails non-zero.
    if "agent-runner" in roles and "gateway" not in roles:
        print("\n→ probe gateway")
        rc = _ns._probe_gateway_or_die(resolved["gateway_url"])
        if rc != 0:
            return rc

    # 4) service sessions — resolve the launch-time skip set. An operator start
    # records its --disable-service set as the watchdog's durable marker; an
    # internal restart reads that marker and unions its transient skips.
    from shared.disabled_services import resolve_launch_skip

    if rollout_child:
        disabled_services = (*disabled_services, "restarter")
    launch_skip = resolve_launch_skip(set(disabled_services), persist=persist_services)

    # 4a) probe before binding: refuse to launch a daemon onto a health port
    # another unit already answers on. Placed here because this is the last point
    # at which nothing has been spawned, and the roster is only knowable once the
    # skip set is resolved.
    rc = _refuse_occupied_health_ports(_launch_roster(roles, launch_skip))
    if rc != 0:
        return rc

    # Failed start attempts must leave recovery actions gated.
    serving_generation = start_serving.begin_start()
    _record_running_sha(repo)
    launch = _launch_sessions(roles, launch_skip, repo)
    started = launch.started
    # 4a) hand the launch failures to whoever runs this start from another process.
    # Written unconditionally so a clean start clears a previous run's list; the
    # rollout's local leg is the consumer (`update._run_gateway_local_update`),
    # because its `ava start` is a child and an exit code cannot carry names.
    from shared import launch_failures

    launch_failures.record(list(launch.failed))

    # 4.5) warm the agent boot stack (agent-runner hosts only) — pre-pay the
    # first agent's cold-start (heavy import page cache + MCP daemon spawn +
    # browser/npx) so the first spawn after a fresh start is fast. Detached +
    # best-effort; never blocks or fails start. Called via `_ns` so the autouse
    # test guard (tests/conftest.py:_guard_agent_warmup) can no-op it.
    if not maintenance.held():
        _ns._launch_agent_warmup(roles, repo)

    # 5) idempotent clear of the paused state — `ava start` means "I want to
    # serve"; when the gateway crashes between phase A and B leaving
    # a paused posture row so agent-runners are stuck at 503, a manual
    # `ava start` can also recover (no longer requires ssh + rm coordination).
    # R1 (Task #1021): this transition changes only the host posture row. The
    # cluster orchestrator's separate Gate marker spans restart and Phase B;
    # local start must never clear or reclassify that maintenance owner.
    from shared.host_deploy_state import set_posture

    set_posture("paused" if maintenance.held() else "converging" if rollout_child else "idle")

    # 5.1) generation-scoped successful-finalize: a Phase-B `ava start`
    # resumes without a cluster/resume op, so the journaled generation must be
    # recorded `resumed` or it stays `paused` forever (2026-08-26 residue);
    # a rollout child (converging) skips — its finally owns that boundary.
    if not rollout_child:
        from ops.cluster_pause import finalize_pause_owner_journal

        finalize_pause_owner_journal()

    # 5.5) wait for the just-launched services to pass their probes before the
    # status snapshot. The spawn returns the instant the session starts, but a uvicorn
    # daemon needs a beat to bind its port -- probe it immediately and every row
    # reads as a cross, making a healthy start look like a total failure. Poll the
    # same probes `ava status` uses and return the instant they pass.
    #
    # `started` is exactly the right roster to gate on and it is not a list kept by
    # hand: it is `ops.spec.services_for_capabilities_annotated(roles)` minus the
    # config/capability-gated entries (browser with no display, browser-mcp with no
    # AF_UNIX, a disabled heartbeat) minus the operator's --disable-service set. A
    # service that is *skipped* therefore never reaches the gate, so it can never
    # fail a start; adding a service or a gate to `ops/spec.py` moves this set with
    # no edit here. The one deliberate subtraction is the service whose probe cannot
    # judge a launch this fresh (`_probe_judges_a_fresh_launch` -- the frontend, whose
    # ~30-60s build would put a minute on every gateway start); it shows its real
    # state in the snapshot below instead. That predicate is shared with the husk
    # check in `_launch_sessions`, which must exempt exactly the same services
    # for exactly the same reason.
    print("\n→ waiting for services to come up")
    with updater_stage("readiness") if updater_telemetry else nullcontext():
        wait = _ns._wait_for_services_ready(
            tuple(s for s in started if _probe_judges_a_fresh_launch(s)),
            timeout_s=SERVICE_READY_TIMEOUT_S,
        )

    # 5.6) seed last_known_good_sha on the first successful gateway start. The pin
    # is only advanced by a completed rollout, so a fresh cluster's automatic
    # rollback has no anchor and aborts; floor it at the commit we just came up on.
    _seed_known_good_if_null(roles)

    # 6) status
    # (Health checks are handled by services/watchdog/daemon.py inside the
    # ava-watchdog session, same 60s interval as the original OS cron.
    # See PR watchdog-daemon for removing the cron dependency.)
    print("\n→ status")
    cmd_status()

    # 7) gateway reachability hint. The gateway's own .env holds the loopback URL
    # (a box reaches its own gateway over loopback); this prints the OTHER address
    # — what a remote agent-runner dials — so whoever just brought the gateway up
    # can enroll runners against it without hunting for the host/port.
    if "gateway" in roles:
        from shared.config import settings as _settings
        from shared.machine import reachable_host
        from shared.netutil import is_loopback_host

        port = _settings.gateway.gateway_port
        host = reachable_host()
        if is_loopback_host(host):
            print(
                f"\n→ gateway reachable at http://{host}:{port} (loopback only — set "
                "AVA_MACHINE_HOST to this box's private-network address to enroll remote agent-runners)"
            )
        else:
            reachable = f"http://{host}:{port}"
            print(f"\n→ gateway reachable at {reachable}")
            print(
                f"  enroll an agent-runner: ava enroll --gateway {reachable} "
                "--machine-name <name> --machine-host <runner-host> "
                "(with AVA_CLUSTER_SECRET set from a non-echoing prompt)"
            )

    # 8) Readiness verdict last: its exit code and printed snapshot describe the same run.
    #
    # Launch failures share the verdict: rollout reads `shared.launch_failures`, while the
    # boot loop retries without an unbounded wait on one service (`shared/boot_policy.py`).
    if launch.failed:
        print(
            f"\n✗ {len(launch.failed)} session(s) could not be launched "
            f"(retried once): {', '.join(launch.failed)}",
            file=sys.stderr,
        )
    if wait.unready:
        _ns._print_unready_services(wait, SERVICE_READY_TIMEOUT_S)
    # The tier's second rail: a non-critical service that missed its short window
    # does not fail the start, but it is reported and alerted — the downgrade must
    # never go silent (see `_probe._notify_non_critical_unready_services`). The
    # IM push is gated on the readiness flag: the boot job's uncapped 60 s
    # retries run with `--no-readiness-gate` and must not spam the user's IM,
    # while the alerts store and this output stay visible.
    if wait.non_critical_unready:
        _ns._print_non_critical_unready_services(wait.non_critical_unready)
        _ns._notify_non_critical_unready_services(
            wait.non_critical_unready, im_enabled=readiness_gate
        )
    # The resolved edge: a non-critical service that is up again closes its open
    # alert instance, so the Inspector never keeps showing a resolved failure
    # (QA #1196 P1-1).
    recovered = _ns._recovered_non_critical_specs(started, wait.non_critical_unready)
    if recovered:
        _ns._resolve_recovered_non_critical_alerts(recovered, im_enabled=readiness_gate)
    if wait.unready or launch.failed:
        waiver = _readiness_waiver(roles, readiness_gate=readiness_gate)
        if waiver is None:
            return SERVICES_NOT_READY_EXIT_CODE
        print(f"  · {waiver}: exiting 0 anyway", file=sys.stderr)
        return 0

    if not start_serving.mark_serving(serving_generation):
        print("  ✗ this start lost its serving generation", file=sys.stderr)
        return 1
    return 0


def cmd_start(
    machine_name: str | None = None,
    serve_gateway: bool | None = None,  # noqa: FBT001 — tri-state capability flag, always passed by name
    serve_agent_runner: bool | None = None,  # noqa: FBT001 — tri-state capability flag, always passed by name
    serve_observability_station: bool | None = None,  # noqa: FBT001 — tri-state capability flag, always passed by name
    machine_description: str | None = None,
    memory_remote: str | None = None,
    gateway_url: str | None = None,
    disabled_services: tuple[str, ...] = (),
    *,
    persist_services: bool = True,
    readiness_gate: bool = True,
    updater_telemetry: bool = False,
    release_receipt: Path | None = None,
) -> int:
    """Start the cluster.

    Multi-machine setup fields (machine_name / serve_gateway / serve_agent_runner
    / serve_observability_station / memory_remote / gateway_url) precedence: env >
    `$AVA_HOME/<field>` file > this function's arg. serve_gateway /
    serve_agent_runner / serve_observability_station are independent booleans
    declaring this host's capability set; the required string fields are filtered
    by that set (both gateway and agent-runner require gateway_url).
    If any required field is missing -> print actionable error and exit 1
    (no TTY prompt — agent-first design). On first arg pass, the CLI writes values to
    `$AVA_HOME/<field>` files; subsequent `ava start` calls do not need the args.

    Branch on the resolved capability set:
    - gateway: native pg/redis up + 9 service sessions (including frontend / daemon / milvus)
    - agent-runner: skip local infra + only start ops/restarter/watchdog (3 service sessions)
      (agent-runner's `~/.ava/.env` AVA_DB_URL / AVA_REDIS_URL / AVA_MILVUS_URI
      must point at the gateway's reachable endpoint; the gateway
      reaches this host by dialing its registered ops URL)
    """
    # Headless runner updates inherit PATH; no tty capture or gate is required.
    return _cmd_start_body(
        machine_name=machine_name,
        serve_gateway=serve_gateway,
        serve_agent_runner=serve_agent_runner,
        serve_observability_station=serve_observability_station,
        machine_description=machine_description,
        memory_remote=memory_remote,
        gateway_url=gateway_url,
        disabled_services=disabled_services,
        persist_services=persist_services,
        readiness_gate=readiness_gate,
        updater_telemetry=updater_telemetry,
        release_receipt=release_receipt,
    )
