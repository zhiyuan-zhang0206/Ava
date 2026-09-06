"""Idempotent host convergence for the ava lifecycle.

Bring a machine to the host-level state the current code expects: the `ava`
symlink on PATH, ~/.local/bin on PATH, the $AVA_HOME dir skeleton, fresh plugin
config images. Run by `cmd_start` (so `ava cluster update` inherits it via its
trailing start), and standalone via `ava converge`.
"""

# Host setup that used to live only in install.sh never reached already-deployed
# hosts on upgrade. Folding it into the lifecycle makes one `ava cluster update` converge
# the fleet. Each step is idempotent + fail-fast; `roles` / `requires_unit_config`
# are declarative filters mirroring ServiceSpec's role-scoping.
from __future__ import annotations

import sys
from pathlib import Path

from cli.commands._converge_brew_pin import ensure_brew_pin
from cli.commands._converge_external_agent_skills import converge_external_agent_skill
from cli.commands._converge_firewall import ensure_firewall_allowlist
from cli.commands._converge_frontend_env import ensure_no_frontend_env_overrides
from cli.commands._converge_gate import ensure_gate
from cli.commands._converge_legacy_permission_watcher import remove_legacy_permission_watcher
from cli.commands._converge_os_jobs import (
    ensure_cluster_autostart,
    ensure_health_probe_cron,
    ensure_watchdog_probe,
    reap_stale_schtasks,
)
from cli.commands._converge_pitr import converge_pitr_foundation
from cli.commands._converge_redis_bridge import ensure_redis_bridge
from cli.commands._converge_source_tree import ensure_source_tree_integrity

# The step contract lives in _converge_spec so step implementations can span
# modules without an import cycle; re-exported here because every caller and
# test reaches for `cli.commands._converge.ConvergeCtx` / `ALL_ROLES`.
from cli.commands._converge_spec import ALL_ROLES, ConvergeCtx, ConvergeStep
from cli.commands._converge_steps import (
    _PATH_BEGIN as _PATH_BEGIN,
)
from cli.commands._converge_steps import (
    _PATH_END as _PATH_END,
)
from cli.commands._converge_steps import (
    _backfill_health_port_keys_step,
    _ensure_ava_home_dirs,
    _ensure_ava_symlink,
    _ensure_local_bin_on_path,
    _ensure_pg_binaries_step,
    _ensure_prod_editable_dir_protection,
    _ensure_prod_editable_exec_gate,
    _ensure_prod_editable_pth,
    _ensure_redis_url_identity_step,
    _migrate_host_config_to_env,
)
from cli.commands._converge_steps import (
    _shell_rc_path as _shell_rc_path,
)
from cli.commands._health_preflight import ensure_health_preflight as _ensure_health_preflight
from cli.commands._lgtm import ensure_lgtm_stack_step
from cli.commands._lgtm_native import ensure_lgtm_native_step
from cli.commands._otel_collector import ensure_otel_collector_step
from cli.commands._ownership_preflight import (
    ensure_ownership_preflight as _ensure_ownership_preflight,
)
from cli.commands._pgbouncer import _ensure_pgbouncer_step
from cli.commands._port_preflight import ensure_port_preflight as _ensure_port_preflight
from shared.accessibility import (
    clear_status as clear_accessibility_status,
)
from shared.accessibility import (
    write_status as write_accessibility_status,
)
from shared.browser_deps import browser_deps_notice, browser_deps_warning
from shared.cluster import is_default_home
from shared.config import settings
from shared.machine import MachineRoles
from shared.platform_backend import get_backend
from shared.platform_probes import browser_incapability
from shared.runtime_config import migrate_permissions_helper_env_keys
from shared.screen_capture import clear_status, write_status

__all__ = [
    "ALL_ROLES",
    "CONVERGE_STEPS",
    "ConvergeCtx",
    "ConvergeStep",
    "cmd_converge",
    "converge_host",
]


# --- unit-state steps (need a configured unit) ----------------------------


