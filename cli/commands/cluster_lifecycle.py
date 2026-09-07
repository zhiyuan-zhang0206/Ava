"""Cluster lifecycle helpers — registry allocation + `ava cluster ls/down/destroy`.

Identity is the home path (path-only): a cluster is born by
`scripts/install.sh` -> `python -m cli.install_cluster` (which calls
`_ensure_record` / the data-plane bring-up / provision), and `ava start` is a
pure bring-up — the settings-free `cli.preflight.require_installed_home` gate
(run by `cli.main` before any settings-loading import) fail-fasts an
uninstalled home with a role-appropriate pointer instead of birthing anything.
The management verbs address a cluster by its home path (`--path`), never a name.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from shared.cluster import ClusterPorts
from shared.config import settings


def _repo_root() -> Path:
    # cli/commands/ is two levels below repo root.
    return Path(__file__).resolve().parents[2]


def _provision(identity: str, *, base_admin_url: str, db_admin_password: str) -> bool:
    """Thin wrapper around cluster.provision_database for monkeypatching in tests."""
    from shared import cluster as cl

    return cl.provision_database(
        identity, base_admin_url=base_admin_url, db_admin_password=db_admin_password
    )


def _ensure_pgvector_extension(identity: str, *, base_admin_url: str) -> None:
    """Thin wrapper around cluster.ensure_pgvector_extension for monkeypatching
    in tests (same seam as `_provision` — birth-side provisioning steps are
    stubbed together). A remote-managed plane is skipped here (no local admin
    socket; its extension provisioning belongs to its owner) — the same guard
    the `ava start` call site carries."""
    from shared import cluster as cl

    if settings.data_plane.is_remote:
        return
    cl.ensure_pgvector_extension(identity, base_admin_url=base_admin_url)


def _ensure_cluster_instance(
    rec: Any,
    cluster_secret: str,
    identity: str,
    runner_password: str | None = None,
    *,
    db_admin_password: str = "",
    redis_admin_password: str = "",
    redis_password: str = "",
) -> int:
    """Thin wrapper around the per-cluster Postgres+Redis bring-up (for
    monkeypatching in tests, like `_provision`). `identity` is the data-plane
    db/role/ACL identifier, names-as-data (see ensure_cluster_instance).
    `runner_password` (the gateway .env AVA_RUNNER_DB_PASSWORD) is threaded at
    install birth, when the .env does not exist yet; a later bring-up resolves
    it from the file itself."""
    from cli.commands._cluster_instance import ensure_cluster_instance
    from shared.cluster import record_pgbouncer_port

    return ensure_cluster_instance(
        pg_port=rec.ports["postgres"],
        redis_port=rec.ports["redis"],
        cluster_secret=cluster_secret,
        db_admin_password=db_admin_password or cluster_secret,
        redis_admin_password=redis_admin_password or cluster_secret,
        redis_password=redis_password or cluster_secret,
        pgbouncer_port=record_pgbouncer_port(rec),
        identity=identity,
        redis_user=identity,
        runner_password=runner_password,
    )


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


def _ensure_record(home: Path) -> tuple[Any, bool]:
    """Read-allocate-save the cluster's registry record under the host registry
    lock, so concurrent births serialize and never claim the same port block.
    Keyed by the home path — the cluster's identity.

    Returns `(record, created)`. `created` is False when the record already
    existed (returned unchanged) and True when this call allocated it — the caller
    uses it to roll the registration back if a later provisioning step fails,
    rather than leaking an orphan port-block reservation."""
    from datetime import UTC, datetime

    from shared import cluster as cl

    with cl.registry_lock():
        rec = cl.get_record(home)
        if rec is not None:
            return rec, False
        if cl.is_default_home(home):
            # The default home (prod ~/.ava) uses fixed historical ports (incl. its
            # own pg/redis on 5433/6380). LEGACY_AVA_PORTS moved to the
            # dependency-free port_block module as a plain dict — it IS the legacy
            # ClusterPorts shape.
            ports = cast("ClusterPorts", cl.LEGACY_AVA_PORTS)
        else:
            registry = cl.load_registry()
            # Allocate a fresh port block not already claimed by non-default clusters.
            existing_bases: set[int] = {
                min(cast("dict[str, int]", r.ports).values())
                for r in registry.values()
                if not cl.is_default_home(Path(r.gateway_home))
            }
            ports = cl.allocate_ports(existing_bases)

        rec = cl.ClusterRecord(
            ports=ports,
            gateway_home=str(Path(home).expanduser()),
            created_at=datetime.now(UTC).isoformat(),
            # The derived-URL host for this cluster's data plane (empty =
            # loopback, the single-box posture). Read from the settings knob
            # AVA_DATA_PLANE_HOST at birth — a host-scope input that survives
            # the env-authority pass on a not-yet-born home — and snapshotted
            # on the record because derive_env runs at birth, before the
            # home's .env exists (external data plane: Task #1752).
            data_plane_host=(settings.data_plane.data_plane_host or "").strip(),
        )
        cl.save_record_locked(rec)  # lock already held — see ensure_registered
        return rec, True


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

    # Stop the cluster first — the child stop runs with AVA_HOME = this home, so
    # it takes down that home's services AND its own pg/redis (the instance must
    # be down before --drop-db removes its data dirs).
    rc = cmd_cluster_down(path=str(home))
    if rc != 0:
        # Non-fatal: the stop failed (e.g. sessions already gone), but we proceed
        # to free the registry entry so the port block is returned.
        print(
            f"  ⚠ ava cluster down returned rc={rc}; proceeding with registry cleanup",
            file=sys.stderr,
        )

    # Free the registry entry under the host lock (delete_record is now
    # self-serializing; the locked variant keeps this critical section whole).
    with cl.registry_lock():
        deleted = cl.delete_record_locked(home)

    if not deleted:
        # Another process beat us; nothing to clean up.
        print(f"  · registry entry for '{home}' was already absent", file=sys.stderr)
    else:
        print(f"✓ removed '{home}' from cluster registry (port block freed)")

    # Deregister this cluster's OS-scheduled jobs. Freeing the registry slot
    # without this leaves launchd / crontab entries pointing at a home that no
    # longer has a cluster — for a dev worktree they also point at a checkout
    # about to be deleted, so they fail every interval, forever.
    _unregister_scheduled_jobs(home)

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
    probe, both capabilities' watchdog probes, boot autostart, logs maintenance).

    `home` is passed to each helper as an argument. It cannot be signalled by
    setting `AVA_HOME`: `settings` is constructed once at import, so a mid-process
    mutation of the environment changes nothing, and the helpers would deregister
    THIS process's cluster — running `ava cluster destroy --path <worktree>` from
    the prod checkout (the documented way to address a cluster) would tear down
    prod's own health probe, watchdog probes and autostart.

    Failures are reported, not raised: a half-registered cluster, or a host whose
    scheduler is unavailable, must still be destroyable — the registry slot is
    already freed by the time this runs.
    """
    from cli.commands._converge_gate import unregister_gate
    from shared.os_autostart import unregister_autostart
    from shared.os_cron import unregister_os_cron
    from shared.os_logs_job import unregister_logs_job
    from shared.os_watchdog_probe import unregister_watchdog_probe

    jobs: list[tuple[str, Callable[[], None]]] = [
        ("health probe", lambda: unregister_os_cron(home)),
        ("autostart", lambda: unregister_autostart(home)),
        ("logs maintenance", lambda: unregister_logs_job(home)),
        ("watchdog probe (gateway)", lambda: unregister_watchdog_probe("gateway", home)),
        ("watchdog probe (agent-runner)", lambda: unregister_watchdog_probe("agent-runner", home)),
        ("fleet UI gate", lambda: unregister_gate(home)),
    ]
    failed: list[str] = []
    for name, unregister in jobs:
        try:
            unregister()
        except Exception as e:
            failed.append(f"{name} ({e})")

    if failed:
        print(
            f"  ⚠ could not deregister some of '{home}' OS-scheduled jobs: {', '.join(failed)}",
            file=sys.stderr,
        )
    else:
        print(
            f"✓ removed '{home}' OS-scheduled jobs "
            "(health probe, watchdog probes, autostart, logs maintenance)"
        )


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
