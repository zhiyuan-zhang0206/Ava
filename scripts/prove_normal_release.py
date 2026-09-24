"""CI-only real normal-release checked-chain proof against isolated PG + native sessions.

WHAT THIS PROVES (design #4117 section 7.2; plan ws/4132-plan.md D1)

The normal-release checked chain (``cli.commands._update_normal_release``) --
the stage machine over ``waiting -> selected -> bootstrap_stopped -> starting ->
observed -> committed`` -- is driven for real. The retained image's interpreter
runs this script as a subprocess; the chain stops a real restricted observer
session, ``start_normal_service`` performs a real gated fork (per-session gate +
pre-exec birth receipt through ``shared.spawn_receipt``), and every stage's
recovery logic is exercised by crashing at one named seam per case and
re-entering through the production ``updater_handoff.resume_bootstrap`` entry
(exact owner-death evidence; a NEW process reclaims the handoff).

After both passes the driver asserts, per design 7.2 a-e:

  a) at most one live instance per service session, with the survivor's record
     + birth receipt identity cross-checked and no straggler caught by an
     exact-argv scan or a second listener;
  b) zero unproven stop signals: every ``graceful_signal`` call carries the
     exact retained bootstrap identity (spy-recorded);
  c) the retained journal stage stays inside the per-case allowed set;
  d) re-entry converges without extra spawn attempts or selector rewrites;
  e) the violations list in ``normal-release-proof.json`` totals zero.

The service processes are CI-only fixtures: the image's own interpreter runs an
inline HTTP server (``FIXTURE_SOURCE``) as the service command. They are
spawned through the real machinery (real gate, real receipt, real record, real
readiness observation -- the module-less branch of ``observe_normal_service``,
the same branch a frontend start uses), so the proof drives the exact code path
a production start takes without booting the production service roster. The
proof refuses outside isolated GitHub Linux scratch (same environment gate as
the rest of the prove family).

Artifacts: ``$RUNNER_TEMP/prepare/normal-release-proof.json`` plus
``normal-release-observation-<case>.json`` and per-pass event logs under
``normal-release-cases/<case>/``.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

import psutil
import psycopg
from dotenv import dotenv_values
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from cli.commands import _release_services as release_services
from cli.commands import _update_normal_release as normal
from cli.commands._release_inventory import prepare_unit_inventory
from cli.commands._release_selector import pending_transaction, selector_bytes
from cli.commands._release_services import PreparedService, normal_spawn_command
from cli.commands._update_bootstrap import bootstrap_command
from ops.service_spec import ServiceSpec
from services.agent_ops.bootstrap import (
    ObserverProjection,
    PreparedObservation,
    read_prepared_context,
)
from shared import posixproc, spawn_receipt, updater_handoff
from shared.machine import MachineRole
from shared.managed_writer_activation import commit_current
from shared.managed_writer_barrier import (
    ManagedUnit,
    ManagedUnitClosure,
    ManagedWriterCollection,
    RolloutIdentity,
)
from shared.managed_writer_observation import (
    ExpectedProcess,
    ExpectedUnitWriters,
    ObservationChallenge,
    observe_process,
)
from shared.managed_writer_publication import (
    CandidateUnitPlan,
    NormalService,
    NormalStartPlan,
    PendingPublication,
    PublishedUnit,
    WriterPublication,
)
from shared.native_job_observation import read_crontab
from shared.runtime_release import ReleaseRejectedError, verify_release
from shared.session_backend import get_backend
from shared.session_record import SessionRecord

_TARGET_SHA = "d" * 40
_MACHINE = "runtime-proof"
_SESSION_LABEL = "normal-release-proof"
_APPLIED_NAMES = ("0001_ci_proof",)
_CLUSTER_SECRET = "normal-release-proof-secret"  # noqa: S105 -- CI-only fixture bearer.
_CAPABILITY: frozenset[MachineRole] = frozenset({"gateway"})

# The CI-only service fixture source. argv after sh -c exec:
#   <image python> -B -c FIXTURE_SOURCE <port> <startup-delay-seconds>
# It serves 200 for any GET on its exact loopback port. The module-less branch
# of observe_normal_service (the frontend branch) requires exactly: one owned
# listener, the prepared executable + argv, and a bounded 200.
FIXTURE_SOURCE = (
    "import json,os,sys,time\n"
    "time.sleep(float(sys.argv[2]))\n"
    "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
    "\n"
    "class _Handler(BaseHTTPRequestHandler):\n"
    "    def do_GET(self):\n"
    "        body = json.dumps({'fixture': 'normal-release-proof', 'pid': os.getpid()}).encode()\n"
    "        self.send_response(200)\n"
    "        self.send_header('Content-Type', 'application/json')\n"
    "        self.send_header('Content-Length', str(len(body)))\n"
    "        self.end_headers()\n"
    "        self.wfile.write(body)\n"
    "\n"
    "    def log_message(self, *args):\n"
    "        pass\n"
    "\n"
    "ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), _Handler).serve_forever()\n"
)


def require_present[T](value: T | None, message: str) -> T:
    """Fail the proof when `value` is None, and narrow the Optional for pyright."""
    if value is None:
        raise AssertionError(message)
    return value


def require(condition: bool, message: str) -> None:  # noqa: FBT001 -- CI assertion predicate.
    if not condition:
        raise AssertionError(message)


def install_cron(value: bytes) -> None:
    """Install the CI user's crontab -- the unit's real launcher inventory source."""
    subprocess.run(["/usr/bin/crontab", "-"], input=value, check=True, timeout=5)
    require(
        read_crontab(datetime.now(UTC) + timedelta(seconds=30)) == value,
        "native crontab write was not observed",
    )


def private_json(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(value)


class Events:
    """One pass's structured evidence log (JSON lines, appended)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    def write(self, payload: dict[str, object]) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, sort_keys=True) + "\n")


def read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def scoped_db_url(namespace: str) -> str:
    return make_conninfo(os.environ["AVA_DB_URL"], options=f"-csearch_path={namespace}")


def scoped_unit_env(original: bytes, namespace: str) -> bytes:
    """Rewrite the unit .env's AVA_DB_URL to this proof's scoped URL.

    The checked-chain drives load shared.config, and the boot env-authority
    pass makes the unit's .env the authoritative source for AVA_DB_URL: an
    ambient scoped override is clobbered back to the unit's raw URL, so every
    drive loses this proof's search_path namespace and dies on its first table
    lookup ("relation deployment_state does not exist"). The file IS the
    unit's database projection, so for this proof's lifetime it carries the
    scoped URL; main() restores the original bytes in its finally. The quoted
    keyword-value conninfo must survive the dotenv parse byte-for-byte --
    asserted below with a round-trip through dotenv itself -- and psycopg
    parses it unchanged.
    """
    url = scoped_db_url(namespace)
    line = f'AVA_DB_URL="{url}"'
    # A double-quoted dotenv value decodes escapes and interpolates ${...}, so
    # a quote, backslash or dollar-brace in the URL would silently rewrite the
    # credential; re-parse the written line with the same dotenv call the unit
    # boot uses and refuse any deviation from the scoped URL.
    parsed = dotenv_values(stream=StringIO(line)).get("AVA_DB_URL")
    if parsed != url:
        raise AssertionError(
            f"scoped AVA_DB_URL does not survive the .env round-trip: dotenv reads {parsed!r}"
        )
    lines = original.decode("utf-8").splitlines()
    for index, candidate in enumerate(lines):
        if candidate.startswith("AVA_DB_URL="):
            lines[index] = line
            break
    else:
        raise AssertionError("unit .env does not declare AVA_DB_URL")
    return ("\n".join(lines) + "\n").encode("utf-8")


