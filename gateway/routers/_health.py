"""Gateway health endpoint kept separate from the broader status router."""

from __future__ import annotations

import json
import time

import psycopg
from fastapi import Request
from fastapi.responses import JSONResponse
from psycopg_pool import PoolTimeout

from shared import process_sha
from shared.health_schema import DEGRADED, OK, component, render
from shared.machine import machine_name
from shared.paths import ava_home

_STARTED_AT = time.time()


def get_health(request: Request) -> dict[str, object] | JSONResponse:
    """Liveness probe over the private network (the gateway is unauthenticated). DB ping
    included — a gateway that can serve HTTP but cannot reach Postgres is not
    "healthy" enough for the watchdog to leave alone.

    The identity fields answer the questions a 200 alone cannot: `name` is
    which *service* answered, `home` this gateway's `$AVA_HOME` (its cluster
    identity), `machine` its host, `started_at` this process's birth, and `sha`
    the code frozen at boot. A 200 on this port only proves *something* listens
    there, so the watchdog verifies the stable identity too rather than the status
    code alone — see `services/healthchecks/gateway.py`. The start and code facts
    also let read-only post-deploy checks detect a new serving generation without
    consulting disk state. Each field was additive to the `{"status": "ok"}` contract.

    Liveness must fail faster than the resource it protects: this uses the
    control-plane pool and a 1-2 second database budget rather than waiting up
    to 30 seconds for the data-plane pool. A 503 therefore means the process is
    alive but Postgres is degraded; the watchdog's consecutive-failure policy
    decides whether that merits a respawn.

    `name` is a constant naming the service this route belongs to — the point being
    that an impostor answering here reports its own name, or none. No probe reads it
    yet; `shared.daemon_health._probe_home` gains the `name` arm only once every
    deployed gateway emits the field, and that ordering is load-bearing (#1038)."""
    identity: dict[str, object] = {
        "status": "ok",
        "name": "gateway",
        "home": str(ava_home()),
        "machine": machine_name(),
        "started_at": _STARTED_AT,
        "sha": process_sha.get(),
        "liveness": OK,
    }
    components = [component("http", OK, progress="serving")]
    try:
        with request.app.state.control_db_pool.connection() as conn, conn.cursor() as cur:
            # PgBouncer drops `options` startup parameters, so the per-request
            # liveness budget must be set inside this borrowed transaction.
            cur.execute("SET LOCAL statement_timeout = '2000'")
            cur.execute("SELECT 1")
    except (PoolTimeout, psycopg.Error) as exc:
        identity["status"] = DEGRADED
        components.append(component("db", DEGRADED, detail=f"{type(exc).__name__}: {exc}"))
        status, body = render(identity, components)
        return JSONResponse(
            status_code=status,
            content=json.loads(body),
            headers={"Retry-After": "1"},
        )
    components.append(component("db", OK))
    _status, body = render(identity, components)
    return json.loads(body)
