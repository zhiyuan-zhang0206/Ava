"""Early host-wiring and unit-state converge steps."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from cli.commands._converge_spec import ConvergeCtx
from shared.config import settings
from shared.platform_backend import get_backend
from shared.private_storage import converge_private_tree, ensure_private_file

# --- host-wiring steps (no preconditions) ---------------------------------


def _ensure_ava_symlink(ctx: ConvergeCtx) -> None:
    # On Windows the `ava` entry point is `.venv\Scripts\ava.exe`, reached via the
    # uv/venv on PATH (or the install.ps1 shim) — there is no `~/.local/bin/ava`
    # symlink model, and symlink creation needs admin/dev-mode. Skip.
    if not get_backend().supports_ava_symlink():
        return
    target = ctx.repo / ".venv" / "bin" / "ava"
    link = Path.home() / ".local" / "bin" / "ava"
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() and link.readlink() == target:
        return
    link.unlink(missing_ok=True)
    link.symlink_to(target)


_PATH_BEGIN = "# >>> ava path >>>"
_PATH_END = "# <<< ava path <<<"


def _shell_rc_path() -> Path:
    return Path.home() / (".zshrc" if os.environ.get("SHELL", "").endswith("zsh") else ".bashrc")


def _ensure_local_bin_on_path(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    # POSIX shell-rc PATH wiring; Windows uses a different PATH model (the
    # install.ps1 shim / venv Scripts on PATH), so there is no .bashrc to edit.
    if not get_backend().supports_shell_rc():
        return
    local_bin = Path.home() / ".local" / "bin"
    rc = _shell_rc_path()
    line = f'case ":$PATH:" in *":{local_bin}:"*) ;; *) export PATH="{local_bin}:$PATH" ;; esac'
    block = f"{_PATH_BEGIN}\n{line}\n{_PATH_END}"
    existing = rc.read_text() if rc.exists() else ""
    pattern = re.compile(re.escape(_PATH_BEGIN) + r".*?" + re.escape(_PATH_END), re.DOTALL)
    if pattern.search(existing):
        new = pattern.sub(block, existing)
    else:
        sep = "" if existing == "" or existing.endswith("\n") else "\n"
        new = f"{existing}{sep}{block}\n"
    if new != existing:
        rc.write_text(new)


def _ensure_ava_home_dirs(ctx: ConvergeCtx) -> None:
    for sub in ("configs", "secrets"):
        (ctx.ava_home / sub).mkdir(parents=True, exist_ok=True)
    for sub in ("logs", "workspaces", "memory"):
        converge_private_tree(ctx.ava_home / sub)
    marker = ctx.ava_home / "logs" / ".metadata_never_index"
    marker.touch(exist_ok=True)
    ensure_private_file(marker)


def _ensure_prod_editable_pth(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    """Keep the prod virtualenv anchored to stable, allowlisted source (pth + direct_url)."""
    import shared.cluster_drift
    import shared.editable_install

    source_root = shared.cluster_drift.prod_source_dir()
    if source_root is None:
        return
    repairs = shared.editable_install.repair_editable_install(
        source_root,
        allowed_roots=(Path.home() / "Ava",),
    )
    for repair in repairs:
        print(
            f"  ! poisoned editable install: {repair.path} pointed at "
            f"{repair.poisoned_target!r}; repaired to {repair.source_root}",
            file=sys.stderr,
        )


def _ensure_prod_editable_dir_protection(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    """Block atomic editable-record replacement outside the sanctioned write window."""
    if os.name == "nt":
        return
    import shared.cluster_drift
    import shared.editable_install
    from cli.commands.status import _update_in_flight

    if _update_in_flight():
        print(
            "  · prod site-packages protection skipped: cluster update in flight", file=sys.stderr
        )
        return
    source_root = shared.cluster_drift.prod_source_dir()
    if source_root is None:
        return
    directories = (
        *shared.editable_install.editable_site_packages_dirs(source_root),
        *shared.editable_install.editable_dist_info_dirs(source_root),
    )
    for directory in directories:
        if directory.stat().st_mode & 0o777 == 0o555:
            continue
        directory.chmod(0o555)


def _ensure_prod_editable_exec_gate(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    """Fail converge unless the prod venv's console command can import agent code.

    The earlier repair step can restore known pointer and direct-URL records.
    This final gate covers the remaining half-uninstalls, including a missing
    dist-info directory or console script that only a package reinstall can
    restore. An import proof is the final discriminator because a read-only
    site-packages directory can make uv report success after a partial change.
    """

    import shared.cluster_drift
    import shared.editable_install
    from cli.commands._update_uv_sync import run_uv_sync

    source_root = shared.cluster_drift.prod_source_dir()
    if source_root is None:
        return
    allowed_roots = (Path.home() / "Ava",)
    violations = list(
        shared.editable_install.editable_install_violations(
            source_root,
            allowed_roots=allowed_roots,
        )
    )
    violations.extend(shared.editable_install.editable_console_script_violations(source_root))
    if violations:
        print(
            "  ! prod editable install incomplete; attempting one package reinstall recovery",
            file=sys.stderr,
        )
        sync_result = run_uv_sync(source_root, reinstall_package="ava")
        violations = list(
            shared.editable_install.editable_install_violations(
                source_root,
                allowed_roots=allowed_roots,
            )
        )
        violations.extend(shared.editable_install.editable_console_script_violations(source_root))
        if sync_result.returncode != 0:
            violations.append(f"uv sync recovery failed (rc={sync_result.returncode})")
    violations.extend(
        shared.editable_install.editable_import_gate(source_root, allowed_roots=allowed_roots)
    )
    if not violations:
        return
    detail = "\n".join(f"- {violation}" for violation in violations)
    print(f"  ✗ prod editable exec gate failed:\n{detail}", file=sys.stderr)
    manual_recovery = f"cd {source_root} && uv sync --reinstall-package ava"
    raise RuntimeError(
        f"prod editable exec gate failed:\n{detail}\nRun {manual_recovery} or ava cluster update."
    )


def _ensure_pg_binaries_step(ctx: ConvergeCtx) -> None:  # noqa: ARG001
    """Fetch the vendored relocatable Postgres + inject the pinned pgvector
    extension files (both idempotent — no-ops once the host-level
    `~/.ava/runtime/` tree carries them), so a gateway host needs no
    `brew install postgresql@17` before the data plane comes up and the
    memory-search pgvector backend has its extension binaries."""
    if not get_backend().supports_data_plane():
        return  # Platform uses container-based pg, not vendored binaries
    from shared.runtime_binaries import ensure_pg_binaries, ensure_pgvector

    ensure_pg_binaries()
    ensure_pgvector()


def _ensure_redis_url_identity_step(ctx: ConvergeCtx) -> None:
    """Backfill the data-plane identity (username) into a legacy AVA_REDIS_URL.

    Gateway-only (the redis instance and its ACL user are the gateway box's data
    plane). A cluster born before the names-as-data ACL model carries
    `redis://:<secret>@host/0` — no username — so its runtime dials as the redis
    `default` admin user and the gateway watchdog's redis-acl healthcheck (which
    reads the identity from the URL as data) had nothing to re-affirm. The
    identity is read from the cluster's own db_url (`identity_from_url` —
    names-as-data for this legacy backfill), falling back to the fixed
    birth identifier only when db_url carries no username either. Safe to write
    mid-flight: the file-only Redis runtime password stays inside the URL, and
    `ava start` re-affirms the ACL user under this same identity
    (ensure_cluster_instance takes it from redis_identity) before daemons dial with
    the new URL.

    The URL is read from the .env FILE, never from settings — the in-memory dial
    value is host-rewritten (loopback self-dial), and persisting that would
    clobber the reachable host. Idempotent: a URL already carrying a username is
    left byte-identical.
    """
    from urllib.parse import urlsplit

    from shared.cluster import DATA_PLANE_IDENTITY, identity_from_url, redis_password_from_env
    from shared.envfile import upsert_env
    from shared.url_secret import url_with_userinfo

    env_path = ctx.ava_home / ".env"
    if not env_path.exists():
        return
    raw = ""
    for line in env_path.read_text().splitlines():
        if line.split("=", 1)[0].strip() == "AVA_REDIS_URL" and "=" in line:
            raw = line.split("=", 1)[1].strip()
            break
    if not raw or urlsplit(raw).username:
        return
    runtime_password = redis_password_from_env() or settings.data_plane.cluster_secret
    if not runtime_password:
        return  # no-secret test/unprovisioned homes: userinfo cannot be minted
    try:
        identity = identity_from_url(settings.data_plane.db_url)
    except ValueError:
        identity = DATA_PLANE_IDENTITY
    upsert_env(
        env_path,
        {"AVA_REDIS_URL": url_with_userinfo(raw, identity, runtime_password)},
        audit_site="converge_redis_identity",
    )
    print(f"  · backfilled AVA_REDIS_URL username {identity!r} (legacy URL carried none)")


# --- unit-state steps (need a configured unit) ----------------------------


def _migrate_host_config_to_env(ctx: ConvergeCtx) -> None:
    """One-time .env hygiene migrations (host-override file, inverted legacy
    AVA_SKIP_* keys, AVA_PRIMARY_GATEWAY_URL rename) — idempotent, file-only."""
    from shared import runtime_config

    runtime_config.migrate_host_json_to_env()
    if changed := runtime_config.migrate_skip_alias_env_keys(ctx.ava_home / ".env"):
        print(f"  · legacy AVA_SKIP_* env keys migrated: {', '.join(changed)}", file=sys.stderr)
    runtime_config.migrate_primary_gateway_url_key(ctx.ava_home / ".env")


def _backfill_health_port_keys_step(ctx: ConvergeCtx) -> None:
    """Backfill missing AVA_*_HEALTH_PORT keys into a block-style unit's .env.

    A unit enrolled before a health daemon joined `_HEALTH_PORT_SERVICES`
    carries no key for it, so that daemon falls back to the LEGACY shared port
    (`daemon_health.health_port`) — and two co-located units on one localhost
    namespace (a Windows unit and its WSL2 sibling) then collide on the same
    shared default (2026-09-02: win/wsl both on 8114). When the unit's present
    health keys prove one block base (see
    `env_registry.backfill_missing_health_ports`), the missing keys are derived
    and written under the env lock, exactly as enroll would have written them.
    A legacy unit's fixed 8102-8111 pins are not offset-consistent, so it is
    never touched; a complete key set is a no-op. Idempotent by construction.
    """
    from shared.env_registry import (
        backfill_missing_health_ports,
        health_port_env_aliases,
    )
    from shared.envfile import upsert_env

    env_path = ctx.ava_home / ".env"
    if not env_path.exists():
        return
    wanted = set(health_port_env_aliases().values())
    existing: dict[str, str] = {}
    for line in env_path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() in wanted:
            existing[key.strip()] = value.strip()
    missing = backfill_missing_health_ports(existing)
    if not missing:
        return
    upsert_env(env_path, missing, audit_site="converge_health_port_backfill")
    print(
        f"  · backfilled {len(missing)} daemon health-port key(s) from this unit's "
        f"port block: {', '.join(f'{k}={v}' for k, v in sorted(missing.items()))}"
    )
