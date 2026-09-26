"""Close only completed proof agents, then compare their retained durable state."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg import sql

from scripts.preview import local
from scripts.preview.linux_observer import _require_private_endpoint
from scripts.preview.runtime import require_success


def _connection(run: Path) -> psycopg.Connection[Any]:
    from shared.config import settings

    config = json.loads((run / "config.json").read_text())
    _require_private_endpoint(settings.data_plane.db_url, config["ports"]["pgbouncer"])
    connection = psycopg.connect(settings.data_plane.db_url, connect_timeout=5)
    connection.read_only = True
    connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    connection.execute("SET LOCAL statement_timeout = '5s'")
    return connection


def state(run: Path, agent: int) -> dict[str, Any]:
    """Hash closed identity/configuration and all retained checkpoint rows."""
    with _connection(run) as connection:
        row = connection.execute(
            "SELECT a.id, a.created_at, m.machine, m.born_spawner, m.birth_config, "
            "m.config_overlay, m.status, m.closed_at FROM agents a "
            "JOIN agents_meta m ON m.id=a.id WHERE a.id=%s",
            (agent,),
        ).fetchone()
        if row is None or row[-2] != "terminated" or row[-1] is None:
            raise RuntimeError("proof agent has not completed durable final termination")
        content: dict[str, Any] = {"identity": list(row)}
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            content[table] = sorted(
                value[0]
                for value in connection.execute(
                    sql.SQL("SELECT to_jsonb(t)::text FROM {} t WHERE thread_id=%s").format(
                        sql.Identifier(table)
                    ),
                    (str(agent),),
                )
            )
        if not content["checkpoints"]:
            raise RuntimeError("completed proof agent has no retained checkpoint")
    return {
        "agent": agent,
        "sha256": hashlib.sha256(
            json.dumps(content, sort_keys=True, default=str).encode()
        ).hexdigest(),
        "rows": {
            name: len(content[name])
            for name in ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
        },
    }


def freeze(run: Path, label: str) -> None:
    from cli.commands._maintenance_stop import require_no_terminals

    receipt = run / f"release-frozen-{label}.json"
    if receipt.exists():
        raise RuntimeError("completed-work evidence already exists; termination is not retried")
    smoke = json.loads((run / f"smoke-release-{label}.json").read_text())
    agent = int(smoke["agent"])
    config = json.loads((run / "config.json").read_text())
    _require_private_endpoint(config["gateway_url"], config["ports"]["gateway"])
    evidence: dict[str, Any] = {"agent": agent, "result": "running", "started_at": time.time()}
    local.write_json(receipt, evidence)
    try:
        response = httpx.post(
            f"{config['gateway_url']}/api/agents/{agent}/terminate",
            json={"force": False, "final": True},
            timeout=90,
        )
        require_success(response)
        evidence["accepted"] = response.json()
        local.write_json(receipt, evidence)
        deadline = time.monotonic() + 90
        while True:
            try:
                observed = state(run, agent)
                require_no_terminals()
                break
            except RuntimeError as exc:
                evidence["pending"] = str(exc)
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "completed smoke agent lacks durable/native closure"
                    ) from exc
                time.sleep(0.25)
        evidence.update(result="passed", state=observed)
    except BaseException as exc:
        evidence.update(result="failed", error=repr(exc))
        raise
    finally:
        evidence["finished_at"] = time.time()
        local.write_json(receipt, evidence)


def verify(run: Path, label: str) -> None:
    observed: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {"agents": observed, "result": "running"}
    try:
        _compare_frozen(run, observed)
        evidence["result"] = "passed"
    except BaseException as exc:
        evidence.update(result="failed", error=repr(exc))
        raise
    finally:
        local.write_json(run / f"release-state-{label}.json", evidence)


def _compare_frozen(run: Path, observed: list[dict[str, Any]]) -> None:
    for path in sorted(run.glob("release-frozen-*.json")):
        previous = json.loads(path.read_text())
        if previous["result"] != "passed":
            raise RuntimeError("a prior completed-work fixture failed closure")
        current = state(run, previous["agent"])
        observed.append(current)
        if current != previous["state"]:
            raise RuntimeError(f"retained agent identity/checkpoints changed: {current['agent']}")
    if not observed:
        raise RuntimeError("cycle has no captured completed-work fixture")
