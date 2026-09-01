"""`ava cluster ensure-db-role` — provision the `ava_runner` Postgres role on a live cluster.

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


def cmd_ensure_db_role() -> int:
    """Ensure the `ava_runner` Postgres role + grants + credential on this cluster.

    Returns 0 on success; 1 when the home is not a gateway home, the registry
    record is missing, or Postgres is not reachable (the role lives in pg, so a
    stopped cluster must be started first).
    """
    from cli.commands._cluster_instance import _pg_running, pg_admin_url
    from cli.commands._pgbouncer import _running_pid, ensure_pgbouncer
    from shared import cluster as cl
    from shared.config import settings
    from shared.envfile import upsert_env
    from shared.paths import ava_home

    if settings.data_plane.is_remote:
        print(
            "✗ this cluster's data plane is remote-managed — the runner role is "
            "provisioned at the provider (SaaS) or on the host that owns the "
            "instance; nothing to provision here.",
            file=sys.stderr,
        )
        return 1

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
    db_admin_password = (env.get("AVA_DB_ADMIN_PASSWORD") or cluster_secret).strip()
    runner_password = (env.get(cl.RUNNER_DB_PASSWORD_ENV) or "").strip()
    if not runner_password:
        runner_password = secrets.token_urlsafe(32)
        print(f"  · minted a fresh {cl.RUNNER_DB_PASSWORD_ENV} (none was in .env)")
    base_admin_url = pg_admin_url(rec.ports["postgres"])

    cl.ensure_checkpoint_schema(
        identity, base_admin_url=base_admin_url, db_admin_password=db_admin_password
    )
    cl.ensure_runner_role(
        identity,
        base_admin_url=base_admin_url,
        runner_password=runner_password,
    )
    upsert_env(
        env_path,
        {cl.RUNNER_DB_PASSWORD_ENV: runner_password},
        audit_site="ensure_runner_db_role",
    )
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
                db_admin_password=db_admin_password,
                runner_password=runner_password,
            )
            print("  ✓ pooler userlist refreshed (ava_runner entry live now)")
        else:
            print(
                "  · pooler not running — the next `ava start` writes the ava_runner "
                "entry into its userlist"
            )
    return 0


def refresh_runner_grants_after_migration() -> None:
    """Re-affirm `ava_runner`'s grants because a migration just changed the schema.

    A migration that CREATES a table leaves the runner unable to read it:
    `GRANT SELECT ON ALL TABLES` is a point-in-time loop over what existed at
    install birth, and nothing re-runs it on a schedule (the standing
    `ALTER DEFAULT PRIVILEGES` beside it covers tables created after IT was
    declared, which is no help to a cluster born before this shipped). So the
    moment the schema grows is the moment the read surface has to be re-affirmed
    — otherwise a pure agent-runner, which dials as `ava_runner`, sees
    `permission denied` on the new table for the rest of the cluster's life.

    Deliberately narrower than `cmd_ensure_db_role`, which is the operator
    door and may CREATE the role, mint a password and rewrite the pooler
    userlist. This is a start-path step, so it only ever re-affirms what a
    cluster already adopted, and quietly does nothing otherwise:

    - not a gateway home (no `AVA_DB_URL` in `.env`) — a runner has no admin
      credential and no business touching roles;
    - no `AVA_RUNNER_DB_PASSWORD` — the cluster predates the runner-role cutover
      and never provisioned one. Minting it here would silently adopt the whole
      role on somebody's next start; `ava cluster ensure-db-role` is the
      deliberate door for that.

    Reports and continues on failure. Being behind on grants is recoverable and
    self-heals on the next migration; a start that already brought the data
    plane up should not abort over it.
    """
    import sys

    from dotenv import dotenv_values

    from cli.commands._cluster_instance import pg_admin_url
    from shared import cluster as cl
    from shared.config import settings
    from shared.paths import ava_home

    if settings.data_plane.is_remote:
        # A remote/SaaS plane provisions its own roles — there is no local
        # runner-role grant to refresh (Task #1752).
        print("  · runner-role grant refresh skipped (remote-managed data plane)")
        return
    env = dotenv_values(ava_home() / ".env")
    db_url = (env.get("AVA_DB_URL") or "").strip()
    runner_password = (env.get(cl.RUNNER_DB_PASSWORD_ENV) or "").strip()
    if not db_url or not runner_password:
        return
    rec = cl.get_record(ava_home())
    if rec is None:
        return
    try:
        cl.ensure_runner_role(
            cl.identity_from_url(db_url),
            base_admin_url=pg_admin_url(rec.ports["postgres"]),
            runner_password=runner_password,
        )
    except Exception as exc:  # see the docstring: report, never abort the start
        print(
            f"  ! could not re-affirm {cl.RUNNER_ROLE} grants after the migration "
            f"({exc}); run `ava cluster ensure-db-role` — until then this "
            "cluster's agent-runners cannot read tables the migration added",
            file=sys.stderr,
        )
        return
    print(f"  ✓ re-affirmed {cl.RUNNER_ROLE} read grants over the new schema")
