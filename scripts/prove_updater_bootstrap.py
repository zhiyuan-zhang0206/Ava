"""CI-only real restricted A/B updater hop, native sessions/cron and old-schema PG.

Both generations contain the same reviewed application revision with different
verified preparation inputs. This is NOT a source-version migration/LKG proof.
"""

from __future__ import annotations

import json
import os
import platform
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import psutil
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from cli.commands import _update_bootstrap as hop
from cli.commands._release_inventory import prepare_unit_inventory
from services.agent_ops.bootstrap import ObserverProjection, PreparedObservation
from shared import updater_handoff
from shared.managed_writer_barrier import RolloutIdentity
from shared.managed_writer_observation import (
    ExpectedUnitWriters,
    ObservationChallenge,
    observe_process,
)
from shared.native_job_observation import read_crontab
from shared.runtime_release import ReleaseRejectedError, VerifiedRelease, verify_release
from shared.session_backend import get_backend


def require(value: bool, message: str) -> None:  # noqa: FBT001 — CI assertion predicate.
    if not value:
        raise AssertionError(message)


def private_json(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(value)


def challenge_budget(until: datetime) -> float:
    remaining = (until - datetime.now(UTC)).total_seconds()
    require(remaining > 0, "CI operation challenge expired before invocation")
    return remaining


def fault_worker(mode: str, request: Path) -> int:  # noqa: PLR0915 — scoped real-updater fault interposition and evidence.
    """Test-only interposition around the actual existing updater entry."""
    from cli.commands._update_agent_runner import main as updater_main

    if mode in {"expire-after-stop", "holder-change-after-stop"}:
        real_wait = hop._wait_exited

        def turnover(plan: hop.PreparedBootstrapHop, process: hop.ExpectedProcess) -> None:
            real_wait(plan, process)
            with psycopg.connect(os.environ["AVA_DB_URL"], autocommit=True) as conn:
                if mode == "expire-after-stop":
                    conn.execute(
                        "UPDATE deployment_state SET expires_at=clock_timestamp()-interval '1 second'"
                    )
                else:
                    conn.execute("UPDATE deployment_state SET holder='another-operation'")

        backend = get_backend()
        with (
            patch.object(hop, "_wait_exited", side_effect=turnover),
            patch.object(backend, "new_session", wraps=backend.new_session) as starts,
        ):
            try:
                return updater_main(["--bootstrap-hop", str(request)])
            finally:
                (request.parent / f"effect-count-{mode}.json").write_text(
                    json.dumps({"native_session_starts": starts.call_count})
                )
    if mode == "crash-after-stop":
        real_journal = hop._journal

        def crash(plan: hop.PreparedBootstrapHop, generation: str, stage: str, cron: bytes) -> None:
            real_journal(plan, generation, stage, cron)
            if stage == "old_stopped":
                raise SystemExit(77)

        with patch.object(hop, "_journal", side_effect=crash):
            return updater_main(["--bootstrap-hop", str(request)])
    if mode != "candidate-bind-failure":
        raise AssertionError("unknown CI fault mode")
    real_start = hop._start_observer

    def blocked_candidate(
        plan: hop.PreparedBootstrapHop, image: hop.VerifiedRelease, context_path: str
    ) -> None:
        if image.digest != plan.image.digest:
            real_start(plan, image, context_path)
            return
        with socket.socket() as occupied:
            # A has exited, but its accepted connections can remain in TIME_WAIT.
            # Reuse that address without SO_REUSEPORT: an existing live listener
            # still refuses, and B must actually fail against this occupied one.
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupied.bind(("127.0.0.1", plan.projection.ops_port))
            occupied.listen()
            real_start(plan, image, context_path)
            deadline = time.monotonic() + challenge_budget(plan.candidate.challenge.valid_until) / 2
            while time.monotonic() < deadline:
                try:
                    process, kind = hop._recorded_observer(plan)
                except ReleaseRejectedError:
                    time.sleep(0.05)
                    continue
                require(kind == "B", "failure fixture did not launch actual B")
                if observe_process(process) == "exited":
                    (request.parent / "occupied-candidate-exit.json").write_text(
                        json.dumps(
                            {"identity": process.model_dump(), "exited_while_occupied": True}
                        )
                    )
                    return
                time.sleep(0.05)
            raise AssertionError("candidate did not encounter the actual occupied endpoint")

    def recorded_candidate(
        plan: hop.PreparedBootstrapHop, image: hop.VerifiedRelease, context_path: str
    ) -> None:
        try:
            blocked_candidate(plan, image, context_path)
        except Exception as exc:
            # The updater may legitimately catch this failure for compensation.
            # Preserve fixture versus native-launch attribution without secret text.
            (request.parent / "occupied-candidate-error.json").write_text(
                json.dumps({"exception": type(exc).__name__, "errno": getattr(exc, "errno", None)})
            )
            raise

    with patch.object(hop, "_start_observer", side_effect=recorded_candidate):
        return updater_main(["--bootstrap-hop", str(request)])


def wait_endpoint(context: PreparedObservation, projection: ObserverProjection) -> None:
    deadline = time.monotonic() + challenge_budget(context.challenge.valid_until) / 2
    last_error = "no observation attempted"
    while time.monotonic() < deadline:
        try:
            hop.probe_bootstrap(context, projection)
            return
        except (OSError, ValueError, RuntimeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.05)
    raise AssertionError("actual restricted endpoint did not become available: " + last_error)


def verify_probe_reuse(
    context: PreparedObservation,
    projection: ObserverProjection,
    image: VerifiedRelease,
    other_image: VerifiedRelease,
) -> None:
    """Actual HTTP identity still rejects a mismatched invocation-local image."""
    with patch.object(hop, "_verify_image", side_effect=AssertionError("rehashed image")):
        hop.probe_bootstrap(context, projection, verified_image=image)
        for invalid in (
            other_image,
            replace(image, root=image.root.parent.parent / "wrong-home" / image.digest),
            replace(image, manifest_digest="0" * 64),
        ):
            try:
                hop.probe_bootstrap(context, projection, verified_image=invalid)
            except ReleaseRejectedError:
                continue
            raise AssertionError("actual endpoint accepted a mismatched verified image")


def failure_snapshot(home: Path, old: hop.ExpectedProcess) -> str:
    """Bounded identity-only evidence before the CI fixture cleans up processes."""
    evidence: dict[str, object] = {
        "old_identity": old.model_dump(),
        "old_state": observe_process(old),
    }
    try:
        state = json.loads(updater_handoff.state_path().read_bytes())
        evidence["journal_stage"] = state.get("bootstrap_hop", {}).get("stage", "not-created")
    except FileNotFoundError:
        evidence["journal_stage"] = "absent"
    try:
        record = json.loads((home / "run/sessions/ava-ops.json").read_bytes())
        current = hop.ExpectedProcess.model_validate(
            {key: record[key] for key in ("pid", "create_time", "starttime")}
        )
        evidence["record_identity"] = current.model_dump()
        evidence["record_state"] = observe_process(current)
        if evidence["record_state"] == "alive":
            proc = psutil.Process(current.pid)
            evidence["record_executable"] = proc.exe()
            evidence["children"] = [
                {"pid": child.pid, "birth": child.create_time(), "name": child.name()}
                for child in proc.children(recursive=True)
            ]
    except FileNotFoundError:
        evidence["record_identity"] = "absent"
    except psutil.Error as exc:
        evidence["record_observation_error"] = type(exc).__name__
    return json.dumps(evidence, sort_keys=True)


def record_hop_observation(
    home: Path,
    mode: str,
    old: hop.ExpectedProcess,
    started: float,
    outcome: int | str,
    stderr: bytes,
) -> None:
    (home.parent / f"hop-observation-{mode}.json").write_text(
        json.dumps(
            {
                "outcome": outcome,
                "elapsed_s": time.monotonic() - started,
                "native": json.loads(failure_snapshot(home, old)),
                "phases": [
                    line
                    for line in stderr.decode(errors="replace").splitlines()
                    if "bootstrap_hop_phase " in line
                ],
            },
            indent=2,
        )
    )


def main() -> None:  # noqa: PLR0915 — isolated CI native lifetimes, always restored in finally.
    if len(sys.argv) > 1 and sys.argv[1] == "--fault-worker":
        raise SystemExit(fault_worker(sys.argv[2], Path(sys.argv[3])))
    artifact, manifest, old_artifact, old_manifest, schema_digest = sys.argv[1:]
    home = Path(os.environ["AVA_HOME"]).resolve()
    require(
        sys.platform == "linux"
        and os.environ.get("GITHUB_ACTIONS") == "true"
        and home.is_relative_to(Path(os.environ["RUNNER_TEMP"]).resolve()),
        "native updater proof requires isolated Linux CI scratch",
    )
    image = verify_release(
        home / "releases",
        artifact,
        manifest_digest=manifest,
        platform_tag=platform.platform(),
        schema_digest=schema_digest,
    )
    old_image = verify_release(
        home / "releases",
        old_artifact,
        manifest_digest=old_manifest,
        platform_tag=platform.platform(),
        schema_digest=schema_digest,
    )
    require(image.digest != old_image.digest, "A/B must be distinct real prepared generations")
    sessions = home / "run/sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    require(not list(sessions.iterdir()), "proof refuses existing session records")
    until = datetime.now(UTC) + timedelta(minutes=10)
    original_cron = read_crontab(until)
    require(not original_cron.strip(), "proof refuses to replace another CI job")
    namespace = "updater_" + uuid4().hex
    base_url = os.environ["AVA_DB_URL"]
    with psycopg.connect(base_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace)))
        conn.execute("SELECT set_config('search_path',%s,false)", (namespace,))
        conn.execute("CREATE TABLE machine_units(machine_name text,home text)")
        conn.execute("INSERT INTO machine_units VALUES ('runtime-proof',%s)", (str(home),))
        conn.execute(
            "CREATE TABLE deployment_state(id int,phase text,kind text,note text,holder text,"
            "acquired_at timestamptz,expires_at timestamptz,target_sha text)"
        )
        row = conn.execute(
            "INSERT INTO deployment_state VALUES (1,'updating','rollout',NULL,'hop',"
            "clock_timestamp(),clock_timestamp()+interval '10 minutes',%s) "
            "RETURNING acquired_at",
            ("d" * 40,),
        ).fetchone()
        if row is None:
            raise AssertionError("operation fixture missing")
        operation = RolloutIdentity(holder="hop", acquired_at=row[0], target_sha="d" * 40)
        with socket.socket() as free:
            free.bind(("127.0.0.1", 0))
            port = free.getsockname()[1]
        env = dict(os.environ) | {
            "AVA_DB_URL": make_conninfo(base_url, options=f"-csearch_path={namespace}"),
            "AVA_CLUSTER_SECRET": uuid4().hex,
            "AVA_OPS_HEALTH_PORT": str(port),
        }
        projection = ObserverProjection.model_validate(
            {
                "db_url": env["AVA_DB_URL"],
                "cluster_secret": env["AVA_CLUSTER_SECRET"],
                "ops_port": port,
            }
        )
        old_context = PreparedObservation(
            expected=ExpectedUnitWriters(
                machine="runtime-proof",
                home=str(home),
                artifact_digest=old_artifact,
                manifest_digest=old_manifest,
                processes=(),
                sessions=(),
                launchers=(),
            ),
            operation=operation,
            challenge=ObservationChallenge(challenge=uuid4(), valid_until=until),
            schema_digest=schema_digest,
        )
        old_path = home / "run/hop-recovery.json"
        private_json(old_path, old_context.model_dump_json())
        old_command = hop.bootstrap_command(old_image, old_path)
        cron = (
            "@reboot "
            + shlex.join([f"AVA_HOME={home}", *old_command])
            + " # ava-restricted-proof\n"
        ).encode()

        def install_cron(value: bytes) -> None:
            subprocess.run(["/usr/bin/crontab", "-"], input=value, check=True, timeout=5)
            require(read_crontab(until) == value, "native crontab write was not observed")

        def stop_session() -> None:
            backend = get_backend()
            if backend.has_session("ava-ops"):
                stopped, detail = backend.kill_session("ava-ops", graceful=True, timeout=10)
                require(stopped, "CI exact native cleanup failed: " + detail)

        try:
            for mode in (
                "success",
                "candidate-bind-failure",
                "crash-after-stop",
                "expire-after-stop",
                "holder-change-after-stop",
            ):
                install_cron(cron)
                require(
                    get_backend().new_session(
                        "ava-ops",
                        "exec " + shlex.join(old_command),
                        home,
                        env=env,
                        login_shell=False,
                    ),
                    "A native session launch refused",
                )
                wait_endpoint(old_context, projection)
                if mode == "success":
                    verify_probe_reuse(old_context, projection, old_image, image)
                with psycopg.connect(env["AVA_DB_URL"]) as inventory_conn:
                    receipt = prepare_unit_inventory(
                        inventory_conn, image, home, "runtime-proof", schema_digest=schema_digest
                    )
                expected = ExpectedUnitWriters.model_validate_json(
                    json.dumps(json.loads(receipt.read_bytes())["expected"])
                )
                candidate = PreparedObservation(
                    expected=expected,
                    operation=operation,
                    challenge=ObservationChallenge(challenge=uuid4(), valid_until=until),
                    schema_digest=schema_digest,
                )
                candidate_path = home / f"run/hop-candidate-{mode}.json"
                private_json(candidate_path, candidate.model_dump_json())
                # Real prior updater ownership is recorded by a separate process,
                # not a made-up dead PID. It remains alive for the refusal probe.
                predecessor = subprocess.Popen(  # noqa: S603 — verified image, fixed CI ownership helper.
                    [
                        str(image.interpreter),
                        "-I",
                        "-B",
                        "-c",
                        "from shared import updater_handoff as h; import sys; "
                        "g=h.begin(expected_session='prior-updater').generation; "
                        "assert h.claim_running(g,expected_session='prior-updater'); sys.stdin.buffer.read(1)",
                    ],
                    cwd=home,
                    env=env,
                    stdin=subprocess.PIPE,
                )
                deadline = time.monotonic() + 10
                while updater_handoff.read().status != "running":
                    require(time.monotonic() < deadline, "predecessor failed to publish handoff")
                    time.sleep(0.05)
                prior = updater_handoff.read()
                request_path = home / f"run/hop-request-{mode}.json"
                private_json(
                    request_path,
                    json.dumps(
                        {
                            "candidate_context": str(candidate_path),
                            "recovery_context": str(old_path),
                            "inventory_receipt": str(receipt),
                            "predecessor": {
                                "pid": prior.owner_pid,
                                "create_time": prior.owner_create_time,
                            },
                        }
                    ),
                )
                normal = [
                    str(image.interpreter),
                    "-I",
                    "-B",
                    "-X",
                    "faulthandler",
                    "-m",
                    "cli.commands._update_agent_runner",
                    "--bootstrap-hop",
                    str(request_path),
                ]
                try:
                    refused = subprocess.run(  # noqa: S603 — actual verified updater, CI private request.
                        normal, cwd=home, env=env, capture_output=True, timeout=30, check=False
                    )
                    require(refused.returncode != 0, "live predecessor was accepted")
                    require(read_crontab(until) == cron, "refusal changed native launcher")
                    wait_endpoint(old_context, projection)
                except subprocess.TimeoutExpired as exc:
                    stderr = exc.stderr or b""
                    raise AssertionError(
                        "live-predecessor refusal timed out: "
                        + stderr.decode(errors="replace")[-12000:]
                    ) from exc
                finally:
                    if predecessor.stdin is None:
                        raise AssertionError("predecessor lacks its requested pipe")
                    predecessor.stdin.close()
                    predecessor.wait(timeout=10)
                argv = (
                    normal
                    if mode == "success"
                    else [
                        str(image.interpreter),
                        "-I",
                        "-B",
                        str(Path(__file__).resolve()),
                        "--fault-worker",
                        mode,
                        str(request_path),
                    ]
                )
                invocation_started = time.monotonic()
                try:
                    result = subprocess.run(  # noqa: S603 — verified updater or copied CI-only fault worker.
                        argv,
                        cwd=home,
                        env=env,
                        capture_output=True,
                        timeout=challenge_budget(until),
                        check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    raw = json.loads(updater_handoff.state_path().read_bytes())
                    stage = raw.get("bootstrap_hop", {}).get("stage", "before-bootstrap-journal")
                    stderr = exc.stderr or b""
                    record_hop_observation(
                        home,
                        mode,
                        expected.sessions[0].process,
                        invocation_started,
                        "timeout",
                        stderr,
                    )
                    raise AssertionError(
                        f"{mode} timed out at {stage}; "
                        + failure_snapshot(home, expected.sessions[0].process)
                        + ": "
                        + stderr.decode(errors="replace")[-12000:]
                    ) from exc
                record_hop_observation(
                    home,
                    mode,
                    expected.sessions[0].process,
                    invocation_started,
                    result.returncode,
                    result.stderr,
                )
                if result.returncode not in {
                    3 if mode == "success" else 77 if mode == "crash-after-stop" else 1
                }:
                    raise AssertionError(
                        f"{mode} updater returned {result.returncode}; "
                        + failure_snapshot(home, expected.sessions[0].process)
                        + ": "
                        + result.stderr.decode(errors="replace")[-4000:]
                    )
                if mode == "crash-after-stop":
                    require(
                        json.loads(updater_handoff.state_path().read_bytes())["bootstrap_hop"][
                            "stage"
                        ]
                        == "old_stopped",
                        "crash discarded compensating inputs",
                    )
                    resumed = subprocess.run(  # noqa: S603 — same verified updater and retained request.
                        normal,
                        cwd=home,
                        env=env,
                        capture_output=True,
                        timeout=challenge_budget(until),
                        check=False,
                    )
                    require(resumed.returncode == 1, "dead-owner resume did not restore A")
                if mode == "candidate-bind-failure":
                    evidence = home / "run/occupied-candidate-exit.json"
                    if not evidence.exists():
                        diagnostic = home / "run/occupied-candidate-error.json"
                        detail = (
                            diagnostic.read_text()
                            if diagnostic.exists()
                            else "no fixture error recorded"
                        )
                        raise AssertionError(
                            "actual candidate bind failure was not proved: " + detail
                        )
                    failure = json.loads(evidence.read_text())
                    require(
                        failure["exited_while_occupied"] is True,
                        "compensation swallowed a failed native bind fault fixture",
                    )
                if mode in {"expire-after-stop", "holder-change-after-stop"}:
                    effects = json.loads((home / f"run/effect-count-{mode}.json").read_text())
                    require(
                        effects["native_session_starts"] == 0,
                        "stale operation attempted even an unrecorded native spawn",
                    )
                    recorded = json.loads((sessions / "ava-ops.json").read_bytes())
                    require(
                        recorded["pid"] == expected.sessions[0].process.pid,
                        "stale operation spawned another process",
                    )
                    require(
                        observe_process(expected.sessions[0].process) == "exited",
                        "authority turnover was not injected after real stop",
                    )
                    require(read_crontab(until) == b"", "stale operation restored launcher")
                    require(
                        updater_handoff.state_path().exists(), "failure discarded recovery record"
                    )
                    # Reset only this isolated fixture for the next independent case;
                    # production cannot revive an expired operation this way.
                    conn.execute(
                        "UPDATE deployment_state SET holder='hop',expires_at=clock_timestamp()+interval '10 minutes'"
                    )
                    updater_handoff.state_path().unlink()
                    (sessions / "ava-ops.json").unlink()
                    continue
                wait_endpoint(candidate if mode == "success" else old_context, projection)
                require(
                    read_crontab(until) == (b"" if mode == "success" else cron),
                    "native quiesce/recovery readback differed",
                )
                require(updater_handoff.read().status == "inactive", "terminal handoff not cleared")
                stop_session()
                (sessions / "ava-ops.json").unlink(missing_ok=True)
            (home.parent / "updater-bootstrap-proof.json").write_text(
                json.dumps(
                    {
                        "actualUpdaterModule": True,
                        "sourceAbsent": True,
                        "distinctPreparedGenerationsSameRevision": True,
                        "nativeCronQuiesceReadback": True,
                        "actualSessionResponderIdentity": True,
                        "livePredecessorRefusedBeforeStop": True,
                        "candidateFailureRestoresRestrictedA": True,
                        "deadOwnerCrashResumeRestoresRestrictedA": True,
                        "leaseTurnoverDuringStopPreventsSpawnAndCron": True,
                        "normalSourceLkgProved": False,
                        "normalReady": False,
                    },
                    indent=2,
                )
            )
        finally:
            stop_session()
            install_cron(original_cron)
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(namespace)))


if __name__ == "__main__":
    main()
