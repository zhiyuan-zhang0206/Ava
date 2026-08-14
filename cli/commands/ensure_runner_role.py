"""`ava cluster ensure-runner-role` — provision the ava_runner role on a live cluster.

The one-shot counterpart of install-time provisioning (Task #1236): a cluster
born before the runner-role design gets the SAME idempotent SQL — the
least-privilege `ava_runner` role + its table grants — plus the
`AVA_RUNNER_DB_PASSWORD` credential written into the gateway's `.env`, and a
refreshed pooler userlist when the pooler is running (so enrolled runners can
authenticate without waiting for the next `ava start`).

Runs against THIS checkout's home (the anchored-home gate in cli.main); the
cluster's Postgres must be up. Idempotent — safe to re-run after a manual
`.env` password edit (the role password is re-affirmed from the file value).
"""

from __future__ import annotations

import secrets
import sys

from dotenv import dotenv_values


def cmd_ensure_runner_role() -> int:
    """Ensure the ava_runner role + grants + credential on this cluster.

    Returns 0 on success; 1 when the home is not a gateway home, the registry
    record is missing, or Postgres is not reachable (the role lives in pg, so a
    stopped cluster must be started first).
    """
    from cli.commands._cluster_instance import _pg_running, pg_admin_url
    from cli.commands._pgbouncer import _running_pid, ensure_pgbouncer
    from shared import cluster as cl
    from shared.envfile import upsert_env
    from shared.paths import ava_home

    home = ava_home()
    env_path = home / ".env"
    env = dotenv_values(env_path)
    db_url = (env.get("AVA_DB_URL") or "").strip()
    if not db_url:
        print(
            f"✗ {env_path} carries no AVA_DB_URL — this home is not a gateway "
            "install (the runner role lives in the gateway's Postgres).",
            file=sys.stderr,
        )
        return 1
    rec = cl.get_record(home)
    if rec is None:
        print(
            f"✗ no registry record for home {home} — cannot resolve its Postgres "
            "port. Install births it (`scripts/install.sh`).",
            file=sys.stderr,
        )
        return 1
    if not _pg_running(rec.ports["postgres"]):
        print(
            f"✗ this cluster's Postgres is not running (127.0.0.1:{rec.ports['postgres']}) — "
            "the runner role lives in pg, so start the cluster first (`ava start`), then "
            "re-run this command.",
            file=sys.stderr,
        )
        return 1

    identity = cl.identity_from_url(db_url)
    cluster_secret = (env.get("AVA_CLUSTER_SECRET") or "").strip()
    runner_password = (env.get(cl.RUNNER_DB_PASSWORD_ENV) or "").strip()
    if not runner_password:
        runner_password = secrets.token_urlsafe(32)
        print(f"  · minted a fresh {cl.RUNNER_DB_PASSWORD_ENV} (none was in .env)")
    base_admin_url = pg_admin_url(rec.ports["postgres"])

    cl.ensure_checkpoint_schema(
        identity, base_admin_url=base_admin_url, cluster_secret=cluster_secret
    )
    cl.ensure_runner_role(
        identity,
        base_admin_url=base_admin_url,
        runner_password=runner_password,
    )
    upsert_env(env_path, {cl.RUNNER_DB_PASSWORD_ENV: runner_password})
    print(
        f"✓ ensured role {cl.RUNNER_ROLE} (LOGIN NOSUPERUSER) + table grants on "
        f"db {identity!r}; password persisted to {env_path}"
    )

    enabled = (env.get("AVA_PGBOUNCER_ENABLED") or "true").strip().lower()
    if enabled in ("1", "true", "yes", "on"):
        if _running_pid() is not None:
            ensure_pgbouncer(
                pg_port=rec.ports["postgres"],
                listen_port=cl.record_pgbouncer_port(rec),
                db_name=identity,
                role=identity,
                cluster_secret=cluster_secret,
                runner_password=runner_password,
            )
            print("  ✓ pooler userlist refreshed (ava_runner entry live now)")
        else:
            print(
                "  · pooler not running — the next `ava start` writes the ava_runner "
                "entry into its userlist"
            )
    return 0
