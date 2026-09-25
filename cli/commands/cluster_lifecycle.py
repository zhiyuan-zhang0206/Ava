"""Home-addressed cluster management; initialization belongs only to start."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path


def _repo_root() -> Path:
    # cli/commands/ is two levels below repo root.
    return Path(__file__).resolve().parents[2]


def _subprocess_env(*, gateway_home: Path) -> dict[str, str]:
    """Environment for a cluster subprocess (the `ava stop` child of
    `cmd_cluster_down`).

    Two categories of inherited env are stripped so the child's own
    $AVA_HOME/.env wins, not the parent's already-loaded config:
    - `derived_env_keys()` (AVA_DB_URL / AVA_REDIS_URL / ports / channels) —
      else this cluster's db/redis URLs leak in and `load_dotenv` (override=False)
      would not replace them, silently pointing the child at the wrong database.
    - `env_identity_keys()` (serve flags / name / gateway-url /
      memory-remote) — else the child inherits this host's identity.
    """
    from shared.env_registry import derived_env_keys, env_identity_keys

    stripped = derived_env_keys() | env_identity_keys()
    env = {k: v for k, v in os.environ.items() if k not in stripped}
    env["AVA_HOME"] = str(gateway_home)
    # Acting on a home this checkout does not own is the whole point here — the
    # child runs THIS checkout's `cli.main stop` (cwd=_repo_root(), sys.executable)
    # against ANOTHER cluster's home, which is exactly the shape
    # `resolve_ava_home` refuses. Stopping is home-scoped and reads no code from
    # the target checkout, so the mixing that makes a contradiction dangerous
    # (this checkout's `migrations/` against that cluster's database) cannot
    # happen; say so explicitly rather than letting the guard reject the verb.
    env["AVA_HOME_OVERRIDE"] = "1"
    # No config-source pin needed: AVA_CONFIG_SOURCE is gone (2026-08-01) and the
    # child (`ava stop`) is a settings-lite verb — cli.main opts it out of the
    # gateway fetch, and it reads only this target home's host-scope .env, which
    # is exactly what a teardown needs with the gateway down.
    return env


def cmd_cluster_down(*, path: str) -> int:
    """Stop a cluster's services + its own pg/redis (does not drop data).

    Both a CLI verb (`ava cluster down --path`) and the first step of
    `cmd_cluster_destroy`. It addresses a cluster you are NOT in, by home path —
    to stop the one you are in, use `ava stop`."""
    from shared import cluster as cl

    home = Path(path).expanduser()
    rec = cl.get_record(home)
    if rec is None:
        print(f"✗ ava cluster down: no cluster at '{home}' in the registry", file=sys.stderr)
        return 1

    # The child stop runs with AVA_HOME = the target home, so its data-plane
    # teardown reaches only THAT home's own pg/redis instance — which is exactly
    # what "stop the cluster at this path" means, so no --keep-infra (destroy's
    # --drop-db then removes data dirs of a genuinely stopped instance, never a
    # live one). -y so the non-interactive subprocess does not hang/abort on the
    # stdin confirm. --stop-browser: a cluster-down tears this cluster fully
    # down, so its headed browser session goes too. (The keep-browser default is
    # for in-place stop / update of the cluster you are living in, not for
    # stopping a different one.)
    cmd = [sys.executable, "-m", "cli.main", "stop", "-y", "--stop-browser"]
    # No derived env: the child reads the cluster's connection vars from its own
    # $AVA_HOME/.env (the inherited values are stripped by _subprocess_env).
    env = _subprocess_env(gateway_home=home)
    result = subprocess.run(cmd, cwd=_repo_root(), env=env, check=False)
    return result.returncode


def cmd_cluster_destroy(*, path: str, drop_db: bool = False) -> int:
    """Remove a cluster: stop it, delete its registry entry, optionally remove its
    data dirs.

    Refuses to destroy the default home (`~/.ava`) — it is prod. Returns 0 on
    success, 1 if the path is not registered or is the default home.

    Deliberately leaves the home's own files alone (`.env` included) without
    `--drop-db`: destroy frees the *slot*, and a destroyed home's `.env` is the
    only copy of that cluster's secret, of any key hand-added beyond
    `SEED_ENV_KEYS`, and of the URLs its data-plane identity is read from — so
    deleting it would make "free the port block" discard credentials that exist
    nowhere else (it would not strand the preserved pg data: the role password
    is re-affirmed from the current secret on every bring-up). What stops the leftover home
    from being booted onto a block since reallocated is the start gate
    (`cli/preflight.py`), which refuses a home the registry does not corroborate
    — no record at all, or a record whose port block its `.env` contradicts.
    """
    from shared import cluster as cl

    home = Path(path).expanduser()
    if cl.is_default_home(home):
        print(
            f"✗ ava cluster destroy: refusing to destroy the default home ({cl.default_home()}) "
            "— it is the production cluster; use 'ava stop' to stop it",
            file=sys.stderr,
        )
        return 1

    rec = cl.get_record(home)
    if rec is None:
        print(f"✗ ava cluster destroy: no cluster at '{home}' in the registry", file=sys.stderr)
        return 1

    from services.permissions_helper.launchd_job import unregister_helper
    from shared.platform import file_lock
    from shared.private_storage import write_private_bytes

    # Publish a terminal intent before stopping. Concurrent/internal starts must
    # refuse it even while this home still owns its reservation.
    with file_lock(home / "start-intent.lock", timeout_s=30):
        write_private_bytes(home / "destroy-intent.json", b'{"version":1,"state":"destroying"}\n')
        rc = cmd_cluster_down(path=str(home))
        if rc != 0:
            print(
                "cluster stop incomplete; reservation and destroy intent retained", file=sys.stderr
            )
            return rc
        try:
            _unregister_scheduled_jobs(home)
            unregister_helper(home, helper_port=rec.ports["permissions_helper"])
        except (OSError, RuntimeError) as exc:
            print(f"cluster cleanup incomplete; reservation retained: {exc}", file=sys.stderr)
            return 1
        with cl.registry_lock():
            current = cl.get_record(home)
            if current != rec:
                raise RuntimeError("cluster reservation changed during destroy")
            cl.delete_record_locked(home)
        write_private_bytes(home / "destroy-intent.json", b'{"version":1,"state":"detached"}\n')
        print(f"removed {home} from cluster registry after exact cleanup")

    if drop_db:
        # The whole Postgres+Redis instance is this cluster's own, so removing its
        # data dirs IS the drop — there is no shared server to DROP DATABASE inside.
        # `cmd_cluster_down` already stopped it (above); delete the dirs under the
        # cluster's home, plus the short pg socket dir under /tmp.
        import shutil

        for d in (home / "pg", home / "redis", Path("/tmp") / f"ava-pg-{cl.home_slug(home)}"):  # noqa: S108
            shutil.rmtree(d, ignore_errors=True)
        print(f"✓ removed '{home}' per-cluster data plane (pg + redis data dirs)")

    return 0


def _unregister_scheduled_jobs(home: Path) -> None:
    """Remove every OS-scheduled job the cluster at `home` registered (health
    probe, boot autostart, the systemd boot unit, logs maintenance and packages refresh).

    `home` is passed to each helper as an argument. It cannot be signalled by
    setting `AVA_HOME`: `settings` is constructed once at import, so a mid-process
    mutation of the environment changes nothing, and the helpers would deregister
    THIS process's cluster — running `ava cluster destroy --path <worktree>` from
    the prod checkout (the documented way to address a cluster) would tear down
    prod's own health probe and autostart.

    Every job must be retired before the registry slot can be freed. An
    unavailable scheduler is ambiguous custody, so failures are raised.
    """
    from shared.os_autostart import unregister_autostart
    from shared.os_boot_unit import uninstall as uninstall_boot_unit
    from shared.os_cron import unregister_os_cron
    from shared.os_logs_job import unregister_logs_job
    from shared.os_packages import unregister_packages_job

    def uninstall_boot_unit_job() -> None:
        # The steps it returns belong to the boot-unit CLI verbs; destroy
        # reports only success/failure per job.
        uninstall_boot_unit(home)

    jobs: list[tuple[str, Callable[[], None]]] = [
        ("health probe", lambda: unregister_os_cron(home)),
        ("autostart", lambda: unregister_autostart(home)),
        ("boot unit", uninstall_boot_unit_job),
        ("logs maintenance", lambda: unregister_logs_job(home)),
        ("packages refresh", lambda: unregister_packages_job(home)),
    ]
    failed: list[str] = []
    for name, unregister in jobs:
        try:
            unregister()
        except Exception as e:
            failed.append(f"{name} ({e})")

    if failed:
        raise RuntimeError("could not remove scheduled jobs: " + ", ".join(failed))


def cmd_cluster_ls() -> int:
    """List all registered clusters (label = home basename, computed display)."""
    from shared import cluster as cl

    registry = cl.load_registry()
    if not registry:
        print("(no clusters registered)")
        return 0

    for rec in registry.values():
        # ports always holds the full PORT_OFFSETS set by contract — index, don't .get.
        print(
            f"{cl.home_label(Path(rec.gateway_home))}"
            f"  gateway={rec.ports['gateway']}  frontend={rec.ports['frontend']}"
            f"  pg={rec.ports['postgres']}  redis={rec.ports['redis']}"
            f"  home={rec.gateway_home}"
        )
    return 0
