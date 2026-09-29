"""Write-generation evidence for the preview's release A/B/A cycle.

Run from the preview's source checkout with its private home and registry
environment, as separate processes of the stdlib controller:

- ``capture --label L``: retain the active generation's two logins (0600, in
  the preview's private run directory) before the transition fences them.
- ``probe --label L``: start a stale writer outside root custody: it holds an
  open transaction as the captured gateway login over direct TCP, then keeps
  reconnecting and committing, logging every outcome.
- ``fenced --label L``: after the transition, stop the probe (its exact birth),
  require that its held transaction aborted, that no reconnect committed after
  it, and that every captured login is refused over TCP, the owner-only socket
  and the pooler; then delete the captured credentials.
- ``stop``: stop every recorded probe still alive (cleanup after a failure).

Only non-secret facts (names, numbers, outcomes) reach ``fence-L.json``.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psutil
import psycopg
from dotenv import dotenv_values

from base.cluster.authority import active_generation, read_secret, render_userlist
from base.db.pg_admin import pg_socket_path
from base.host.private_storage import write_private_bytes
from base.native_process.ownership import OwnedProcess

# A refused login: SCRAM fails first for a login whose verifier was removed,
# NOLOGIN after it; the pooler reports its own SCRAM failure.
_REFUSED = (
    "password authentication failed",
    "not permitted to log in",
    "SASL authentication failed",
    "no password supplied",
)
_LABEL = re.compile(r"[a-z][a-z0-9-]{0,31}")


def observe(home: Path) -> dict[str, Any]:
    """The active generation's non-secret identity and the pooler's served names."""
    generation = active_generation(home)
    userlist = (home / "pgbouncer" / "userlist.txt").read_bytes()
    if userlist != render_userlist(home, generation):
        raise RuntimeError(f"pooler does not serve exactly write generation {generation.number}")
    return {
        "number": generation.number,
        "credential_digest": generation.credential_digest,
        "roles": list(generation.roles),
        "origin": generation.origin.model_dump(mode="json"),
    }


@dataclasses.dataclass(frozen=True)
class Context:
    run: Path

    @property
    def home(self) -> Path:
        return self.run / "home"

    def ports(self) -> dict[str, int]:
        return json.loads((self.run / "config.json").read_text())["ports"]

    def database(self) -> str:
        url = dotenv_values(self.home / ".env")["AVA_DB_URL"]
        if not url:
            raise RuntimeError("preview home has no database endpoint")
        return url.rsplit("/", 1)[1].split("?", 1)[0]

    @contextmanager
    def read_only(self) -> Generator[psycopg.Connection[Any]]:
        """One read-only snapshot of this home's database, with its own deadline.

        The OS-user administrator over the owner-only socket (peer), bound to
        this home's postmaster. The proof runs from the source checkout, which
        is no admitted runtime once a release image is selected
        (`require_admitted_runtime`) and so holds no write-generation login.
        """
        from psycopg.conninfo import make_conninfo

        from base.db.pg_admin import connect, pg_admin_url

        url = make_conninfo(pg_admin_url(self.ports()["postgres"]), dbname=self.database())
        with connect(url, expected_data_dir=self.home / "pg", connect_timeout=5) as connection:
            connection.read_only = True
            connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            connection.execute("SET LOCAL statement_timeout = '5s'")
            yield connection

    def path(self, kind: str, label: str) -> Path:
        if _LABEL.fullmatch(label) is None:
            raise ValueError("invalid generation evidence label")
        return self.run / f"{kind}-{label}.json"


def capture(context: Context, label: str) -> None:
    generation = active_generation(context.home)
    secret = read_secret(context.home, generation)
    logins = {
        cls: {"name": secret.roles.of(cls).name, "password": secret.roles.of(cls).password}
        for cls in ("gateway", "runner")
    }
    body = {"number": generation.number, "logins": logins}
    write_private_bytes(context.path("generation", label), json.dumps(body).encode())


def _captured(context: Context, label: str) -> dict[str, Any]:
    return json.loads(context.path("generation", label).read_bytes())


def _dsn(context: Context, login: dict[str, str], *, host: str, port: int) -> dict[str, Any]:
    return {
        "host": host,
        "port": port,
        "user": login["name"],
        "password": login["password"],
        "dbname": context.database(),
        "connect_timeout": 5,
    }


def probe(context: Context, label: str) -> None:
    """Start the stale writer detached from this process; record its exact birth."""
    login = _captured(context, label)["logins"]["gateway"]
    log = context.run / f"stale-writer-{label}.jsonl"
    dsn = _dsn(context, login, host="127.0.0.1", port=context.ports()["postgres"])
    with log.open("wb") as output:
        process = subprocess.Popen(  # noqa: S603 — this module's own probe entry, fixed argv
            [
                sys.executable,
                "-m",
                "scripts.preview.release_generation",
                str(context.run),
                "writer",
            ],
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=Path(__file__).resolve().parents[2],
        )
    if process.stdin is None:
        raise RuntimeError("stale writer has no credential channel")
    # Credentials travel on stdin, never in argv or the environment.
    process.stdin.write(json.dumps(dsn).encode())
    process.stdin.close()
    birth = OwnedProcess.capture(psutil.Process(process.pid))
    context.path("stale-writer", label).write_text(json.dumps(dataclasses.asdict(birth)))
    deadline = time.monotonic() + 15
    while not _events(log):
        if time.monotonic() > deadline or process.poll() is not None:
            raise RuntimeError("stale writer did not open its transaction")
        time.sleep(0.1)
    if _events(log)[0]["event"] != "held":
        raise RuntimeError(f"stale writer failed before holding a transaction: {_events(log)}")


def _emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps({**event, "at": time.time()}) + "\n")
    sys.stdout.flush()


def _reason(exc: BaseException) -> str:
    lines = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {lines[-1] if lines else ''}"[:300]


def writer(dsn: dict[str, Any]) -> None:
    """Hold one open transaction until it ends, then keep trying to commit."""
    held = psycopg.connect(**dsn)
    row = held.execute("SELECT txid_current()").fetchone()
    _emit({"event": "held", "xid": None if row is None else int(row[0])})
    while True:
        try:
            held.execute("SELECT 1")
        except psycopg.Error as exc:
            _emit({"event": "held-ended", "error": _reason(exc)})
            break
        time.sleep(0.2)
    while True:
        try:
            with psycopg.connect(**dsn) as conn:
                row = conn.execute("SELECT txid_current()").fetchone()
                conn.commit()
            _emit(
                {
                    "event": "reconnect",
                    "committed": True,
                    "xid": None if row is None else int(row[0]),
                }
            )
        except psycopg.Error as exc:
            _emit({"event": "reconnect", "committed": False, "error": _reason(exc)})
        time.sleep(0.5)


def _events(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def _stop_probe(context: Context, label: str) -> None:
    _stop_birth(json.loads(context.path("stale-writer", label).read_text()))


def stop(context: Context, _label: str) -> None:
    """Stop every recorded stale writer still alive (cleanup after a failure)."""
    for record in sorted(context.run.glob("stale-writer-*.json")):
        _stop_birth(json.loads(record.read_text()))


def _stop_birth(record: dict[str, Any]) -> None:
    """SIGTERM only the recorded birth, and wait for it to be gone."""
    birth = OwnedProcess(record["pid"], record["birth"], record["starttime"])
    if birth.live():
        os.kill(birth.pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while birth.live():
            if time.monotonic() > deadline:
                raise RuntimeError("stale writer did not stop")
            time.sleep(0.05)


def judge(events: list[dict[str, Any]], held_status: str | None) -> dict[str, Any]:
    """The probe proves the fence: its held transaction aborted, it never
    committed again, and it kept trying."""
    kinds = [event["event"] for event in events]
    if kinds[:2] != ["held", "held-ended"]:
        raise RuntimeError(f"the stale writer's transaction was never ended: {kinds[:3]}")
    reconnects = [event for event in events if event["event"] == "reconnect"]
    committed = [event for event in reconnects if event["committed"]]
    if committed:
        raise RuntimeError(f"a stale writer committed after the fence: {committed[0]}")
    if not reconnects:
        raise RuntimeError("the stale writer never retried after its transaction ended")
    if held_status != "aborted":
        raise RuntimeError(f"the stale writer's held transaction is {held_status!r}, not aborted")
    return {
        "held_ended": events[1]["error"],
        "refused_reconnects": len(reconnects),
        "last_refusal": reconnects[-1]["error"],
        "held_transaction": held_status,
    }


def _held_status(context: Context, xid: int) -> str | None:
    with context.read_only() as conn:
        row = conn.execute("SELECT txid_status(%s)", (xid,)).fetchone()
    return None if row is None else row[0]


def require_refused(context: Context, logins: dict[str, dict[str, str]]) -> list[str]:
    """Every captured login fails over direct TCP, the owner-only socket and the pooler."""
    ports = context.ports()
    routes = {
        "tcp": ("127.0.0.1", ports["postgres"]),
        "socket": (str(pg_socket_path(context.home)), ports["postgres"]),
        "pooler": ("127.0.0.1", ports["pgbouncer"]),
    }
    refused: list[str] = []
    for cls, login in sorted(logins.items()):
        for route, (host, port) in routes.items():
            try:
                psycopg.connect(**_dsn(context, login, host=host, port=port)).close()
            except psycopg.OperationalError as exc:
                if not any(marker in str(exc) for marker in _REFUSED):
                    raise RuntimeError(
                        f"{cls} login over {route} failed for another reason: {exc}"
                    ) from exc
                refused.append(f"{cls}/{route}")
                continue
            raise RuntimeError(f"fenced {cls} login still authenticates over {route}")
    return refused


def fenced(context: Context, label: str) -> None:
    _stop_probe(context, label)
    events = _events(context.run / f"stale-writer-{label}.jsonl")
    held = events[0]["xid"] if events and events[0]["event"] == "held" else None
    report: dict[str, Any] = {"label": label, "result": "failed"}
    try:
        report["probe"] = judge(events, None if held is None else _held_status(context, held))
        captured = _captured(context, label)
        report["generation"] = captured["number"]
        report["refused"] = require_refused(context, captured["logins"])
        report["result"] = "passed"
    finally:
        context.path("generation", label).unlink(missing_ok=True)
        context.path("fence", label).write_text(json.dumps(report, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("action", choices=("capture", "probe", "fenced", "stop", "writer"))
    parser.add_argument("--label", default="")
    args = parser.parse_args()
    context = Context(args.run.resolve(strict=True))
    if args.action == "writer":
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        writer(json.loads(sys.stdin.read()))
        return
    actions = {"capture": capture, "probe": probe, "fenced": fenced, "stop": stop}
    actions[args.action](context, args.label)


if __name__ == "__main__":
    main()
