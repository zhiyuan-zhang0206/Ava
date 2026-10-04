"""Idempotent host convergence for the ava lifecycle.

Bring a machine to the host-level state the current code expects: `ava` on
PATH, ~/.local/bin on PATH, the $AVA_HOME dir skeleton, fresh plugin config
images. Run by development `cmd_start`, and
standalone via `ava converge`. Verified release startup consumes prepared assets
without mutating its image.
"""

# Host preparation is idempotent and fail-fast. Role and selected-service
# filters share the start lifecycle's desired roster.
from __future__ import annotations

import sys
from pathlib import Path

from base.cluster import is_default_home
from base.cluster.machine import MachineRoles
from base.config import settings
from base.host.converge.accessibility import (
    clear_status as clear_accessibility_status,
)
from base.host.converge.accessibility import (
    write_status as write_accessibility_status,
)
from base.host.converge.browser_deps import browser_deps_notice, browser_deps_warning
from base.host.converge.screen_capture import clear_status, write_status
from base.host.env.dotenv_boot import resolve_ava_home
from base.host.system.probes import browser_incapability
from base.telemetry.lgtm_local import BACKENDS
from cli.commands.converge._brew_pin import ensure_brew_pin
from cli.commands.converge._frontend_env import ensure_no_frontend_env_overrides
from cli.commands.converge._os_jobs import (
    ensure_cluster_autostart,
    ensure_health_probe_cron,
    ensure_logs_maintenance,
    ensure_packages_refresh_job,
    ensure_pr_flow_job,
    ensure_walg_job,
)
from cli.commands.converge._ownership_preflight import (
    ensure_ownership_preflight as _ensure_ownership_preflight,
)
from cli.commands.converge._steps import (
    _PATH_BEGIN as _PATH_BEGIN,
)
from cli.commands.converge._steps import (
    _PATH_END as _PATH_END,
)
from cli.commands.converge._steps import (
    _ensure_ava_home_dirs,
    _ensure_ava_on_path,
    _ensure_local_bin_on_path,
    _ensure_pg_binaries_step,
    ensure_local_git_hooks,
)
from cli.commands.converge._steps import (
    _shell_rc_path as _shell_rc_path,
)
from cli.commands.converge.firewall import ensure_firewall_allowlist
from cli.commands.converge.health_preflight import (
    ensure_health_preflight as _ensure_health_preflight,
)
from cli.commands.converge.redis_bridge import ensure_redis_bridge

# The step contract lives in spec.py so step implementations can span
# modules without an import cycle; re-exported here because every caller and
# test reaches for `cli.commands.converge.host.ConvergeCtx` / `ALL_ROLES`.
from cli.commands.converge.spec import ALL_ROLES, ConvergeCtx, ConvergeStep
from cli.commands.converge.walg import converge_walg
from cli.commands.data_plane.pgbouncer import ensure_pgbouncer_step
from cli.commands.extensions.external_skills import converge_external_agent_skill
from cli.commands.observability.lgtm_native import ensure_lgtm_native_step
from cli.commands.observability.otel_collector import ensure_otel_collector_step

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
    from base.packages.plugins.enable_config import update_all_disk_images

    update_all_disk_images()


def _converge_skills_step(ctx: ConvergeCtx) -> None:
    """Sync repo + plugin skills into `$AVA_HOME/skills/` — the single dir the
    skill scanner loads (see `cli/commands/extensions/skills_sync.py`)."""
    from cli.commands.extensions.skills_sync import converge_skills

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
    from services.desktop.browser.profile import ensure_browser_profile

    ensure_browser_profile(interactive=sys.stdin.isatty() and sys.stdout.isatty())