def case_env(home: Path, namespace: str, ops_port: int) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home.parent),
        "AVA_HOME": str(home),
        "AVA_DB_URL": scoped_db_url(namespace),
        "AVA_CLUSTER_SECRET": _CLUSTER_SECRET,
        "AVA_OPS_HEALTH_PORT": str(ops_port),
        "AVA_TRANSPORT_ENCRYPTION": "overlay",
        "AVA_GATEWAY_URL": "http://127.0.0.1:1",
        "GITHUB_ACTIONS": "true",
        "RUNNER_TEMP": os.environ["RUNNER_TEMP"],
    }


def session_record(home: Path, session: str) -> SessionRecord | None:
    return SessionRecord.read(home / "run" / "sessions" / f"{session}.json")


def record_identity(record: SessionRecord) -> ExpectedProcess:
    return ExpectedProcess(
        pid=record.pid, create_time=record.create_time, starttime=record.starttime
    )


def no_record_write(_self: SessionRecord, _path: Path) -> None:
    """INJ-8: suppress the record write so the crash lands in the W2 window
    (a live child without its record) that recovery must reconcile."""


def poll_until(predicate: Any, what: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def port_listeners(port: int) -> list[dict[str, object]]:
    listeners: list[dict[str, object]] = []
    for entry in psutil.net_connections(kind="tcp"):
        if entry.laddr and entry.laddr.port == port and entry.status == psutil.CONN_LISTEN:
            listeners.append({"pid": entry.pid})
    return listeners


def stragglers_by_argv(argv: tuple[str, ...]) -> list[int]:
    """Every live process whose exact argv equals the fixture service command."""
    wanted = list(argv)
    found: list[int] = []
    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            if process.info["cmdline"] == wanted:
                found.append(int(process.info["pid"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


# --- drive reconstruction ----------------------------------------------------


def prepared_service(image: Any, entry: dict[str, Any], home: Path) -> PreparedService:
    session = str(entry["session"])
    port = int(entry["port"])
    delay = float(entry.get("delay", 0.0))
    argv = (str(image.interpreter), "-B", "-c", FIXTURE_SOURCE, str(port), str(delay))
    resolved = str(image.interpreter.resolve(strict=True))
    prepared = PreparedService(
        identity=NormalService(
            session=session,
            module=None,
            executable=resolved,
            entrypoint=resolved,
            command_digest=hashlib.sha256(("exec " + shlex.join(argv)).encode()).hexdigest(),
        ),
        spec=ServiceSpec(
            session=session.removeprefix("ava-"),
            cmd="exec " + shlex.join(argv),
            capabilities=_CAPABILITY,
            requires_db=False,
            curl_url=f"http://127.0.0.1:{port}/healthz",
            tcp_port=port,
        ),
        argv=argv,
        cwd=image.cwd,
        environment={
            "PATH": "/usr/bin:/bin",
            "HOME": str(home.parent),
            "AVA_HOME": str(home),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    require(
        normal_spawn_command(prepared) == prepared.spec.cmd,
        "fixture command construction drifted from normal_spawn_command",
    )
    return prepared


def rebuild_plan(meta: dict[str, Any], home: Path) -> normal.PreparedNormalRelease:
    image = verify_release(
        home / "releases",
        str(meta["artifact"]),
        manifest_digest=str(meta["manifest"]),
        platform_tag=platform.platform(),
        schema_digest=str(meta["schema_digest"]),
    )
    request = normal.NormalReleaseRequest.model_validate_json(
        Path(str(meta["request_path"])).read_bytes()
    )
    context = read_prepared_context(Path(str(meta["context_path"])))
    services = tuple(prepared_service(image, entry, home) for entry in meta["services"])
    return normal.PreparedNormalRelease(
        request_path=Path(str(meta["request_path"])),
        request=request,
        context=context,
        projection=ObserverProjection.from_environment(),
        services=services,
        bootstrap=SessionRecord(**meta["bootstrap"]),
        resume_generation=str(meta["generation"]),
    )


def write_bootstrap_journal(
    plan: normal.PreparedNormalRelease, generation: str, events: Events
) -> None:
    journal: dict[str, object] = {
        "request": str(plan.request_path),
        "request_digest": hashlib.sha256(Path(plan.request_path).read_bytes()).hexdigest(),
        "inventory_digest": plan.request.unit.inventory_digest,
        "candidate_context_digest": hashlib.sha256(
            Path(plan.request.context_path).read_bytes()
        ).hexdigest(),
        "recovery_context_digest": hashlib.sha256(b"recovery:" + generation.encode()).hexdigest(),
        "normal_release_planned": True,
        "stage": "candidate_ready",
        "cron": "",
        "phases": [
            {
                "stage": "candidate_ready",
                "observed_at": datetime.now(UTC).isoformat(),
                "monotonic_s": 0.0,
                "pid": os.getpid(),
                "elapsed_s": None,
            }
        ],
    }
    updater_handoff.write_bootstrap_recovery(generation, journal)
    events.write({"event": "bootstrap-journal-written"})


def run_flow(
    mode: str, plan: normal.PreparedNormalRelease, meta: dict[str, Any], events: Events
) -> int:
    generation = str(meta["generation"])
    readback = normal._drive_checked_normal_release(plan, generation)
    events.write({"event": "chain-complete"})
    context = plan.context
    with (
        psycopg.connect(
            plan.projection.db_url.get_secret_value(), autocommit=True, connect_timeout=5
        ) as conn,
        pending_transaction(conn, context),
    ):
        publication_id = commit_current(
            conn, context.operation, context.challenge.challenge, (readback,)
        )
    events.write({"event": "publication-committed", "publication_id": str(publication_id)})
    normal.commit_normal_release_after_publication(plan, generation)
    events.write({"event": "unit-committed"})
    if mode in {"settle", "inj-13", "inj-14", "inj-14b"}:
        cleared = updater_handoff.clear(generation)
        events.write({"event": "cleared", "ok": cleared})
        require(cleared, "clear refused on a committed recovery")
    return 0


@contextmanager
def instrumentation(events: Events, mode: str, case_dir: Path) -> Generator[None, None, None]:
    """Base spies (journal writes, spawn calls, stop signals, selector CAS) plus
    this mode's fault injection, entered after the spies so injections wrap them."""
    backend = get_backend()
    journal_real = normal._write_normal_journal
    spawn_real = posixproc.new_session
    signal_real = backend.graceful_signal
    select_real = normal.select_pending_release

    def journal_spy(generation: str, journal: Any) -> Any:
        events.write({"event": "journal-write", "stage": journal.stage})
        return journal_real(generation, journal)

    def spawn_spy(
        name: str,
        cmd: str,
        cwd: Path,
        *,
        env: dict[str, str],
        stderr_append: Path | None = None,
        gate_fd: int | None = None,
        receipt: tuple[Path, str] | None = None,
    ) -> bool:
        events.write({"event": "spawn-call", "session": name})
        return spawn_real(
            name, cmd, cwd, env=env, stderr_append=stderr_append, gate_fd=gate_fd, receipt=receipt
        )

    def signal_spy(name: str, *, expected: SessionRecord | None = None) -> bool:
        events.write(
            {
                "event": "graceful-signal",
                "session": name,
                "expected_pid": None if expected is None else expected.pid,
                "expected_starttime": None if expected is None else expected.starttime,
            }
        )
        return signal_real(name, expected=expected)

    def select_spy(conn: Any, context: Any, unit: Any, previous: Any) -> Any:
        path = Path(unit.home) / "releases" / "current-release"
        before = path.read_bytes() if path.exists() else None
        result = select_real(conn, context, unit, previous)
        after = path.read_bytes() if path.exists() else None
        events.write({"event": "selector-cas", "wrote": before != after})
        return result

    with ExitStack() as stack:
        stack.enter_context(patch.object(normal, "_write_normal_journal", journal_spy))
        stack.enter_context(patch.object(posixproc, "new_session", spawn_spy))
        stack.enter_context(patch.object(backend, "graceful_signal", signal_spy))
        stack.enter_context(patch.object(normal, "select_pending_release", select_spy))
        apply_injection(stack, events, mode, case_dir)
        yield


def apply_injection(  # noqa: PLR0915 -- one flat dispatch per named injection window.
    stack: ExitStack, events: Events, mode: str, case_dir: Path
) -> None:
    if mode in {"success", "settle"}:
        return

    def fire() -> None:
        events.write({"event": "injection-fired", "mode": mode})

    if mode == "inj-1":

        def crash_select(*_args: object, **_kwargs: object) -> None:
            fire()
            raise SystemExit(77)

        stack.enter_context(patch.object(normal, "select_pending_release", crash_select))
        return
    if mode in {"inj-2a", "inj-2b"}:
        real_replace = Path.replace
        armed = {"once": True}

        def replace_hooked(self: Path, target: Any) -> Any:
            if armed["once"] and Path(target).name == "current-release":
                armed["once"] = False
                fire()
                raise SystemExit(77)
            return real_replace(self, target)

        stack.enter_context(patch.object(Path, "replace", replace_hooked))
        return
    if mode in {"inj-3", "inj-5", "inj-10"}:
        target_stage = {
            "inj-3": "selected",
            "inj-5": "bootstrap_stopped",
            "inj-10": "observed",
        }[mode]
        real_write = normal._write_normal_journal

        def journal_crash(generation: str, journal: Any) -> Any:
            if journal.stage == target_stage:
                fire()
                raise SystemExit(77)
            return real_write(generation, journal)

        stack.enter_context(patch.object(normal, "_write_normal_journal", journal_crash))
        return
    if mode == "inj-4":

        def wait_crash(*_args: object, **_kwargs: object) -> None:
            fire()
            raise SystemExit(77)

        stack.enter_context(patch.object(normal, "_wait_bootstrap_stopped", wait_crash))
        return
    if mode == "inj-6":

        def start_crash(*_args: object, **_kwargs: object) -> None:
            fire()
            raise SystemExit(77)

        stack.enter_context(patch.object(normal, "start_normal_service", start_crash))
        return
    if mode == "inj-7a":
        marker = case_dir / "marker-inj-7a"

        def blocked_spawn(*_args: object, **_kwargs: object) -> bool:
            marker.write_text("helper-not-started", encoding="utf-8")
            events.write({"event": "injection-blocked", "mode": mode})
            while True:
                time.sleep(30)

        stack.enter_context(patch.object(posixproc, "new_session", blocked_spawn))
        return
    if mode == "inj-8":
        real_gated = spawn_receipt.execute_gated_spawn

        def suppressed_gated(*args: Any, **kwargs: Any) -> Any:
            # The record write lives inside the gated spawn (the supervisor
            # writes it right after the fork); suppress it there, then crash.
            # The crash must fire OUTSIDE execute_gated_spawn: that call
            # adjudicates any exception as a helper report ("the receipt
            # adjudicates; the report is only a hint") and continues, so a
            # SystemExit raised inside it was swallowed and the release ran
            # to completion (round 3, attempt 1: exit 0, empty stderr). The
            # crash leaves the W2 state -- a live child with its birth
            # receipt and no record -- which the settle's phase-zero adoption
            # repairs (design 4.4, the one privileged repair path).
            with patch.object(SessionRecord, "write", no_record_write):
                real_gated(*args, **kwargs)
            fire()
            raise SystemExit(77)

        stack.enter_context(patch.object(spawn_receipt, "execute_gated_spawn", suppressed_gated))
        return
    if mode in {"inj-9a", "inj-9b"}:
        if mode == "inj-9a":

            def await_crash(*_args: object, **_kwargs: object) -> None:
                fire()
                raise SystemExit(77)

            # A freshly spawned service waits inside start_normal_service via
            # the release_services global; only an already-adopted service
            # reaches the name imported into the normal module, so patch both
            # and the crash lands on the first service's first readiness wait
            # (round 3, attempt 1: patching only the normal name never fired).
            stack.enter_context(
                patch.object(release_services, "await_normal_service_ready", await_crash)
            )
            stack.enter_context(patch.object(normal, "await_normal_service_ready", await_crash))
        else:
            real_observe = release_services.observe_normal_service
            rounds = {"n": 0}

            def observe_crash(*args: Any, **kwargs: Any) -> Any:
                rounds["n"] += 1
                if rounds["n"] >= 4:
                    fire()
                    raise SystemExit(77)
                return real_observe(*args, **kwargs)

            stack.enter_context(
                patch.object(release_services, "observe_normal_service", observe_crash)
            )
        return
    if mode == "inj-11":

        def readback_crash(*_args: object, **_kwargs: object) -> None:
            fire()
            raise SystemExit(77)

        stack.enter_context(patch.object(normal, "record_pending_unit_readback", readback_crash))
        return
    if mode == "inj-12":

        def commit_crash(*_args: object, **_kwargs: object) -> None:
            fire()
            raise SystemExit(77)

        stack.enter_context(
            patch.object(normal, "commit_normal_release_after_publication", commit_crash)
        )
        return
    if mode == "inj-13":

        def clear_crash(_generation: str) -> None:
            fire()
            raise SystemExit(77)

        stack.enter_context(patch.object(updater_handoff, "clear", clear_crash))
        return
    if mode in {"inj-14", "inj-14b"}:
        real_unlink = Path.unlink
        armed = {"once": True}

        def unlink_hooked(
            self: Path,
            missing_ok: bool = False,  # noqa: FBT001, FBT002 -- mirrors Path.unlink's signature.
        ) -> None:
            target = self == updater_handoff.bootstrap_state_path()
            if mode == "inj-14b" and armed["once"] and target:
                # Crash after the clear-time GC, before the first unlink: the
                # attempt directory is gone while both state files remain.
                armed["once"] = False
                fire()
                raise SystemExit(77)
            real_unlink(self, missing_ok=missing_ok)
            if mode == "inj-14" and armed["once"] and target:
                # Crash between the two unlinks: the bootstrap envelope is gone.
                armed["once"] = False
                fire()
                raise SystemExit(77)

        stack.enter_context(patch.object(Path, "unlink", unlink_hooked))
        return
    raise AssertionError(f"unknown fault mode {mode!r}")


def drive(mode: str, meta_path: Path) -> int:
    """One drive subprocess: fresh claim (or resume) + the chain, under injection."""
    meta: dict[str, Any] = json.loads(meta_path.read_text(encoding="utf-8"))
    case_dir = meta_path.parent
    events = Events(case_dir / f"events-{mode}.jsonl")
    home = Path(str(meta["home"])).resolve()
    generation = str(meta["generation"])
    require(
        sys.platform == "linux"
        and os.environ.get("GITHUB_ACTIONS") == "true"
        and home.is_relative_to(Path(os.environ["RUNNER_TEMP"]).resolve()),
        "normal release drive requires isolated Linux CI scratch",
    )
    plan = rebuild_plan(meta, home)
    if mode == "settle":
        if not updater_handoff.resume_bootstrap(generation, expected_session=_SESSION_LABEL):
            # The mid-clear crash (inj-14) can leave a dead handoff whose
            # bootstrap envelope is already unlinked: nothing is left to
            # resume. Production converges exactly this leftover through the
            # generic recovery (ops update recover): prove the dead lineage
            # allows generic recovery, then clear the generation. Mirror that
            # entry -- clear itself CAS-checks the generation and clearability.
            handoff = updater_handoff.read()
            if not updater_handoff.allows_generic_recovery(handoff):
                events.write({"event": "resume-refused"})
                return 4
            events.write({"event": "resume-refused", "reason": "bootstrap envelope unlinked"})
            cleared = updater_handoff.clear(generation)
            events.write({"event": "cleared", "ok": cleared, "via": "generic-recovery"})
            return 0 if cleared else 5
        events.write({"event": "resumed", "owner_pid": os.getpid()})
    else:
        updater_handoff.begin(expected_session=_SESSION_LABEL, generation=generation)
        require(
            updater_handoff.claim_running(generation, expected_session=_SESSION_LABEL),
            "drive could not claim the fresh handoff",
        )
        write_bootstrap_journal(plan, generation, events)
    with instrumentation(events, mode, case_dir):
        try:
            return run_flow(mode, plan, meta, events)
        except ReleaseRejectedError as exc:
            if meta.get("settle_refuse"):
                events.write({"event": "refused-as-expected", "error": str(exc)})
                return 3
            raise


# --- driver-side evidence checks ---------------------------------------------


def fixture_argv(meta: dict[str, Any], entry: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(meta["interpreter"]),
        "-B",
        "-c",
        FIXTURE_SOURCE,
        str(entry["port"]),
        str(entry.get("delay", 0.0)),
    )


def journal_stage() -> str | None:
    envelope = updater_handoff.read_bootstrap_recovery()
    if envelope is None:
        return None
    journal = cast("dict[str, Any]", envelope["journal"])
    nested = journal.get("normal_release")
    if nested is None:
        return None
    return str(cast("dict[str, Any]", nested)["stage"])


def receipts_for(home: Path, generation: str, session: str) -> list[spawn_receipt.SpawnReceipt]:
    directory = spawn_receipt.spawn_attempt_dir(home, generation)
    receipts: list[spawn_receipt.SpawnReceipt] = []
    if directory.exists():
        for path in sorted(directory.glob(f"{session}.*.receipt.json")):
            receipts.append(spawn_receipt.SpawnReceipt.model_validate_json(path.read_text()))
    return receipts


def write_stages(events: list[dict[str, Any]]) -> list[str]:
    return [str(entry["stage"]) for entry in events if entry.get("event") == "journal-write"]


def selector_writes(events: list[dict[str, Any]]) -> int:
    return sum(1 for entry in events if entry.get("event") == "selector-cas" and entry.get("wrote"))


def require_no_extra_effects(name: str, events: list[dict[str, Any]], *, selector_max: int) -> None:
    require(
        selector_writes(events) <= selector_max,
        f"[{name}] selector CAS rewrote the pointer more than {selector_max} time(s)",
    )


def check_signals(
    name: str, meta: dict[str, Any], events: list[dict[str, Any]], *, minimum: int
) -> None:
    calls = [entry for entry in events if entry.get("event") == "graceful-signal"]
    bootstrap_pid = int(meta["bootstrap"]["pid"])
    for entry in calls:
        require(
            entry.get("session") == "ava-ops" and entry.get("expected_pid") == bootstrap_pid,
            f"[{name}] stop signal without the retained bootstrap identity: {entry}",
        )
    require(len(calls) >= minimum, f"[{name}] stop signals {len(calls)} < {minimum}")


def check_instances(name: str, meta: dict[str, Any], home: Path, *, extra: bool) -> None:
    generation = str(meta["generation"])
    for entry in meta["services"]:
        session = str(entry["session"])
        argv = fixture_argv(meta, entry)
        live = stragglers_by_argv(argv)
        require(len(live) <= 1, f"[{name}] {session}: {len(live)} fixture instances {live}")
        listeners = port_listeners(int(entry["port"]))
        require(len(listeners) <= 1, f"[{name}] {session}: {len(listeners)} listeners")
        record = session_record(home, session)
        record_live = record is not None and observe_process(record_identity(record)) == "alive"
        if record is not None and record_live:
            require(
                live == [record.pid],
                f"[{name}] {session}: live record {record.pid} vs argv scan {live}",
            )
            # The design's 0/1 rule: a crash window may land mid-startup (the
            # fixture server not yet bound), so absence is legal here; a
            # present listener must be the recorded process. The settled check
            # (extra=True) keeps requiring exactly one.
            require(
                not listeners or listeners[0]["pid"] == record.pid,
                f"[{name}] {session}: listener ownership mismatch {listeners}",
            )
            # The clear-time GC retires receipts and gates by removing their
            # directory (design 4.5/5), so once the settled clear -- or a
            # mid-clear crash -- has run, a live record's gate can only probe a
            # fresh inert file. When the directory is gone the record, argv and
            # listener checks above are the surviving evidence (the GC itself is
            # pinned by check_pass2 and the inj-14 pair); while it still exists,
            # the live record must bind its gate and birth receipt. (Round 4:
            # the unconditional gate probe read every settled case as "free".)
            if spawn_receipt.spawn_attempt_dir(home, generation).exists():
                gate = spawn_receipt.session_lock_path(home, generation, session)
                require(
                    spawn_receipt.probe_session_lock_free(gate) is False,
                    f"[{name}] {session}: live service but the session gate is free",
                )
                births = [
                    item for item in receipts_for(home, generation, session) if item.kind == "birth"
                ]
                require(bool(births), f"[{name}] {session}: live service without a birth receipt")
                latest = births[-1]
                require(
                    latest.pid == record.pid and latest.starttime == record.starttime,
                    f"[{name}] {session}: birth vs record identity mismatch",
                )
        if extra:
            record_pid = record.pid if record is not None else None
            require(
                record_live
                and record_pid is not None
                and live == [record_pid]
                and len(listeners) == 1,
                f"[{name}] {session}: settled service is not exactly one live instance",
            )


def check_pass1(name: str, case: dict[str, Any], meta: dict[str, Any], home: Path) -> None:
    events = read_events(meta_path_for(meta) / f"events-{case['mode']}.jsonl")
    require(
        write_stages(events) == [str(item) for item in case["writes1"]],
        f"[{name}] pass1 journal writes {write_stages(events)} != {case['writes1']}",
    )
    expected_stage = case["stage1"]
    require(
        journal_stage() == expected_stage,
        f"[{name}] pass1 journal stage {journal_stage()!r} != {expected_stage!r}",
    )
    if name in {"inj-2a", "inj-2b"}:
        require(selector_writes(events) == 0, f"[{name}] injected selector write landed")
    if name == "inj-2b":
        require(
            not (home / "releases" / "current-release").exists(),
            f"[{name}] selector pointer exists after the pre-write crash",
        )
    if name == "inj-3":
        require(
            selector_writes(events) == 1, f"[{name}] selector CAS did not land before the crash"
        )
    generation = str(meta["generation"])
    if name in {"inj-6", "inj-7a"}:
        for entry in meta["services"]:
            kinds = [item.kind for item in receipts_for(home, generation, str(entry["session"]))]
            if name == "inj-6":
                require(not kinds, f"[{name}] unexpected receipts before the spawn: {kinds}")
            else:
                require(kinds == ["intent"], f"[{name}] expected the bare intent, saw {kinds}")
    if name == "inj-8":
        session = str(meta["services"][0]["session"])
        kinds = [item.kind for item in receipts_for(home, generation, session)]
        require(kinds == ["birth"], f"[{name}] expected a birth receipt, saw {kinds}")
        require(session_record(home, session) is None, f"[{name}] record was not suppressed")
        births = [item for item in receipts_for(home, generation, session) if item.kind == "birth"]
        require(
            observe_process(births[-1].expected_process()) == "alive",
            f"[{name}] W2 child is not alive",
        )
    if name in {"inj-13", "inj-14", "inj-14b"}:
        require(updater_handoff.state_path().exists(), f"[{name}] handoff vanished before clear")
        if name == "inj-13":
            require(
                updater_handoff.bootstrap_state_path().exists(),
                f"[{name}] bootstrap envelope missing before clear",
            )
        else:
            require(
                not spawn_receipt.spawn_attempt_dir(home, str(meta["generation"])).exists(),
                f"[{name}] clear-time GC did not retire the attempt directory",
            )
            if name == "inj-14":
                require(
                    not updater_handoff.bootstrap_state_path().exists(),
                    f"[{name}] mid-clear crash did not remove the bootstrap envelope first",
                )
            else:
                require(
                    updater_handoff.bootstrap_state_path().exists(),
                    f"[{name}] the GC-to-unlink crash was not the injected point",
                )
    if case.get("signals_min"):
        check_signals(name, meta, events, minimum=int(case["signals_min"]))


def check_pass2(name: str, case: dict[str, Any], meta: dict[str, Any]) -> None:
    events = read_events(meta_path_for(meta) / "events-settle.jsonl")
    if case.get("settle") == "refuse":
        require(
            write_stages(events) == [],
            f"[{name}] refused settle still wrote journal stages {write_stages(events)}",
        )
        require(
            journal_stage() == "waiting",
            f"[{name}] refused settle changed the stage to {journal_stage()!r}",
        )
        refusals = [entry for entry in events if entry.get("event") == "refused-as-expected"]
        require(
            len(refusals) == 1,
            f"[{name}] expected exactly one refusal record, saw {len(refusals)}",
        )
        reason = str(refusals[0].get("error", ""))
        require(
            "connection budget" in reason,
            f"[{name}] refusal reason {reason!r} does not name the exhausted budget",
        )
        return
    require(
        write_stages(events) == [str(item) for item in case["writes2"]],
        f"[{name}] settle journal writes {write_stages(events)} != {case['writes2']}",
    )
    # The committed stage is already pinned by the journal-write events
    # above; the envelope itself is gone by design here (the settle's clear
    # unlinks it), so the post-settle record of the clear half is the
    # cleared event. (Round 3, attempt 1: reading the stage after clear can
    # only ever see None -- the two old requirements excluded each other.)
    require(
        any(entry.get("event") == "cleared" and entry.get("ok") is True for entry in events),
        f"[{name}] settle did not record a successful clear",
    )
    if name == "inj-14":
        # The mid-clear crash left no envelope to resume: the settle must have
        # refused the resume and converged through the generic recovery.
        require(
            any(entry.get("event") == "resume-refused" for entry in events)
            and any(
                entry.get("event") == "cleared" and entry.get("via") == "generic-recovery"
                for entry in events
            ),
            f"[{name}] settle did not converge through the generic recovery",
        )
    require(
        not updater_handoff.state_path().exists()
        and not updater_handoff.bootstrap_state_path().exists(),
        f"[{name}] clear did not retire the handoff pair",
    )
    require(
        not spawn_receipt.spawn_attempt_dir(
            Path(str(meta["home"])), str(meta["generation"])
        ).exists(),
        f"[{name}] settled clear did not GC the attempt directory",
    )


def meta_path_for(meta: dict[str, Any]) -> Path:
    return Path(str(meta["case_dir"]))


def run_case(  # noqa: PLR0915 -- one bounded fixture lifecycle per case.
    case: dict[str, Any],
    conn: psycopg.Connection,
    namespace: str,
    home: Path,
    image: Any,
    schema_digest: str,
) -> dict[str, Any]:
    name = str(case["case"])
    case_dir = home.parent / "normal-release-cases" / name
    case_dir.mkdir(parents=True, exist_ok=True)
    generation = uuid4().hex
    session_names = ["ava-proof", "ava-proof-two"][: int(case.get("services", 1))]
    services = [
        {"session": session, "port": free_port(), "delay": float(case.get("delay", 0.0))}
        for session in session_names
    ]
    challenge_seconds = int(case.get("challenge", 600))
    ops_port = free_port()
    env = case_env(home, namespace, ops_port)
    outcome: dict[str, Any] = {"case": name, "ok": False, "opsPort": ops_port}
    try:
        holder = f"normal-proof-{name}"
        row = require_present(
            conn.execute(
                "UPDATE deployment_state SET holder=%s, acquired_at=clock_timestamp(),"
                " expires_at=clock_timestamp() + make_interval(secs => %s), target_sha=%s,"
                " managed_writer_evidence=NULL WHERE id=1 RETURNING acquired_at",
                (holder, float(challenge_seconds), _TARGET_SHA),
            ).fetchone(),
            "deployment fixture row is missing",
        )
        operation = RolloutIdentity(
            holder=holder, acquired_at=cast("datetime", row[0]), target_sha=_TARGET_SHA
        )
        now = datetime.now(UTC)
        if now <= operation.acquired_at:
            time.sleep(0.05)
            now = datetime.now(UTC)
        valid_until = now + timedelta(seconds=challenge_seconds)
        challenge = ObservationChallenge(challenge=uuid4(), valid_until=valid_until)
        context = PreparedObservation(
            expected=ExpectedUnitWriters(
                machine=_MACHINE,
                home=str(home),
                artifact_digest=image.digest,
                manifest_digest=image.manifest_digest,
                processes=(),
                sessions=(),
                launchers=(),
            ),
            operation=operation,
            challenge=challenge,
            schema_digest=schema_digest,
        )
        context_path = home / "run" / f"normal-context-{name}.json"
        private_json(context_path, context.model_dump_json())
        launch = "exec " + shlex.join(bootstrap_command(image, context_path))
        require(
            get_backend().new_session("ava-ops", launch, home, env=env, login_shell=False),
            "bootstrap observer session launch refused",
        )
        deadline = time.monotonic() + 30
        while True:
            if port_listeners(ops_port):
                break
            record = session_record(home, "ava-ops")
            if record is not None and observe_process(record_identity(record)) in {
                "exited",
                "identity_mismatch",
            }:
                raise AssertionError("bootstrap observer exited before serving: " + observer_tail())
            require(
                time.monotonic() < deadline,
                "bootstrap observer never served: " + observer_tail(),
            )
            time.sleep(0.05)
        record = require_present(
            session_record(home, "ava-ops"), "bootstrap observer record is missing"
        )
        require(
            observe_process(record_identity(record)) == "alive",
            "bootstrap observer vanished after serving",
        )
        bootstrap = asdict(record)
        receipt_path = prepare_unit_inventory(
            conn, image, home, _MACHINE, schema_digest=schema_digest
        )
        receipt_bytes = receipt_path.read_bytes()
        unit = PublishedUnit(
            machine=_MACHINE,
            home=str(home),
            prepared_receipt_digest=hashlib.sha256(receipt_bytes).hexdigest(),
            artifact_digest=image.digest,
            manifest_digest=image.manifest_digest,
            inventory_digest=str(json.loads(receipt_bytes)["inventory_digest"]),
        )
        candidate_digest = hashlib.sha256(f"candidate-{name}-{generation}".encode()).hexdigest()
        publication = WriterPublication(
            current=None,
            pending=PendingPublication(
                operation=operation,
                predecessor=None,
                candidate_digest=candidate_digest,
                challenge=challenge.challenge,
                units=(unit,),
                collection=ManagedWriterCollection(
                    operation=operation,
                    candidate_digest=candidate_digest,
                    challenge=challenge.challenge,
                    collected_at=now,
                    valid_until=valid_until,
                    units=(
                        ManagedUnitClosure(
                            unit=ManagedUnit(
                                machine=_MACHINE,
                                home=str(home),
                                inventory_digest=unit.prepared_receipt_digest,
                            ),
                            boot_id=uuid4(),
                            observer_instance=uuid4(),
                            observation_digest=hashlib.sha256(
                                b"closure:" + generation.encode()
                            ).hexdigest(),
                            outcome="old_writers_absent_relaunchers_fenced",
                        ),
                    ),
                ),
                normal_start_plan=NormalStartPlan(
                    schema_digest=schema_digest,
                    applied_names=_APPLIED_NAMES,
                    units=(
                        CandidateUnitPlan(
                            unit=unit,
                            services=tuple(
                                sorted(
                                    (
                                        prepared_service(image, entry, home).identity
                                        for entry in services
                                    ),
                                    key=lambda item: item.session,
                                )
                            ),
                            previous_selector_digest=None,
                            selector_digest=hashlib.sha256(selector_bytes(unit)).hexdigest(),
                        ),
                    ),
                ),
                migration=None,
                unit_readbacks=(),
            ),
        )
        conn.execute(
            "UPDATE deployment_state SET managed_writer_evidence=%s WHERE id=1",
            (Jsonb(publication.model_dump(mode="json")),),
        )
        request = normal.NormalReleaseRequest(
            context_path=str(context_path),
            unit=unit,
            previous_selector=None,
            predecessor=ExpectedProcess(
                pid=bootstrap["pid"],
                create_time=bootstrap["create_time"],
                starttime=bootstrap.get("starttime"),
            ),
        )
        request_path = home / "run" / f"normal-request-{name}.json"
        private_json(request_path, request.model_dump_json())
        meta: dict[str, Any] = {
            "case": name,
            "home": str(home),
            "case_dir": str(case_dir),
            "generation": generation,
            "artifact": image.digest,
            "manifest": image.manifest_digest,
            "schema_digest": schema_digest,
            "interpreter": str(image.interpreter),
            "cwd": str(image.cwd),
            "request_path": str(request_path),
            "context_path": str(context_path),
            "services": services,
            "bootstrap": bootstrap,
            "settle_refuse": bool(case.get("settle_refuse", False)),
            "challenge_seconds": challenge_seconds,
        }
        meta_path = case_dir / "drive-plan.json"
        meta_path.write_text(json.dumps(meta, sort_keys=True, indent=2), encoding="utf-8")

        marker = case_dir / "marker-inj-7a" if case["mode"] == "inj-7a" else None
        proc1 = run_drive(
            meta_path, str(case["mode"]), env, marker=marker, timeout=challenge_seconds + 120
        )
        expect1 = str(case.get("pass1", "crash77"))
        actual1 = (
            "killed"
            if proc1.returncode == -signal.SIGKILL
            else (
                "crash77"
                if proc1.returncode == 77
                else ("ok" if proc1.returncode == 0 else f"rc{proc1.returncode}")
            )
        )
        require(
            actual1 == expect1,
            f"[{name}] pass1 exit {proc1.returncode} != {expect1}; stderr={tail(proc1)}",
        )
        check_pass1(name, case, meta, home)
        # The settle's clear retires the envelope by design, so the stage is
        # readable only up to here: record what pass1 left behind (it is what
        # check_pass1 has just pinned), not a post-clear None.
        stage_after_pass1 = journal_stage()
        check_instances(name, meta, home, extra=False)

        if case.get("settle") == "refuse":
            remaining = (valid_until - datetime.now(UTC)).total_seconds()
            if remaining > 0:
                time.sleep(remaining + 4.0)
        proc2 = run_drive(meta_path, "settle", env, marker=None, timeout=challenge_seconds + 180)
        expect2 = 3 if case.get("settle") == "refuse" else 0
        require(
            proc2.returncode == expect2,
            f"[{name}] settle exit {proc2.returncode} != {expect2}; stderr={tail(proc2)}",
        )
        check_pass2(name, case, meta)
        events1 = read_events(case_dir / f"events-{case['mode']}.jsonl")
        events2 = read_events(case_dir / "events-settle.jsonl")
        all_events = events1 + events2
        if case.get("settle") != "refuse":
            require_no_extra_effects(name, all_events, selector_max=1)
            check_signals(name, meta, all_events, minimum=1)
            check_instances(name, meta, home, extra=True)
        else:
            require_no_extra_effects(name, all_events, selector_max=0)
            check_instances(name, meta, home, extra=False)
        outcome.update(
            {
                "ok": True,
                "pass1Exit": proc1.returncode,
                "settleExit": proc2.returncode,
                "stage1": stage_after_pass1,
                "writes1": write_stages(events1),
                "writes2": write_stages(events2),
                "selectorWrites": selector_writes(all_events),
                "stopSignals": len([e for e in all_events if e.get("event") == "graceful-signal"]),
            }
        )
    finally:
        exc = sys.exc_info()[1]
        if exc is not None:  # a failing case still leaves its CI artifact trail
            outcome["error"] = f"{type(exc).__name__}: {exc}"
            outcome["observerTail"] = observer_tail(2000)
            record = session_record(home, "ava-ops")
            outcome["observerRecord"] = asdict(record) if record is not None else None
            outcome["opsListeners"] = port_listeners(ops_port)
        (home.parent / f"normal-release-observation-{name}.json").write_text(
            json.dumps(outcome, indent=2), encoding="utf-8"
        )
        retire_case(home, generation, session_names)
    return outcome


def observer_tail(limit: int = 4000) -> str:
    path = posixproc.session_log_path("ava-ops")
    if not path.exists():
        return "(no observer log)"
    return path.read_text(encoding="utf-8", errors="replace")[-limit:]


def tail(proc: subprocess.CompletedProcess[bytes], limit: int = 4000) -> str:
    return proc.stderr.decode(errors="replace")[-limit:]


def run_drive(
    meta_path: Path,
    mode: str,
    env: dict[str, str],
    *,
    marker: Path | None,
    timeout: float,
) -> subprocess.CompletedProcess[bytes]:
    meta: dict[str, Any] = json.loads(meta_path.read_text(encoding="utf-8"))
    argv = [
        str(meta["interpreter"]),
        "-I",
        "-B",
        "-X",
        "utf8",
        str(Path(__file__).resolve()),
        "--fault-worker",
        mode,
        str(meta_path),
    ]
    process = subprocess.Popen(  # noqa: S603 -- retained interpreter and copied CI-only proof.
        argv, cwd=str(meta["cwd"]), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if marker is not None:
        deadline = time.monotonic() + 120
        while not marker.exists():
            require(process.poll() is None, f"{mode}: drive exited before its marker")
            require(time.monotonic() < deadline, f"{mode}: drive never reached its marker")
            time.sleep(0.05)
        process.kill()
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise AssertionError(
            f"{mode}: drive exceeded its watchdog; stderr={stderr.decode(errors='replace')[-4000:]}"
        ) from None
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def retire_case(home: Path, generation: str, sessions: list[str]) -> None:
    """CI-scratch retirement: evidence is already copied out; remove processes + files."""
    backend = get_backend()
    for session in [*sessions, "ava-ops"]:
        if backend.has_session(session):
            backend.kill_session(session, graceful=False, timeout=10)
        record = session_record(home, session)
        if record is not None:
            verdict = observe_process(record_identity(record))
            require(
                verdict in {"exited", "identity_mismatch"},
                f"teardown left {session} alive: {verdict}",
            )
            (home / "run" / "sessions" / f"{session}.json").unlink(missing_ok=True)
    shutil.rmtree(home / "run" / "updater-spawn" / generation, ignore_errors=True)
    updater_handoff.state_path().unlink(missing_ok=True)
    updater_handoff.bootstrap_state_path().unlink(missing_ok=True)
    (home / "releases" / "current-release").unlink(missing_ok=True)


def create_namespace(conn: psycopg.Connection, namespace: str, home: Path) -> None:
    conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace)))
    conn.execute("SELECT set_config('search_path', %s, false)", (namespace,))
    conn.execute("CREATE TABLE machine_units(machine_name text, home text)")
    conn.execute("INSERT INTO machine_units VALUES (%s, %s)", (_MACHINE, str(home)))
    conn.execute("CREATE TABLE machines(name text)")
    conn.execute("INSERT INTO machines VALUES (%s)", (_MACHINE,))
    # `lock_rollout` predicates `settle_hosts IS NULL` and the lease read selects
    # the settle trio; the real schema has carried it since the initial release.
    conn.execute(
        "CREATE TABLE deployment_state(id int, phase text, kind text, holder text,"
        " acquired_at timestamptz, expires_at timestamptz, target_sha text,"
        " managed_writer_evidence jsonb, settle_hosts text[], settle_note text,"
        " settle_started_at timestamptz)"
    )
    conn.execute(
        "INSERT INTO deployment_state(id, phase, kind, holder, acquired_at, expires_at,"
        " target_sha) VALUES (1, 'updating', 'rollout', 'proof-idle',"
        " clock_timestamp(), clock_timestamp() + interval '1 hour', %s)",
        (_TARGET_SHA,),
    )
    conn.execute("CREATE TABLE schema_migrations(name text)")
    for name in _APPLIED_NAMES:
        conn.execute("INSERT INTO schema_migrations VALUES (%s)", (name,))


CASES: tuple[dict[str, Any], ...] = (
    {
        "case": "success",
        "mode": "success",
        "services": 1,
        "pass1": "ok",
        "writes1": [
            "waiting",
            "selected",
            "bootstrap_stopped",
            "starting",
            "observed",
            "committed",
        ],
        "writes2": [],
        "stage1": "committed",
    },
    {
        "case": "inj-1",
        "mode": "inj-1",
        "services": 1,
        "writes1": ["waiting"],
        "writes2": ["selected", "bootstrap_stopped", "starting", "observed", "committed"],
        "stage1": "waiting",
    },
    {
        "case": "inj-2a",
        "mode": "inj-2a",
        "services": 1,
        "writes1": ["waiting"],
        "writes2": ["selected", "bootstrap_stopped", "starting", "observed", "committed"],
        "stage1": "waiting",
    },
    {
        "case": "inj-2b",
        "mode": "inj-2b",
        "services": 1,
        # Crash lands at the selector write after real readiness: release
        # verification plus roster readiness cost ~1-2 min per case on CI
        # runners, so the budget must outlast pass1 while still expiring
        # before settle (the harness sleeps out the remainder). 20s refused
        # the observer itself on its exhausted budget (round 2).
        "challenge": 300,
        "writes1": ["waiting"],
        "writes2": [],
        "stage1": "waiting",
        "settle": "refuse",
        "settle_refuse": True,
    },
    {
        "case": "inj-3",
        "mode": "inj-3",
        "services": 1,
        "writes1": ["waiting"],
        "writes2": ["selected", "bootstrap_stopped", "starting", "observed", "committed"],
        "stage1": "waiting",
    },
    {
        "case": "inj-4",
        "mode": "inj-4",
        "services": 1,
        "writes1": ["waiting", "selected"],
        "writes2": ["bootstrap_stopped", "starting", "observed", "committed"],
        "stage1": "selected",
        "signals_min": 1,
    },
    {
        "case": "inj-5",
        "mode": "inj-5",
        "services": 1,
        "writes1": ["waiting", "selected"],
        "writes2": ["bootstrap_stopped", "starting", "observed", "committed"],
        "stage1": "selected",
    },
    {
        "case": "inj-6",
        "mode": "inj-6",
        "services": 1,
        "writes1": ["waiting", "selected", "bootstrap_stopped", "starting"],
        "writes2": ["starting", "observed", "committed"],
        "stage1": "starting",
    },
    {
        "case": "inj-7a",
        "mode": "inj-7a",
        "services": 1,
        "pass1": "killed",
        "writes1": ["waiting", "selected", "bootstrap_stopped", "starting"],
        "writes2": ["starting", "observed", "committed"],
        "stage1": "starting",
    },
    {
        "case": "inj-8",
        "mode": "inj-8",
        "services": 1,
        "writes1": ["waiting", "selected", "bootstrap_stopped", "starting"],
        "writes2": ["observed", "committed"],
        "stage1": "starting",
    },
    {
        "case": "inj-9a",
        "mode": "inj-9a",
        "services": 2,
        "writes1": ["waiting", "selected", "bootstrap_stopped", "starting"],
        "writes2": ["starting", "observed", "committed"],
        "stage1": "starting",
    },
    {
        "case": "inj-9b",
        "mode": "inj-9b",
        "services": 1,
        "delay": 3.0,
        "writes1": ["waiting", "selected", "bootstrap_stopped", "starting"],
        "writes2": ["observed", "committed"],
        "stage1": "starting",
    },
    {
        "case": "inj-10",
        "mode": "inj-10",
        "services": 2,
        "writes1": ["waiting", "selected", "bootstrap_stopped", "starting", "starting"],
        "writes2": ["observed", "committed"],
        "stage1": "starting",
    },
    {
        "case": "inj-11",
        "mode": "inj-11",
        "services": 1,
        "writes1": ["waiting", "selected", "bootstrap_stopped", "starting", "observed"],
        "writes2": ["committed"],
        "stage1": "observed",
    },
    {
        "case": "inj-12",
        "mode": "inj-12",
        "services": 1,
        "writes1": ["waiting", "selected", "bootstrap_stopped", "starting", "observed"],
        "writes2": ["committed"],
        "stage1": "observed",
    },
    {
        "case": "inj-13",
        "mode": "inj-13",
        "services": 1,
        "writes1": [
            "waiting",
            "selected",
            "bootstrap_stopped",
            "starting",
            "observed",
            "committed",
        ],
        "writes2": [],
        "stage1": "committed",
    },
    {
        "case": "inj-14",
        "mode": "inj-14",
        "services": 1,
        "writes1": [
            "waiting",
            "selected",
            "bootstrap_stopped",
            "starting",
            "observed",
            "committed",
        ],
        "writes2": [],
        # The crash lands after the bootstrap envelope's unlink (the second
        # clear crash point): no readable stage remains by design.
        "stage1": None,
    },
    {
        "case": "inj-14b",
        "mode": "inj-14b",
        "services": 1,
        "writes1": [
            "waiting",
            "selected",
            "bootstrap_stopped",
            "starting",
            "observed",
            "committed",
        ],
        "writes2": [],
        "stage1": "committed",
    },
)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--fault-worker":
        raise SystemExit(drive(sys.argv[2], Path(sys.argv[3])))
    artifact, manifest, schema_digest = sys.argv[1:]
    home = Path(os.environ["AVA_HOME"]).resolve()
    require(
        sys.platform == "linux"
        and os.environ.get("GITHUB_ACTIONS") == "true"
        and home.is_relative_to(Path(os.environ["RUNNER_TEMP"]).resolve()),
        "normal release proof requires isolated Linux CI scratch",
    )
    image = verify_release(
        home / "releases",
        artifact,
        manifest_digest=manifest,
        platform_tag=platform.platform(),
        schema_digest=schema_digest,
    )
    # CI scratch: retire any handoff left by an earlier proof in this private home
    # (a retained failure envelope must not block this suite). The serving pointer
    # is borrowed state: prove_runtime_prepare writes a sentinel there and reads it
    # back at two later points (its preparation checks), and the sibling proofs
    # leave it as they find it -- so the suite snapshots the exact bytes here and
    # restores them in the finally.
    selector_path = home / "releases" / "current-release"
    selector_original = selector_path.read_bytes() if selector_path.exists() else None
    updater_handoff.state_path().unlink(missing_ok=True)
    updater_handoff.bootstrap_state_path().unlink(missing_ok=True)
    selector_path.unlink(missing_ok=True)
    # The unit's launcher inventory must be non-empty ("complete coverage");
    # install the one real CI-scratch registration, the same way the updater hop
    # proof does, and restore the previous table at the end of the run.
    original_cron = read_crontab(datetime.now(UTC) + timedelta(seconds=30))
    require(not original_cron.strip(), "proof refuses to replace another CI job")
    install_cron(f"@reboot AVA_HOME={home} /usr/bin/true # ava-normal-release-proof\n".encode())
    namespace = "normal_" + uuid4().hex
    unit_env_path = home / ".env"
    require(unit_env_path.exists(), "unit .env is missing; runtime-prepare must have written it")
    unit_env_original = unit_env_path.read_bytes()
    conn = psycopg.connect(
        make_conninfo(os.environ["AVA_DB_URL"], options=f"-csearch_path={namespace}"),
        autocommit=True,
    )
    violations: list[str] = []
    summary: dict[str, Any] = {}
    try:
        # Scoped for the whole run: config-loading entries (the drives) let the
        # unit's .env win over ambient cluster-scope values (env authority), so
        # the file must carry this proof's namespace; restored in the finally.
        unit_env_path.write_bytes(scoped_unit_env(unit_env_original, namespace))
        create_namespace(conn, namespace, home)
        for case in CASES:
            name = str(case["case"])
            print(f"[normal-release] case {name}: driving", flush=True)
            try:
                summary[name] = run_case(case, conn, namespace, home, image, schema_digest)
                print(f"[normal-release] case {name}: ok", flush=True)
            except Exception as exc:  # collect per-case, finish the run.
                violations.append(f"{name}: {type(exc).__name__}: {exc}")
                summary[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                print(
                    f"[normal-release] case {name}: FAILED {type(exc).__name__}: {exc}",
                    flush=True,
                )
        (home.parent / "normal-release-proof.json").write_text(
            json.dumps(
                {
                    "chainsDriven": True,
                    "realGatedFork": True,
                    "realBirthReceipts": True,
                    "kernelSessions": True,
                    "injections": len([item for item in CASES if item.get("mode") != "success"]),
                    "replaysPerCase": 2,
                    "violations": violations,
                    "cases": summary,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        if violations:
            raise AssertionError("normal release proof violations: " + "; ".join(violations))
    finally:
        install_cron(original_cron)
        unit_env_path.write_bytes(unit_env_original)
        if selector_original is None:
            selector_path.unlink(missing_ok=True)
        else:
            selector_path.write_bytes(selector_original)
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(namespace)))
        conn.close()


if __name__ == "__main__":
    main()
