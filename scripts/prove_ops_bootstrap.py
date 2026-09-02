"""CI-only actual retained-wheel ops bootstrap against isolated old-schema PG."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import psutil
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from services.agent_ops.bootstrap import PreparedObservation
from shared.managed_writer_barrier import RolloutIdentity
from shared.managed_writer_observation import (
    ExpectedProcess,
    ExpectedUnitWriters,
    ObservationChallenge,
)


def require(condition: bool, message: str) -> None:  # noqa: FBT001 — CI assertion predicate
    if not condition:
        raise AssertionError(message)


def main() -> None:  # noqa: PLR0915 — one bounded CI process/DB lifetime with explicit cleanup
    artifact, manifest, schema_digest = sys.argv[1:]
    home = Path(os.environ["AVA_HOME"]).resolve()
    if os.environ.get("GITHUB_ACTIONS") != "true" or not home.is_relative_to(
        Path(os.environ["RUNNER_TEMP"]).resolve()
    ):
        raise RuntimeError("bootstrap proof is restricted to GitHub runner scratch")
    namespace = "observer_" + uuid4().hex
    base_url = os.environ["AVA_DB_URL"]
    with psycopg.connect(base_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace)))
        conn.execute("SELECT set_config('search_path',%s,false)", (namespace,))
        conn.execute("CREATE TABLE machine_units(machine_name text,home text)")
        conn.execute("INSERT INTO machine_units VALUES ('proof',%s)", (str(home),))
        conn.execute(
            "CREATE TABLE deployment_state(id int,phase text,kind text,note text,holder text,"
            "acquired_at timestamptz,expires_at timestamptz,target_sha text)"
        )
        row = conn.execute(
            "INSERT INTO deployment_state VALUES (1,'updating','rollout',NULL,'proof',"
            "clock_timestamp(),clock_timestamp()+interval '10 minutes',%s) RETURNING acquired_at",
            ("c" * 40,),
        ).fetchone()
        if row is None:
            raise AssertionError("fixture operation missing")
        context = PreparedObservation(
            expected=ExpectedUnitWriters(
                machine="proof",
                home=str(home),
                artifact_digest=artifact,
                manifest_digest=manifest,
                processes=(
                    ExpectedProcess(pid=os.getpid(), create_time=psutil.Process().create_time()),
                ),
                sessions=(),
                launchers=(),
            ),
            operation=RolloutIdentity(holder="proof", acquired_at=row[0], target_sha="c" * 40),
            challenge=ObservationChallenge(
                challenge=uuid4(), valid_until=row[0] + timedelta(minutes=8)
            ),
            schema_digest=schema_digest,
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        token = uuid4().hex
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(home.parent),
            "AVA_HOME": str(home),
            "AVA_DB_URL": make_conninfo(base_url, options=f"-csearch_path={namespace}"),
            "AVA_CLUSTER_SECRET": token,
            "AVA_OPS_HEALTH_PORT": str(port),
            "AVA_GATEWAY_URL": "http://127.0.0.1:1",
            "AVA_MACHINE_SERVE_GATEWAY": "false",
            "AVA_MACHINE_SERVE_AGENT_RUNNER": "true",
        }
        pid_files = set(home.rglob("*.pid"))

        def argv(candidate: PreparedObservation) -> list[str]:
            path = home.parent / f"observer-context-{uuid4().hex}.json"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(candidate.model_dump_json())
            return [
                sys.executable,
                "-I",
                "-B",
                "-m",
                "services.agent_ops.daemon",
                "--bootstrap-observation",
                str(path),
            ]

        def request(
            path: str, bearer: str, body: bytes | None = None
        ) -> tuple[int, dict[str, Any]]:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}",
                data=body,
                headers={"Authorization": f"Bearer {bearer}"},
            )
            try:
                response = urllib.request.urlopen(req, timeout=3)  # noqa: S310 — fixed CI loopback
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                raw = response.read()
                return response.code, json.loads(raw) if raw else {}

        try:
            wrong_home = context.model_copy(
                update={"expected": context.expected.model_copy(update={"home": str(home.parent)})}
            )
            wrong_image = context.model_copy(
                update={
                    "expected": context.expected.model_copy(update={"manifest_digest": "f" * 64})
                }
            )
            wrong_holder = context.model_copy(
                update={"operation": context.operation.model_copy(update={"holder": "expired"})}
            )
            for rejected in (wrong_home, wrong_image, wrong_holder):
                result = subprocess.run(  # noqa: S603 — exact retained entry and private fixture context
                    argv(rejected),
                    env=env,
                    cwd=home.parent,
                    capture_output=True,
                    timeout=120,
                    check=False,
                )
                require(result.returncode == 2, "invalid prepared context was not refused")
                require(set(home.rglob("*.pid")) == pid_files, "refused entry wrote PID state")
                with socket.socket() as probe:
                    require(
                        probe.connect_ex(("127.0.0.1", port)) != 0, "refused entry bound socket"
                    )
            child = subprocess.Popen(  # noqa: S603 — exact retained entry and private fixture context
                argv(context),
                env=env,
                cwd=home.parent,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 120
                while True:
                    require(child.poll() is None, "bootstrap child exited before bind")
                    try:
                        status, health = request("/healthz", token)
                        break
                    except urllib.error.URLError:
                        if time.monotonic() >= deadline:
                            raise AssertionError("bootstrap child never bound") from None
                        time.sleep(0.1)
                require(
                    status == 503 and health["full_ready"] is False, "bootstrap claims full health"
                )
                payload = json.dumps({"challenge": str(context.challenge.challenge)}).encode()
                require(request("/ops", token, b"{}")[0] == 404, "ordinary ops exposed")
                require(
                    request("/ops/bootstrap-observation", "wrong", payload)[0] == 401, "auth bypass"
                )
                status, observed = request("/ops/bootstrap-observation", token, payload)
                require(
                    status == 200 and observed["processes"] == ["alive"], "real process unobserved"
                )
                require(observed["closure"] == "unknown", "incomplete inventory asserted closure")
                conn.execute(
                    "UPDATE deployment_state SET expires_at=clock_timestamp()-interval '1 second'"
                )
                require(
                    request("/ops/bootstrap-observation", token, payload)[0] == 409,
                    "stale lease accepted",
                )
                require(set(home.rglob("*.pid")) == pid_files, "observer wrote normal PID state")
                (home.parent / "ops-bootstrap-proof.json").write_text(
                    json.dumps(
                        {
                            "actualDaemonEntry": True,
                            "oldSchema": True,
                            "gatewayUnavailable": True,
                            "negativeHomeImageHolder": True,
                            "bootstrapOnly": True,
                            "realPidObserved": True,
                            "expiredOperationRefused": True,
                            "noOrdinaryPidEffects": True,
                        }
                    )
                )
            finally:
                child.terminate()
                child.wait(timeout=10)
        finally:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(namespace)))


if __name__ == "__main__":
    main()