def _ensure_plugin_config_images(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    from shared.plugins_config import update_all_disk_images

    update_all_disk_images()


def _converge_skills_step(ctx: ConvergeCtx) -> None:
    """Sync repo + plugin skills into `$AVA_HOME/skills/` — the single dir the
    skill scanner loads (see `cli/commands/_converge_skills.py`)."""
    from cli.commands._converge_skills import converge_skills

    result = converge_skills(ctx.repo, ctx.ava_home)
    for kind, names in (
        ("copied", result.copied),
        ("updated", result.updated),
        ("removed", result.removed),
    ):
        if names:
            print(f"    {kind}: {', '.join(names)}")
    for warning in result.warnings:
        print(f"  ! skills: {warning}", file=sys.stderr)


def _ensure_browser(ctx: ConvergeCtx) -> None:
    """Preflight the shared headed Chrome and shed the legacy plugin file.

    chrome's MCP config now ships in `<repo>/ava_builtins/mcps/chrome/.mcp.json` (a built-in
    source the loader scans), so this step no longer writes a plugin `.mcp.json`;
    it just removes the one earlier versions wrote. When the browser is enabled,
    probe host capability; on an incapable machine emit a prominent actionable
    warning instead of failing so the rest of converge (and ava start) proceeds.
    """
    (ctx.ava_home / "plugins" / "ava_chrome" / ".mcp.json").unlink(missing_ok=True)
    if not settings.services.browser_enabled:
        return
    reason = browser_incapability()
    if reason is not None:
        if reason.startswith("no display"):
            print(f"  i browser: {browser_deps_notice(reason)}", file=sys.stderr)
            return
        print(f"  ! browser: {reason}", file=sys.stderr)
        print(browser_deps_warning(reason), file=sys.stderr)
        print("    (ava-browser will not start on this host)", file=sys.stderr)
        return
    # Host is browser-capable. Offer, once, to seed the dedicated Chrome profile
    # from the operator's daily Chrome — a security-gated choice that only fires
    # when the profile is still absent/empty AND a human is at the TTY. Watchdog
    # respawns and rollout-driven starts have no TTY, so they take the fresh
    # default and never block.
    from services.browser.profile import ensure_browser_profile

    ensure_browser_profile(interactive=sys.stdin.isatty() and sys.stdout.isatty())


def _ensure_permissions_helper(ctx: ConvergeCtx) -> None:
    """Build, sign, and launchd-load the macOS permissions helper.

    Idempotent bring-up (stable cert, compile + sign, load the LaunchAgent);
    an incapable host warns and skips, and so does a process that cannot reach
    the signing key, while a failure that is neither propagates (fail-fast).
    Desktop permissions stay a one-time manual operator step.
    Renames pre-rename env keys first so this unit's .env stays canonical.
    """
    if changed := migrate_permissions_helper_env_keys(ctx.ava_home / ".env"):
        print(f"  · permissions-helper env keys migrated: {', '.join(changed)}", file=sys.stderr)
    if not settings.services.permissions_helper_enabled:
        return
    from shared.platform_probes import permissions_helper_incapability

    reason = permissions_helper_incapability()
    if reason is not None:
        print(f"  ! permissions-helper: {reason}", file=sys.stderr)
        print("    (ava-permissions-helper will not start on this host)", file=sys.stderr)
        return
    # Capability is a property of the host; reaching the signing key is a
    # property of THIS process, so the probe above cannot answer it and the
    # attempt is what reports it. Both are environment limits and skip; anything
    # else is a real defect and propagates.
    from services.permissions_helper import converge
    from services.permissions_helper.lifecycle import PermissionsHelperSigningUnavailableError

    try:
        converge()
    except PermissionsHelperSigningUnavailableError as exc:
        print(f"  ! permissions-helper: {exc}", file=sys.stderr)
        print(
            "    (keeping the existing build; converge continues so the cluster starts)",
            file=sys.stderr,
        )


def _ensure_cross_machine_transfer(ctx: ConvergeCtx) -> None:
    """Probe the configured cross-machine transfer backend; warn, never block.

    Cross-machine file transfer no longer hard-requires a shared Google Drive
    synced folder. The configured backend is probed at start and used when
    present; a missing backend degrades to a warning so the runner still starts
    (files move through the gateway upload path, GitHub Releases, or IM file
    bridges instead). `AVA_CROSS_MACHINE_TRANSFER_BACKEND=none` skips the probe
    entirely.

    Only probed on a split deployment: a single box (this unit also carries
    'gateway') has no peer to transfer to, so the step is skipped.
    """
    if ctx.roles and "gateway" in ctx.roles:
        return
    backend = settings.general.cross_machine_transfer_backend
    if backend == "none":
        return
    from shared.google_drive import candidate_drive_dirs, find_writable_google_drive

    if find_writable_google_drive() is None:
        looked = ", ".join(str(p) for p in candidate_drive_dirs()) or "(no candidate paths)"
        print(
            "  ! cross-machine transfer backend 'drive' is unavailable on this"
            " agent-runner: no writable Google Drive synced folder, so file transfer"
            " via the shared Drive folder will not work. Install Google Drive for"
            " Desktop (macOS, or on the Windows side of a WSL host -- it then appears"
            " under /mnt/<letter>), sign in, and make sure the synced 'My Drive' area"
            " is writable to enable it; set AVA_CROSS_MACHINE_TRANSFER_BACKEND=none"
            " to skip this probe on a runner that moves files its own way."
            " Looked in: " + looked,
            file=sys.stderr,
        )


def _ensure_github_pr(ctx: ConvergeCtx) -> None:
    """Fail fast when this agent-runner cannot open+merge PRs on the memory repo.

    The memory pool is consolidated nightly by agents that push their machine
    branch and open a PR, which an arbiter merges into main. A host missing the
    GitHub CLI, not authenticated, or lacking write access silently breaks that
    sync, so block start rather than fail silently later.

    Only enforced on a split deployment: a single box (this unit also carries
    'gateway') consolidates locally with no PR round-trip, so the requirement is
    skipped. Also skipped when AVA_MEMORY_KEEP_LOCAL is set (the pool is
    local-only and never pushed off-box). A split agent-runner that does not
    consolidate via PRs can opt out with AVA_REQUIRE_GITHUB_PR=false.
    """
    if ctx.roles and "gateway" in ctx.roles:
        return
    if settings.general.memory_keep_local:
        return
    if not settings.general.require_github_pr:
        return
    from shared.github_pr import github_pr_blocker

    reason = github_pr_blocker()
    if reason is not None:
        raise RuntimeError(
            f"this agent-runner cannot open+merge GitHub PRs on the memory repo: {reason}. "
            "The memory pool is consolidated nightly by agents that push their machine "
            "branch and open/merge PRs: install the GitHub CLI (`gh`), run `gh auth login`, "
            "and grant the account write access to the memory repo, then retry."
        )


# Services that were renamed; the old `ava-<old>` session lingers after an
# upgrade because `_do_stop` only knows the current name. Reaped by converge so the
# rename never strands the old daemon.
# - `runner` -> `ops` (2026-06-05 direct-dial).
# - `watchdog` -> `gateway-watchdog` + `agent-runner-watchdog` (2026-06-20
#   per-capability split). The two replacements are in `build_services()` so they
#   land in `current` and are NOT reaped; only the retired single name is.
# - `pty-supervisor` -> nothing (2026-08-13 per-session pty hosts): agent
#   shells run in their own detached host processes now (shared/pty_sessions);
#   the supervisor daemon is gone. Reaping the old service session kills the
#   shells of that final pre-host era — the one transition where they were
#   still its children.
_RENAMED_AWAY_SERVICES: frozenset[str] = frozenset({"runner", "watchdog", "pty-supervisor"})


def _reap_legacy_sessions() -> None:
    """Migration cleanup: kill daemon sessions left under an OLDER naming scheme so a
    scheme change never strands a daemon (its pidfile would then block the new-named
    one from starting). New code only ever creates `ava-<service>` on this home's
    own session backend.

    Reaped here: `ava-<renamed-away-service>` — the service was renamed (e.g.
    `runner` -> `ops`), so the old session is no longer in `build_services()` and
    nothing else stops it. Enumerated and killed through the session backend
    (native supervisor on POSIX, winproc on Windows).

    Runs in converge (every `ava start`), so a stranded daemon is reaped on the next
    start, then `_launch_sessions` brings up the current-named one (the reaped
    process's pidfile is now free).
    """
    # Windows has no legacy naming schemes to reap (winproc named sessions
    # identically from the start).
    if not get_backend().is_posix():
        return
    # Method-local import: the self-update's in-process stop must load the
    # session backend post-checkout (tests/cli/test_update_import_timing.py).
    from cli.commands._repo import build_services, session_name
    from shared.session_backend import get_backend as _sess_backend

    current = {session_name(spec.session) for spec in build_services()}
    renamed_away = {session_name(svc) for svc in _RENAMED_AWAY_SERVICES} - current
    for name in _sess_backend().list_sessions():
        if name in renamed_away:
            _sess_backend().kill_session(name, graceful=False)


def _reap_legacy_sessions_step(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    _reap_legacy_sessions()


def _migrate_registry_keys_step(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    """Idempotently normalize `clusters.json` to the migration-window form
    (name-keyed, compat name/db_name preserved). Reads already re-key by home;
    this repairs a file a buggy path-only build rewrote without the compat
    fields (which would crash a box-shared pre-cutover reader). See
    shared.cluster.migrate_registry_keys."""
    from shared.cluster import migrate_registry_keys

    if migrate_registry_keys():
        print("  · normalized clusters.json to the backward-compatible window form")


def _migrate_legacy_disabled_marker(ctx: ConvergeCtx) -> None:
    """Carry a pre-rename `$AVA_HOME/skipped_services` over to the name the
    current code reads (`disabled_services`), so an operator's durable
    `--disable-service` intent recorded before the rename is honored instead of
    silently ignored. One-shot: after it runs there is no legacy file left, so
    every later converge is a no-op. See shared.disabled_services.migrate_legacy_marker
    for the both-files-exist rule."""
    from shared.disabled_services import migrate_legacy_marker

    summary = migrate_legacy_marker(ctx.ava_home)
    if summary is not None:
        print(f"  · {summary}")


def _ensure_screen_capture(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    """Preflight OS-level screen capture on agent-runner hosts.

    Asks the permissions helper — the process that actually performs
    ``screencapture_region`` — whether it holds the Screen Recording grant, and
    records the answer for the next agent startup to report. Runs after the
    helper's own bring-up step, and only where a helper can exist at all: on a
    host that cannot run one, that step already said so, and a derived second
    complaint here would be noise rather than news.
    """
    from shared.platform_probes import permissions_helper_incapability

    if (
        not settings.services.permissions_helper_enabled
        or permissions_helper_incapability() is not None
    ):
        clear_status()
        return

    from services.permissions_helper.client import check_screen_capture

    status = check_screen_capture()
    if status.available:
        # Drop any stale "unavailable" file so a fixed host does not fire a
        # false notification on the next agent startup.
        clear_status()
        return
    write_status(status)
    print(f"  ! {status.headline}: {status.diagnostic}", file=sys.stderr)


def _ensure_accessibility(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    """Preflight Accessibility on agent-runner hosts.

    Accessibility gates the helper's synthetic clicks and keystrokes; macOS
    silently drops those events when the helper lacks the grant. Record the
    helper's answer for the next agent startup to report, after the helper has
    been brought up and only where it can exist.
    """
    from shared.platform_probes import permissions_helper_incapability

    if (
        not settings.services.permissions_helper_enabled
        or permissions_helper_incapability() is not None
    ):
        clear_accessibility_status()
        return

    from services.permissions_helper.client import check_accessibility

    status = check_accessibility()
    if status.available:
        clear_accessibility_status()
        return
    write_accessibility_status(status)
    print(f"  ! {status.headline}: {status.diagnostic}", file=sys.stderr)


def _warn_untracked_migrations(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    """Operator-visible warning when migrations/ holds files git does not track.

    The applier skips untracked migrations with a log warning (Task #998); this
    surfaces the same fact on the console every `ava start` / `ava cluster update`
    converge runs, so an operator who wrote a migration into the prod checkout
    without committing it is told it will NOT be applied — the silent no-op
    would otherwise read as "my migration ran". Gateway-only: the gateway is the
    single schema writer (e9d51acea).
    """
    from shared.migrations import untracked_migration_files

    names = untracked_migration_files()
    if names:
        print(
            f"⚠ [migration] {len(names)} untracked file(s) in migrations/ are NOT "
            f"in git and will NOT be applied: {', '.join(names)}"
        )


CONVERGE_STEPS: tuple[ConvergeStep, ...] = (
    # Warning-only ownership preflight must run before every write-capable step:
    # root-owned paths otherwise fail before converge can print the exact repair.
    ConvergeStep("$AVA_HOME ownership preflight", _ensure_ownership_preflight),
    # Reset the prod checkout before any other step reads the tree: a tampered
    # tree would make every later step misbehave, and resetting first means the
    # rest of converge runs against the installed commit.
    ConvergeStep("source tree reset + clean", ensure_source_tree_integrity, host_global=True),
    ConvergeStep("prod editable .pth target", _ensure_prod_editable_pth, host_global=True),
    ConvergeStep(
        "prod editable site-packages protection",
        _ensure_prod_editable_dir_protection,
        host_global=True,
    ),
    ConvergeStep("prod editable exec gate", _ensure_prod_editable_exec_gate, host_global=True),
    ConvergeStep("ava symlink on PATH", _ensure_ava_symlink, host_global=True),
    ConvergeStep("~/.local/bin on PATH", _ensure_local_bin_on_path, host_global=True),
    ConvergeStep("$AVA_HOME dir skeleton", _ensure_ava_home_dirs),
    # Codex and Claude Code own their global homes. This prod-only host step
    # contributes exactly the Ava operator skill when those homes already exist.
    # The private ownership ledger lives under the skeleton created above.
    ConvergeStep(
        "external agent operator skill",
        converge_external_agent_skill,
        host_global=True,
    ),
    # Warning-only port preflight: bind-check the cluster's port block + this
    # unit's health ports before anything is launched; foreign occupants are
    # printed and logged, never blocking (the blocking health-port gate is
    # start._refuse_occupied_health_ports, which runs later with the roster).
    ConvergeStep(
        "port conflict preflight",
        _ensure_port_preflight,
        requires_unit_config=True,
    ),
    # Warning-only health preflight: data-plane reachability (pg/redis, local on
    # gateway / remote on runner) + checkout state (HEAD vs cluster pin, dirty
    # marker). Findings are printed and appended to $AVA_HOME/logs/health_preflight.log,
    # never blocking — the same contract as the port preflight above.
    ConvergeStep(
        "health preflight",
        _ensure_health_preflight,
        requires_unit_config=True,
    ),
    # Fetch the vendored relocatable Postgres + inject pgvector ahead of the
    # data-plane bring-up, so a clean gateway host needs no
    # `brew install postgresql@17` (nor a pgvector package). Gateway-only.
    ConvergeStep(
        "vendored Postgres + pgvector binaries",
        _ensure_pg_binaries_step,
        roles=frozenset({"gateway"}),
    ),
    ConvergeStep(
        "physical backup foundation",
        converge_pitr_foundation,
        roles=frozenset({"gateway"}),
    ),
    # Reconcile the one DB URL (AVA_DB_URL) with the pooler toggle + preflight the
    # PgBouncer binary when the pooler is enabled (gateway box's data plane).
    ConvergeStep(
        "one DB URL + pgbouncer binary (when enabled)",
        _ensure_pgbouncer_step,
        roles=frozenset({"gateway"}),
    ),
    # Legacy clusters carry a username-less AVA_REDIS_URL; backfill the identity
    # so the redis-acl healthcheck has an ACL user to re-affirm. Gateway-only.
    ConvergeStep(
        "redis URL identity backfill",
        _ensure_redis_url_identity_step,
        roles=frozenset({"gateway"}),
    ),
    # Redis itself stays loopback-only. The host-global launchd relay is the
    # authenticated off-box ingress for a split macOS gateway; converge owns
    # both its installed source and job so a fresh host and an upgraded host
    # receive the same implementation.
    ConvergeStep(
        "Redis private-network bridge",
        ensure_redis_bridge,
        roles=frozenset({"gateway"}),
        host_global=True,
        requires_unit_config=True,
    ),
    # The frontend session (and its `npm run build`) runs on gateway hosts, so
    # the guard gates exactly the hosts whose bundle could go stale.
    ConvergeStep(
        "no frontend build-time env overrides",
        ensure_no_frontend_env_overrides,
        roles=frozenset({"gateway"}),
    ),
    # Retire this machine's per-machine host override file into its .env (file-only,
    # no DB — safe to run here before Postgres is up). The cluster DB-row migration
    # needs Postgres + schema, so it runs later in `ava start` (after migrations
    # apply, before the gateway session starts), not here. Idempotent.
    ConvergeStep(
        "migrate legacy env keys -> .env",
        _migrate_host_config_to_env,
        requires_unit_config=True,
    ),
    # A block-style unit whose .env predates a health daemon's slot gets the
    # missing keys derived from its own block, so no daemon falls back to the
    # shared legacy segment on a co-located namespace. File-only, idempotent;
    # legacy units (no consistent block) are untouched.
    ConvergeStep(
        "backfill missing daemon health-port keys",
        _backfill_health_port_keys_step,
        requires_unit_config=True,
    ),
    # Warning-only: untracked `.sql` files in migrations/ are never applied
    # (Task #998) — say so on the console instead of letting the log warning be
    # the only trace. Gateway-only: the gateway is the single schema writer.
    ConvergeStep(
        "untracked migrations warning",
        _warn_untracked_migrations,
        roles=frozenset({"gateway"}),
    ),
    # Plugin config images are read only by agent processes, which run on
    # agent-runners. The gateway never loads a plugin, so materializing its
    # disk images there is dead work (and feeds the inventory leak).
    ConvergeStep(
        "plugin config images",
        _ensure_plugin_config_images,
        roles=frozenset({"agent-runner"}),
        requires_unit_config=True,
    ),
    # Skills load in agent processes only, from the single $AVA_HOME/skills/
    # dir this step keeps in sync with the repo + plugin source trees.
    ConvergeStep(
        "skills sync -> $AVA_HOME/skills",
        _converge_skills_step,
        roles=frozenset({"agent-runner"}),
        requires_unit_config=True,
    ),
    ConvergeStep("otel collector sidecar", ensure_otel_collector_step, requires_unit_config=True),
    ConvergeStep("lgtm native backends", ensure_lgtm_native_step),
    # The native LGTM observability backend — a host singleton, so the step is
    # gated on the $AVA_HOME/lgtm-host marker file
    # inside, not on roles: only the one home the operator marked brings it up;
    # every other cluster on the box (dev worktrees included) no-ops.
    ConvergeStep("lgtm observability stack", ensure_lgtm_stack_step),
    ConvergeStep(
        "browser capability + plugin",
        _ensure_browser,
        roles=frozenset({"agent-runner"}),
        requires_unit_config=True,
    ),
    ConvergeStep(
        "permissions helper build + sign + load",
        _ensure_permissions_helper,
        roles=frozenset({"agent-runner"}),
        requires_unit_config=True,
    ),
    ConvergeStep(
        "cross-machine transfer backend",
        _ensure_cross_machine_transfer,
        roles=frozenset({"agent-runner"}),
        requires_unit_config=True,
    ),
    ConvergeStep(
        "github PR capability",
        _ensure_github_pr,
        roles=frozenset({"agent-runner"}),
        requires_unit_config=True,
    ),
    # macOS Application Firewall allow rules for the binaries this host serves
    # off-box ports from. Rootless-first repair with an older-macOS `sudo -n`
    # fallback; both capabilities (a gateway serves HTTP, a runner serves its
    # ops port), and silent on every host that cannot have the defect.
    ConvergeStep("macOS firewall allow list", ensure_firewall_allowlist),
    # The macOS permission-prompt watcher was removed 2026-08-26 (user ruling:
    # drop all TCC interception); boot out its KeepAlive LaunchAgent so a
    # rollout of the removal cannot leave the job crash-looping against the
    # deleted watcher.py. No-op once the job and plist are gone.
    ConvergeStep(
        "legacy macOS permission-watcher removal",
        remove_legacy_permission_watcher,
        host_global=True,
    ),
    # Warning-only assertion of the operator-approved Homebrew pins. Both roles
    # may share the same macOS host; drift is detected, never repaired here.
    ConvergeStep("Homebrew formula pins", ensure_brew_pin),
    ConvergeStep("reap legacy-named sessions", _reap_legacy_sessions_step),
    ConvergeStep("registry home-path keys", _migrate_registry_keys_step),
    # Pure file work under this cluster's home, so it needs no unit config and no
    # capability: both roles read the marker (the watchdog runs on either), and a
    # standalone `ava converge` on a not-yet-configured host must still repair it.
    # Position is only required to be inside converge — `cmd_start` runs converge
    # (step 1) well before it resolves the launch skip set (step 4), so a
    # migration lands in time for the very start that performs it.
    ConvergeStep("legacy disabled-services marker", _migrate_legacy_disabled_marker),
    ConvergeStep(
        "screen capture availability",
        _ensure_screen_capture,
        roles=frozenset({"agent-runner"}),
        requires_unit_config=True,
    ),
    ConvergeStep(
        "accessibility availability",
        _ensure_accessibility,
        roles=frozenset({"agent-runner"}),
        requires_unit_config=True,
    ),
    # Windows-only: reclaim \Ava\* tasks under a retired home slug (task #1196).
    ConvergeStep("reap stale Windows tasks", reap_stale_schtasks, requires_unit_config=True),
    ConvergeStep(
        "health probe cron job",
        ensure_health_probe_cron,
        roles=frozenset({"gateway"}),
        requires_unit_config=True,
    ),
    # The always-up fleet UI entry: owns the frontend port, proxies the app,
    # survives updates by construction (not a service session). Gateway-only.
    ConvergeStep(
        "fleet UI gate (always-up entry)",
        ensure_gate,
        roles=frozenset({"gateway"}),
        requires_unit_config=True,
    ),
    # The watchdog keeps the services alive; this keeps the WATCHDOG alive.
    # Runs on any serving role — an agent-runner-only box needs it just as much
    # (that is where the gap was observed), and the step itself fans out over
    # whichever capabilities the unit carries.
    ConvergeStep(
        "watchdog probe job",
        ensure_watchdog_probe,
        requires_unit_config=True,
    ),
    # Boot-time autostart of the whole cluster. host_global so only the prod
    # install registers it (a dev worktree cluster must not auto-start on reboot);
    # runs on any serving role since an agent-runner-only box must self-restart too.
    ConvergeStep(
        "cluster boot autostart",
        ensure_cluster_autostart,
        host_global=True,
        requires_unit_config=True,
    ),
)


def converge_host(
    repo: Path,
    roles: MachineRoles | None,
    *,
    ava_home: Path | None = None,
    steps: tuple[ConvergeStep, ...] = CONVERGE_STEPS,
) -> None:
    """Run the applicable converge steps in order; idempotent, fail-fast.

    `roles is None` means the unit is not configured yet (fresh install): steps
    with requires_unit_config=True are skipped with a printed notice. A
    configured host runs a step when it carries any capability the step is scoped
    to (`roles & step.roles`), so a single-box gateway,agent-runner host runs
    both the gateway and agent-runner steps.
    """
    resolved_home = (
        ava_home if ava_home is not None else Path(settings.general.ava_home).expanduser()
    )
    ctx = ConvergeCtx(repo=repo, ava_home=resolved_home, roles=roles)

    # Host-global steps belong to the host's prod install (the default home
    # `~/.ava`), not to a dev cluster spun up from a worktree — a dev cluster must
    # not repoint `~/.local/bin/ava` or rewrite the shell rc.
    #
    # Identity is the home path, so the criterion is direct: this unit's resolved
    # home must BE the default home. An uninstalled dev worktree resolves its home
    # to `~/.ava` too (the unanchored fallback), so additionally require a repo
    # that is not a dev worktree (`.worktrees/...` or `.claude/worktrees/...`) —
    # host-global wiring runs only for a genuine prod-install checkout.
    repo_resolved = str(ctx.repo.resolve())
    repo_is_worktree = ".claude/worktrees" in repo_resolved or "/.worktrees/" in repo_resolved
    is_prod_install = is_default_home(ctx.ava_home) and not repo_is_worktree

    print("\n→ converge host")
    for step in steps:
        if step.host_global and not is_prod_install:
            print(
                f"  · {step.name}: skipped (dev cluster/worktree — host-global wiring is prod-install only)"
            )
            continue
        # When roles is None (unconfigured host) the capability filter is
        # skipped; every role-scoped step in CONVERGE_STEPS is also
        # requires_unit_config=True, so it is caught by the next guard. A
        # role-scoped step that is NOT requires_unit_config would run on an
        # unconfigured host — give such a step requires_unit_config=True or
        # extend this guard.
        if roles is not None and not (roles & step.roles):
            print(f"  · {step.name}: skipped (roles {','.join(sorted(roles))})")
            continue
        if roles is None and step.requires_unit_config:
            print(f"  · {step.name}: deferred to first `ava start` (unit not configured yet)")
            continue
        try:
            step.apply(ctx)
        except Exception as e:
            print(f"  ✗ {step.name}: {e}", file=sys.stderr)
            raise
        print(f"  ✓ {step.name}")


def cmd_converge() -> int:
    """`ava converge` — bring this host to the state the current code expects (idempotent)."""
    import cli.commands as _ns
    from shared import maintenance
    from shared.platform import raise_fd_limit

    maintenance.require_start_allowed()
    raise_fd_limit(65536)  # converge spawns services + frontend deps; children inherit
    repo = _ns._repo_root()
    roles = _ns._roles_or_none()
    print(f"[ava converge] cwd = {repo}  roles = {','.join(sorted(roles)) if roles else 'unknown'}")

    # Source-integrity guard: detect manual git operations in the source tree.
    # Converge does not launch services, so the guard only warns — it does not
    # auto-heal (uv sync) or block. The full guard (with auto-heal) runs at
    # `ava start` time; this is an early-warning check for standalone converge.
    import contextlib
    import subprocess as _sp

    with contextlib.suppress(Exception):
        _head = _sp.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if _head.returncode == 0:
            _head_sha = _head.stdout.strip()
            with contextlib.suppress(Exception):
                from shared.source_integrity import get as _get_installed

                _installed = _get_installed()
                if _installed is not None and _head_sha != _installed:
                    print(
                        f"\n⚠  SOURCE INTEGRITY: HEAD ({_head_sha[:7]}) != "
                        f"installed ({_installed[:7]})\n"
                        f"   The source tree changed outside of `ava cluster update`. "
                        f"Run `ava cluster update` or `ava start` to auto-heal.\n",
                        file=sys.stderr,
                    )

    _ns.converge_host(repo, roles)
    # After the steps, not inside them: standalone converge runs against a
    # cluster that is already up, which is the precondition this needs and which
    # a CONVERGE_STEPS entry would not have on the `ava start` path.
    from cli.commands._converge_extensions import (
        adopt_local_extensions,
        materialize_cluster_extensions,
    )

    adopt_local_extensions()
    materialize_cluster_extensions()
    return 0
