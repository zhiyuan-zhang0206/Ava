"""Decommission of the host's cluster; initialization belongs only to start."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path


def _repo_root() -> Path:
    # cli/commands/ is two levels below repo root.
    return Path(__file__).resolve().parents[2]


def _drop_dirs(home: Path) -> list[Path]:
    """What `--drop-db` deletes: the cluster's own data directories and the short
    Postgres socket directory under /tmp. One list for the prompt and the removal,
    so the prompt names exactly what goes."""
    from base.db.pg_admin import pg_socket_path

    return [home / "pg", home / "redis", pg_socket_path(home)]


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _confirm_destroy(home: Path, *, roles: list[str], drop_db: bool) -> bool:
    """Ask a person at a terminal to type the home path; True only when they did.

    Destroy is irreversible, so the confirmation is the point: it needs stdin and
    stdout to be terminals and offers no flag that skips it. Anything else (no
    terminal, end of input, a different string) does nothing.
    """
    if not _interactive():
        print(
            "✗ ava cluster destroy: refusing without an interactive terminal "
            "(over ssh, use `ssh -t`); there is no flag that skips the confirmation",
            file=sys.stderr,
        )
        return False
    role_text = ", ".join(roles) if roles else "no roles recorded"
    print(f"ava cluster destroy will decommission the cluster at {home} ({role_text}):")
    print("  - stop its services and its own Postgres and Redis")
    print("  - retire this host's OS jobs (launchd, crontab, boot unit) and the permissions helper")
    print("  - mark the home detached: `ava start` refuses it until destroy-intent.json is deleted")
    if "gateway" in roles:
        print("  - this host serves the gateway: every agent-runner loses its control plane")
    if drop_db:
        print("  - DELETE the cluster's database and cache, which cannot be undone:")
        for directory in _drop_dirs(home):
            print(f"      {directory}")
    try:
        answer = input("Type the home path to confirm: ")
    except EOFError:
        answer = ""
    if answer.strip() != str(home):
        print("aborted: nothing was changed", file=sys.stderr)
        return False
    return True


def _stop_cluster() -> int:
    """Stop this cluster's services and its own pg/redis (does not drop data).

    The child is `ava stop`, run from this checkout with the same home. No
    `--keep-infra`: `--drop-db` then removes the data directories of a genuinely
    stopped instance, never a live one. `-y` because the child has no terminal to
    confirm on (destroy confirmed already); a plain stop takes the headed
    browser down too."""
    cmd = [sys.executable, "-m", "cli.main", "stop", "-y"]
    return subprocess.run(cmd, cwd=_repo_root(), check=False).returncode


def cmd_cluster_destroy(*, drop_db: bool = False) -> int:
    """Detach the host's cluster: stop it, retire its OS jobs and helper, mark the
    home detached, optionally remove its data dirs.

    Acts on this process's home (`AVA_HOME`, else `~/.ava`), the default
    production home included, after `_confirm_destroy`. Returns 0 on success, 1
    when the home holds no start intent, the confirmation is not given, or a step
    fails.

    Without `--drop-db` the home's own files stay (`.env` included): a detached
    home's `.env` is the only copy of that cluster's secret, of any key hand-added
    beyond `SEED_ENV_KEYS`, and of the URLs its data-plane identity is read from.
    The `detached` destroy intent is what stops the leftover home from starting
    again (`cli.start_identity.prepare_identity` refuses it); deleting that file by
    hand is the only way back.
    """
    from base.paths import ava_home
    from cli.start_identity import read_intent

    home = ava_home().resolve()
    intent = read_intent(home)
    if intent is None:
        print(f"✗ ava cluster destroy: no start intent at '{home}'", file=sys.stderr)
        return 1
    if not _confirm_destroy(home, roles=list(intent["roles"]), drop_db=drop_db):
        return 1

    from base.config import settings
    from base.host.private_storage import write_private_bytes
    from base.native_process.os_platform import file_lock
    from services.desktop.permissions_helper.launchd_job import unregister_helper

    # Publish a terminal intent before stopping. Concurrent/internal starts must
    # refuse it from the first moment of the teardown.
    with file_lock(home / "start-intent.lock", timeout_s=30):
        write_private_bytes(home / "destroy-intent.json", b'{"version":1,"state":"destroying"}\n')
        rc = _stop_cluster()
        if rc != 0:
            print("cluster stop incomplete; destroy intent retained", file=sys.stderr)
            return rc
        try:
            _unregister_scheduled_jobs()
            unregister_helper(home, helper_port=settings.services.permissions_helper_port)
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            print(f"cluster cleanup incomplete: {exc}", file=sys.stderr)
            return 1
        write_private_bytes(home / "destroy-intent.json", b'{"version":1,"state":"detached"}\n')
        print(f"detached {home} after exact cleanup")

    if drop_db:
        # The whole Postgres+Redis instance is this cluster's own, so removing its
        # data dirs IS the drop — there is no shared server to DROP DATABASE inside.
        # `_stop_cluster` already stopped it (above).
        import shutil

        for directory in _drop_dirs(home):
            shutil.rmtree(directory, ignore_errors=True)
        print(f"✓ removed '{home}' per-cluster data plane (pg + redis data dirs)")

    return 0


def _unregister_scheduled_jobs() -> None:
    """Remove every OS-scheduled job the host registered (health probe, boot
    autostart, logs maintenance, packages refresh, PR flow and the WAL-G tick).

    Labels name jobs, not homes, and each `unregister_*` is a no-op outside the
    default home, so a scratch home's destroy never touches the host's jobs.

    Every job must be retired before the home is marked detached. An
    unavailable scheduler is ambiguous custody, so failures are raised.
    """
    from base.host.system.autostart import unregister_autostart
    from base.host.system.cron import unregister_os_cron
    from base.host.system.logs_job import unregister_logs_job
    from base.host.system.packages_job import unregister_packages_job
    from base.host.system.pr_flow_job import unregister_pr_flow_job
    from base.host.system.walg_job import unregister_walg_job

    jobs: list[tuple[str, Callable[[], None]]] = [
        ("health probe", unregister_os_cron),
        ("autostart", unregister_autostart),
        ("logs maintenance", unregister_logs_job),
        ("packages refresh", unregister_packages_job),
        ("PR flow", unregister_pr_flow_job),
        ("WAL-G tick", unregister_walg_job),
    ]
    failed: list[str] = []
    for name, unregister in jobs:
        try:
            unregister()
        except Exception as e:
            failed.append(f"{name} ({e})")

    if failed:
        raise RuntimeError("could not remove scheduled jobs: " + ", ".join(failed))