def _ensure_permissions_helper(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    """Require the stable signed ancestor before starting a macOS root."""
    if sys.platform != "darwin":
        return
    if not settings.services.permissions_helper_enabled:
        raise RuntimeError("macOS root supervision requires the permissions helper")
    from base.host.system.probes import permissions_helper_incapability

    reason = permissions_helper_incapability()
    if reason is not None:
        raise RuntimeError(f"macOS root supervision cannot start: {reason}")
    from services.desktop.permissions_helper import converge

    converge()


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
    from base.host.converge.google_drive import candidate_drive_dirs, find_writable_google_drive

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
    from base.deploy.git.github_pr import github_pr_blocker

    reason = github_pr_blocker()
    if reason is not None:
        raise RuntimeError(
            f"this agent-runner cannot open+merge GitHub PRs on the memory repo: {reason}. "
            "The memory pool is consolidated nightly by agents that push their machine "
            "branch and open/merge PRs: install the GitHub CLI (`gh`), run `gh auth login`, "
            "and grant the account write access to the memory repo, then retry."
        )


def _ensure_screen_capture(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    """Preflight OS-level screen capture on agent-runner hosts.

    Asks the permissions helper — the process that actually performs
    ``screencapture_region`` — whether it holds the Screen Recording grant, and
    records the answer for the next agent startup to report. Runs after the
    helper's own bring-up step, and only where a helper can exist at all: on a
    host that cannot run one, that step already said so, and a derived second
    complaint here would be noise rather than news.
    """
    from base.host.system.probes import permissions_helper_incapability

    if (
        not settings.services.permissions_helper_enabled
        or permissions_helper_incapability() is not None
    ):
        clear_status()
        return

    from services.desktop.permissions_helper.client import check_screen_capture

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
    from base.host.system.probes import permissions_helper_incapability

    if (
        not settings.services.permissions_helper_enabled
        or permissions_helper_incapability() is not None
    ):
        clear_accessibility_status()
        return

    from services.desktop.permissions_helper.client import check_accessibility

    status = check_accessibility()
    if status.available:
        clear_accessibility_status()
        return
    write_accessibility_status(status)
    print(f"  ! {status.headline}: {status.diagnostic}", file=sys.stderr)


def _warn_untracked_migrations(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    """Operator-visible warning when migrations/ holds files git does not track.

    The applier skips untracked migrations with a log warning (Task #998); this
    surfaces the same fact on the console every `ava start` / the fleet update
    converge runs, so an operator who wrote a migration into the prod checkout
    without committing it is told it will NOT be applied — the silent no-op
    would otherwise read as "my migration ran". Gateway-only: the gateway is the
    single schema writer (e9d51acea).
    """
    from base.deploy.schema.migrations import untracked_migration_files

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
    ConvergeStep("ava on PATH", _ensure_ava_on_path, host_global=True),
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
    # Warning-only health preflight: data-plane reachability (pg/redis, local on
    # gateway / remote on runner) + checkout state (dirty marker). Findings are
    # printed and appended to $AVA_HOME/logs/health_preflight.log, never blocking.
    ConvergeStep(
        "health preflight",
        _ensure_health_preflight,
        requires_unit_config=True,
    ),
    # Validate the selected installed runtime, or provision the pinned vendor
    # distribution when no installation exists. Gateway-only.
    ConvergeStep(
        "PostgreSQL 17 + pgvector runtime",
        _ensure_pg_binaries_step,
        roles=frozenset({"gateway"}),
    ),
    # WAL archiving, when AVA_WALG_CONFIG_FILE switches it on: the pinned binary, a
    # validated configuration and the pinned key fingerprint, all before Postgres
    # starts. A no-op while the key is unset.
    ConvergeStep(
        "WAL-G archiving",
        converge_walg,
        roles=frozenset({"gateway"}),
    ),
    # Reconcile the one DB URL (AVA_DB_URL) with the pooler toggle + preflight the
    # PgBouncer binary when the pooler is enabled (gateway box's data plane).
    ConvergeStep(
        "one DB URL + pgbouncer binary (when enabled)",
        ensure_pgbouncer_step,
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
        services=frozenset({"frontend"}),
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
    ConvergeStep(
        "otel collector sidecar",
        ensure_otel_collector_step,
        requires_unit_config=True,
        services=frozenset({"otel-collector"}),
    ),
    ConvergeStep("lgtm native backends", ensure_lgtm_native_step, services=frozenset(BACKENDS)),
    ConvergeStep(
        "browser capability + plugin",
        _ensure_browser,
        roles=frozenset({"agent-runner"}),
        requires_unit_config=True,
        services=frozenset({"browser", "browser-mcp"}),
    ),
    ConvergeStep(
        "permissions helper build + sign + load",
        _ensure_permissions_helper,
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
    # Warning-only assertion of the operator-approved Homebrew pins. Both roles
    # may share the same macOS host; drift is detected, never repaired here.
    ConvergeStep("Homebrew formula pins", ensure_brew_pin),
    # Warning-only assertion of the local Git hook installations (a drifted
    # INSTALL_PYTHON or a missing stage silently disables local checks).
    # Drift is detected, never repaired here.
    ConvergeStep("local Git hook pointers", ensure_local_git_hooks),
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
    ConvergeStep(
        "daily logs maintenance",
        ensure_logs_maintenance,
        requires_unit_config=True,
    ),
    # The content channel's recurring pass: per-machine skills state, so every
    # serving unit registers it (the command owns all further gating).
    ConvergeStep(
        "packages refresh job",
        ensure_packages_refresh_job,
        requires_unit_config=True,
    ),
    # The PR-flow sampler's daily job: registers only on a production home
    # whose machine holds the sampler's credentials (gh + Trunk token) — in
    # the fleet, macmini; the step itself owns the gating and skips elsewhere.
    ConvergeStep(
        "PR flow sampler job",
        ensure_pr_flow_job,
        requires_unit_config=True,
    ),
    # The WAL-G daily tick: registered while AVA_WALG_CONFIG_FILE is set, removed
    # when it is not. The gateway owns the Postgres it backs up.
    ConvergeStep(
        "WAL-G backup job",
        ensure_walg_job,
        roles=frozenset({"gateway"}),
        requires_unit_config=True,
    ),
    ConvergeStep(
        "health probe cron job",
        ensure_health_probe_cron,
        roles=frozenset({"gateway"}),
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
    services: frozenset[str] | None = None,
) -> None:
    """Run the applicable converge steps in order; idempotent, fail-fast.

    `roles is None` means the unit is not configured yet (fresh install): steps
    with requires_unit_config=True are skipped with a printed notice. A
    configured host runs a step when it carries any capability the step is scoped
    to (`roles & step.roles`), so a single-box gateway,agent-runner host runs
    both the gateway and agent-runner steps.
    """
    resolved_home = ava_home if ava_home is not None else resolve_ava_home()
    selected = _desired_service_names(roles) if services is None else services
    ctx = ConvergeCtx(repo=repo, ava_home=resolved_home, roles=roles, services=selected)

    # Host-global steps belong to the host's prod install (the default home
    # `~/.ava`), not to a dev cluster spun up from a worktree — a dev cluster must
    # not repoint `~/.local/bin/ava` or rewrite the shell rc.
    #
    # Identity is the home path, so the criterion is direct: this unit's resolved
    # home must BE the default home. A dev worktree resolves its home to `~/.ava`
    # too whenever AVA_HOME is unset, so additionally require a repo
    # that is not a dev worktree (`.worktrees/...` or `.claude/worktrees/...`) —
    # host-global wiring runs only for a genuine prod-install checkout.
    repo_resolved = str(ctx.repo.resolve())
    repo_is_worktree = ".claude/worktrees" in repo_resolved or "/.worktrees/" in repo_resolved
    is_prod_install = is_default_home(ctx.ava_home) and not repo_is_worktree

    print("\n→ converge host")
    for step in steps:
        reason = _skip_reason(ctx, step, is_prod_install=is_prod_install)
        if reason is not None:
            print(f"  · {step.name}: skipped ({reason})")
            continue
        try:
            step.apply(ctx)
        except Exception as e:
            print(f"  ✗ {step.name}: {e}", file=sys.stderr)
            raise
        print(f"  ✓ {step.name}")


def _skip_reason(ctx: ConvergeCtx, step: ConvergeStep, *, is_prod_install: bool) -> str | None:
    if step.services and not step.services.intersection(ctx.services):
        return "consumer service not selected"
    if step.host_global and not is_prod_install:
        return "dev cluster/worktree — host-global wiring is prod-install only"
    if ctx.roles is not None and not (ctx.roles & step.roles):
        return f"roles {','.join(sorted(ctx.roles))}"
    if ctx.roles is None and step.requires_unit_config:
        return "unit not configured yet; initialize with ava start"
    return None


def _desired_service_names(roles: MachineRoles | None) -> frozenset[str]:
    from base.deploy.lifecycle.service_selection import read_selection
    from cli.commands._repo import _services_for_roles_annotated

    if roles is None:
        return frozenset()
    selection = read_selection()
    return frozenset(
        spec.session
        for spec, gate in _services_for_roles_annotated(roles)
        if gate is None and selection.enabled(spec.session)
    )


def cmd_converge() -> int:
    """`ava converge` — bring this host to the state the current code expects (idempotent)."""
    from base.deploy.maintenance import admission
    from base.native_process.os_platform import raise_fd_limit
    from cli.commands import _repo

    admission.require_start_allowed()
    raise_fd_limit(65536)  # converge spawns services + frontend deps; children inherit
    repo = _repo._repo_root()
    roles = _repo._roles_or_none()
    print(f"[ava converge] cwd = {repo}  roles = {','.join(sorted(roles)) if roles else 'unknown'}")

    converge_host(repo, roles)
    # After the steps, not inside them: standalone converge runs against a
    # cluster that is already up, which is the precondition this needs and which
    # a CONVERGE_STEPS entry would not have on the `ava start` path.
    from cli.commands.extensions.materialize import (
        adopt_local_extensions,
        materialize_cluster_extensions,
    )

    adopt_local_extensions()
    materialize_cluster_extensions()
    return 0
