"""Per-cluster PgBouncer healthcheck — called every 60s by the gateway watchdog.

PgBouncer is the third per-cluster data-plane process (`cli/commands/_pgbouncer.py`),
and when it is enabled `AVA_DB_URL` points AT it — so every gateway daemon and every
agent reaches Postgres only through the pooler. It was the one data-plane process with
no probe, no alert and no self-heal: the gateway, the frontend and each agent-runner
all have healthchecks, Postgres and Redis are re-affirmed by `ava start` and the
redis-acl check, but a pooler that died stayed dead until a human noticed. On
2026-08-06 one was killed by mistake and the cluster ran without a database front door
for 3.5 minutes with nothing reporting it.

The check probes the pooler's ADMIN console (`pgbouncer_listener_reachable`), not the
end-to-end `SELECT 1` that `ava status` uses. The distinction is the whole design: an
end-to-end probe also fails when Postgres is down, and "restart the pooler" is the
wrong answer to that — it would restart a healthy process every round for the duration
of a Postgres outage and report a repair that repaired nothing. The admin console
proves the listener is up and client scram works while opening no backend connection,
so this check answers exactly "is the pooler process there", and leaves the backend to
the checks that own it.

Repair is `ensure_pgbouncer` — the same idempotent bring-up `ava start` runs, not a
second implementation: it rewrites the ini + userlist, SIGHUPs a live pooler and
starts a dead one. A cluster with `AVA_PGBOUNCER_ENABLED` false has no pooler by
design and is skipped.

Gateway-capability only: the pooler is this cluster's own process on loopback, beside
the Postgres it fronts.
"""

import logging

from shared.cluster import db_identity, get_record, record_pgbouncer_port, record_postgres_port
from shared.config import settings
from shared.log import init_gateway_process
from shared.paths import ava_home

_log = logging.getLogger("services.healthchecks.pgbouncer")


def check(*, listen_port: int, pg_port: int, identity: str, cluster_secret: str) -> None:
    """Probe the pooler's listener; restart it when it is gone, and verify.

    Ports and identity are passed in rather than read here — they are registry
    facts (`record_pgbouncer_port`) and names-as-data (`db_identity`), and the
    caller is the one that already resolved them.

    Raises when the repair did not take, so the watchdog logs a failing
    healthcheck rather than this module deciding what to swallow. A repair that
    keeps failing is a real "the pooler cannot start here" and belongs on the
    operator's screen every round.
    """
    from cli.commands._pgbouncer import ensure_pgbouncer, pgbouncer_listener_reachable

    if pgbouncer_listener_reachable(listen_port, identity, cluster_secret):
        _log.debug("[pgbouncer healthcheck] pooler answering on :%d, no-op", listen_port)
        return

    _log.warning(
        "[pgbouncer healthcheck] pooler not answering on :%d — every consumer's "
        "AVA_DB_URL points here; restarting",
        listen_port,
    )
    rc = ensure_pgbouncer(
        pg_port=pg_port,
        listen_port=listen_port,
        db_name=identity,
        role=identity,
        cluster_secret=cluster_secret,
    )
    if rc != 0 or not pgbouncer_listener_reachable(listen_port, identity, cluster_secret):
        raise RuntimeError(
            f"pgbouncer restart did not bring the pooler back on :{listen_port} "
            f"(ensure_pgbouncer rc={rc}); the cluster has no pooled database front door"
        )

    _log.info("[pgbouncer healthcheck] pooler restarted and answering on :%d", listen_port)
    _emit_repaired(listen_port)


def _emit_repaired(listen_port: int) -> None:
    """Record the repair on the event stream.

    Emitted only AFTER the pooler is verified back, never before: the telemetry
    writer reaches Postgres through this very pooler, so an event announcing the
    outage would be enqueued into the thing that is down. Announcing the repair
    is both the useful signal (a pooler that gets repaired every round is a
    different incident from one repaired once) and the one this path can
    actually write.
    """
    from shared import telemetry

    telemetry.emit(
        "telemetry",
        "pgbouncer_repaired",
        level="warning",
        source="system",
        attributes={"listen_port": listen_port},
    )


def main() -> None:
    init_gateway_process(name="pgbouncer-healthcheck")
    if not settings.data_plane.pgbouncer_enabled:
        # The documented kill-switch, not an anomaly: consumers talk to Postgres
        # directly and there is no pooler to keep alive. Debug, because a
        # supported configuration must not print a line every 60s.
        _log.debug("[pgbouncer healthcheck] pooler disabled for this cluster, skipping")
        return
    rec = get_record(ava_home())
    if rec is None:
        # The listen port is a registry fact only (it is not materialized in
        # .env — AVA_DB_URL carries it), so without a record there is no port to
        # probe. Warn rather than guess: probing the wrong port would report a
        # healthy pooler dead and restart it every round.
        _log.warning(
            "[pgbouncer healthcheck] no registry record for %s — cannot resolve the "
            "pooler port; skipping",
            ava_home(),
        )
        return
    try:
        identity = db_identity()
    except ValueError:
        # A db_url with no username: the pooled front door's scram identity is
        # unknowable, so the probe cannot authenticate. Same shape as the
        # redis-acl check's username-less skip — `ava converge` backfills it.
        _log.warning(
            "[pgbouncer healthcheck] db_url carries no identity — cannot authenticate "
            "against the pooler; skipping. Run `ava converge` to backfill it."
        )
        return
    check(
        listen_port=record_pgbouncer_port(rec),
        pg_port=record_postgres_port(rec),
        identity=identity,
        cluster_secret=settings.data_plane.cluster_secret,
    )


if __name__ == "__main__":
    # Standalone (cron / by hand): the raise leaves this process with a non-zero
    # exit, the contract every healthcheck module shares. Under the watchdog
    # `main()` is called in-process and the raise is what it catches and logs.
    main()
