"""Prepared normal service commands and native/health readbacks for the updater.

The service roster is the existing ops specification, including plugin services.
Unsupported readiness transports reject during preparation, before any stop.
This module never runs converge, scaffold, installers, or a source fallback.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import psutil
import psycopg

from cli.commands._release_selector import pending_transaction, verify_unit_image
from cli.commands._session_lifecycle import _service_extra_env
from ops.spec import ServiceSpec, services_for_capabilities_annotated
from services.agent_ops.bootstrap import PreparedObservation
from shared import spawn_receipt
from shared.config import settings
from shared.machine import machine_role
from shared.managed_writer_activation import (
    NormalServiceReadback,
    SelectorReadback,
    require_pending_candidate_start,
)
from shared.managed_writer_observation import ExpectedProcess, observe_process
from shared.managed_writer_publication import NormalService, PublishedUnit
from shared.proc_tree import stable_create_time
from shared.runtime_interpreter import runtime_venv
from shared.runtime_publication_input import PreparationReceipt
from shared.runtime_release import ReleaseRejectedError, VerifiedRelease
from shared.runtime_service_identity import NormalRuntimeIdentity
from shared.session_backend import get_backend
from shared.session_env import forward_env_dict
from shared.session_record import SessionRecord, pid_starttime_ticks
from shared.updater_recovery import SpawnAttempt
from shared.verified_file import regular_bytes


@dataclass(frozen=True)
class PreparedService:
    identity: NormalService
    spec: ServiceSpec
    argv: tuple[str, ...]
    cwd: Path
    environment: dict[str, str]  # Private child transport only; never serialize to a receipt.


def normal_spawn_command(prepared: PreparedService) -> str:
    """The single source of the exact command string one service launch uses.

    The journal attempt's ``cmd_digest``, the gated launch, the birth receipt
    and the session-record cross-check must all describe the same string, so it
    is computed in exactly one place (design #4117 C1's single spawn exit).
    """
    return "exec " + shlex.join(prepared.argv)


def _command(  # noqa: PLR0915 — one ordered fail-closed command admission boundary.
    spec: ServiceSpec, image: VerifiedRelease
) -> PreparedService:
    tokens = shlex.split(spec.cmd)
    public: dict[str, str] = {}
    while tokens and "=" in tokens[0]:
        key, value = tokens.pop(0).split("=", 1)
        if key not in {"NODE_ENV", "HOSTNAME", "PORT"}:
            raise ReleaseRejectedError("service command has an unsupported environment prefix")
        public[key] = value
    if tokens and tokens[0] == "exec":
        tokens.pop(0)
    if not tokens or any(token in {";", "&&", "||", "|", ">", "&"} for token in tokens):
        raise ReleaseRejectedError("normal release requires a direct executable command")
    executable = Path(tokens[0]).resolve(strict=True)
    if not Path(tokens[0]).is_absolute() or not executable.is_relative_to(image.root):
        raise ReleaseRejectedError("service executable is outside the retained image")
    module: str | None = None
    entrypoint = executable
    if "-m" in tokens:
        module_index = tokens.index("-m")
        if module_index + 1 >= len(tokens):
            raise ReleaseRejectedError("service Python module is absent")
        module = tokens[module_index + 1]
        if not all(part.isidentifier() for part in module.split(".")):
            raise ReleaseRejectedError("service Python module is invalid")
        import shared

        package_root = Path(shared.__file__).resolve().parent.parent
        relative = Path(*module.split("."))
        candidates = (
            package_root / relative.with_suffix(".py"),
            package_root / relative / "__main__.py",
        )
        matches = [path for path in candidates if path.is_file()]
        if len(matches) != 1:
            raise ReleaseRejectedError("service module has no unique retained entry point")
        entrypoint = matches[0].resolve(strict=True)
        if "-B" not in tokens:
            tokens.insert(1, "-B")
    elif spec.session == "frontend":
        if len(tokens) < 2:
            raise ReleaseRejectedError("frontend entry point is absent")
        entrypoint = Path(tokens[1]).resolve(strict=True)
    elif spec.session != "otel-collector":
        raise ReleaseRejectedError("native service has no retained readiness adapter")
    if not entrypoint.is_relative_to(image.root):
        raise ReleaseRejectedError("service entry point escapes the image")
    if spec.curl_url is None and not (spec.session == "otel-collector" and spec.identity_probe):
        raise ReleaseRejectedError(f"normal readiness transport is unsupported: {spec.session}")
    if spec.curl_url is not None:
        parsed = urlsplit(spec.curl_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"localhost", "127.0.0.1"}
            or parsed.port is None
        ):
            raise ReleaseRejectedError(
                "normal service readiness must use its explicit loopback port"
            )
    public.update(_service_extra_env(spec))
    # Database credentials are forwarded privately, not hashed into the public command plan.
    command_view = {
        "argv": tokens,
        "cwd": str(image.cwd),
        "public_environment": {key: value for key, value in public.items() if key != "AVA_DB_URL"},
    }
    command_digest = hashlib.sha256(json.dumps(command_view, sort_keys=True).encode()).hexdigest()
    environment = forward_env_dict()
    environment.update(public)
    environment["AVA_HOME"] = str(image.root.parent.parent)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return PreparedService(
        NormalService(
            session=f"ava-{spec.session}",
            module=module,
            executable=str(executable),
            entrypoint=str(entrypoint),
            command_digest=command_digest,
        ),
        spec,
        tuple(tokens),
        image.cwd,
        environment,
    )


def prepare_normal_services(unit: PublishedUnit, schema_digest: str) -> tuple[PreparedService, ...]:
    """Reconcile the full sealed roster with the actual loaded service discovery."""
    image = verify_unit_image(unit, schema_digest)
    if runtime_venv() != image.root / "venv":
        raise ReleaseRejectedError("normal preparation is not running in the candidate image")
    receipt = PreparationReceipt.model_validate_json(
        regular_bytes(
            Path(unit.home) / "run" / f"release-inventory-{unit.prepared_receipt_digest}.json"
        )
    )
    if receipt.inventory_digest != unit.inventory_digest:
        raise ReleaseRejectedError("normal service receipt inventory changed")
    roster = services_for_capabilities_annotated(machine_role())
    actual = sorted(
        (
            {"session": spec.session, "requires_db": spec.requires_db, "gate": gate}
            for spec, gate in roster
        ),
        key=lambda item: str(item["session"]),
    )
    if actual != [item.model_dump() for item in receipt.services]:
        raise ReleaseRejectedError("normal service roster changed since preparation")
    # Pin the authored dependency order while all discovery is still pre-stop.
    prepared = tuple(_command(spec, image) for spec, gate in roster if gate is None)
    if not prepared:
        raise ReleaseRejectedError("empty normal service roster")
    return prepared


def _record(home: Path, name: str) -> SessionRecord:
    raw = json.loads(regular_bytes(home / "run/sessions" / f"{name}.json"))
    return SessionRecord(**raw)


def _native(process: psutil.Process) -> ExpectedProcess:
    return ExpectedProcess(
        pid=process.pid,
        create_time=stable_create_time(process),
        starttime=pid_starttime_ticks(process.pid),
    )


def _health_bytes(url: str, valid_until: datetime) -> bytes:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise ReleaseRejectedError("normal readiness URL is not loopback HTTP")
    budget = (valid_until - datetime.now(UTC)).total_seconds()
    if budget <= 0:
        raise ReleaseRejectedError("normal readback challenge expired")
    request = urllib.request.Request(url)  # noqa: S310 — scheme and loopback host checked above.

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args: object, **_kwargs: object) -> None:
            raise urllib.error.URLError("normal readiness does not follow redirects")

    with urllib.request.build_opener(NoRedirect).open(
        request, timeout=min(2.0, budget)
    ) as response:
        encoded = response.read(1024 * 1024 + 1)
        if response.status != 200 or response.url != url or len(encoded) > 1024 * 1024:
            raise ReleaseRejectedError("normal health response is not bounded and healthy")
    return encoded


def observe_normal_service(
    prepared: PreparedService,
    selector: SelectorReadback,
    context: PreparedObservation,
    record: SessionRecord,
) -> NormalServiceReadback:
    """Match the exact new session and its actual listening child, never a bare 200."""
    home = Path(selector.unit.home)
    if _record(home, prepared.identity.session) != record:
        raise ReleaseRejectedError("normal service session was replaced")
    supervisor = ExpectedProcess(
        pid=record.pid, create_time=record.create_time, starttime=record.starttime
    )
    if observe_process(supervisor) != "alive":
        raise ReleaseRejectedError("normal service supervisor is not exactly alive")
    parent = psutil.Process(record.pid)
    candidates = (parent, *parent.children(recursive=True))
    port = prepared.spec.tcp_port
    if prepared.spec.curl_url is not None:
        port = urlsplit(prepared.spec.curl_url).port
    if port is None:
        raise ReleaseRejectedError("normal service listener port is unknown")
    listeners = [
        process
        for process in candidates
        if any(
            item.status == psutil.CONN_LISTEN and item.laddr.port == port
            for item in process.net_connections(kind="tcp")
        )
    ]
    if len(listeners) != 1:
        raise ReleaseRejectedError("normal service has no unique owned listener")
    child = listeners[0]
    identity = _native(child)
    if Path(child.exe()).resolve(strict=True) != Path(prepared.identity.executable):
        raise ReleaseRejectedError("normal listener loaded another executable")
    if tuple(child.cmdline()) != prepared.argv:
        raise ReleaseRejectedError("normal listener command differs from its prepared command")
    loaded_module: str | None = None
    payload: dict[str, object]
    if prepared.spec.curl_url is not None:
        encoded = _health_bytes(prepared.spec.curl_url, context.challenge.valid_until)
        if prepared.identity.module is not None:
            decoded = json.loads(encoded)
            if not isinstance(decoded, dict):
                raise ReleaseRejectedError("normal health response is not an object")
            payload = cast(dict[str, object], decoded)
            runtime = NormalRuntimeIdentity.model_validate(payload["runtime"])
            if (
                runtime.process != identity
                or runtime.home != selector.unit.home
                or runtime.artifact_digest != selector.unit.artifact_digest
                or runtime.manifest_digest != selector.unit.manifest_digest
                or runtime.module_path != prepared.identity.entrypoint
                or runtime.module_name
                not in {prepared.identity.module, prepared.identity.module + ".__main__"}
                or payload["readiness"] != "ok"
            ):
                raise ReleaseRejectedError(
                    "normal health identity differs from native/image evidence"
                )
            loaded_module = runtime.module_path
        else:
            payload = {"http_sha256": hashlib.sha256(encoded).hexdigest()}
    else:
        probe = prepared.spec.identity_probe
        if probe is None or probe().verdict.value != "alive":
            raise ReleaseRejectedError("native service protocol probe failed")
        payload = {"native_protocol": prepared.spec.session}
    if observe_process(identity) != "alive" or _record(home, prepared.identity.session) != record:
        raise ReleaseRejectedError("normal process changed during health observation")
    return NormalServiceReadback(
        service=prepared.identity,
        supervisor=supervisor,
        child=identity,
        loaded_module=loaded_module,
        executable=prepared.identity.executable,
        entrypoint=prepared.identity.entrypoint,
        artifact_digest=selector.unit.artifact_digest,
        manifest_digest=selector.unit.manifest_digest,
        readiness="normal",
        challenge=context.challenge.challenge,
        observed_at=datetime.now(UTC),
        valid_until=context.challenge.valid_until,
        observation_digest=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
    )


def _require_attempt(
    attempt: SpawnAttempt, prepared: PreparedService, home: Path, generation: str
) -> str:
    """Bind one journal attempt to its prepared service and compute its command.

    The journal entry is the durable intent; this refuses any attempt whose
    session, command digest, cwd or gate/receipt paths do not describe exactly
    this prepared service and generation — an attempt written for anything
    else must never be executed here.
    """
    command = normal_spawn_command(prepared)
    gate = spawn_receipt.session_lock_path(home, generation, attempt.session)
    receipt = spawn_receipt.receipt_path(home, generation, attempt.session, attempt.nonce)
    if (
        attempt.session != prepared.identity.session
        or attempt.cmd_digest != hashlib.sha256(command.encode()).hexdigest()
        or attempt.cwd != str(prepared.cwd)
        or attempt.spawn_lock_path != gate.relative_to(home).as_posix()
        or attempt.receipt_path != receipt.relative_to(home).as_posix()
    ):
        raise ReleaseRejectedError("normal spawn attempt does not bind its prepared service")
    return command


def adopt_birth_record(
    home: Path,
    prepared: PreparedService,
    receipt: spawn_receipt.SpawnReceipt,
    *,
    generation: str,
) -> SessionRecord:
    """Cross-check one live birth against its session record (repairing W2 only).

    The record write can be lost between the child's birth receipt and the
    spawner's record write (the W2 window): a genuinely MISSING record is
    repaired from the receipt plus the prepared plan — the one privileged
    repair path (design §4.4). A damaged record is never missing (it raises)
    and a mismatched record is never overwritten; both refuse.
    """
    command = normal_spawn_command(prepared)
    session = prepared.identity.session
    try:
        record = spawn_receipt.read_session_record(home, session)
        if record is None:
            record = spawn_receipt.write_recovered_record(
                home,
                session,
                receipt,
                command=command,
                cwd=prepared.cwd,
                generation=generation,
            )
        elif not (
            spawn_receipt.record_matches_receipt(record, receipt)
            and record.cmd == command
            and record.cwd == str(prepared.cwd)
        ):
            raise ReleaseRejectedError("normal session record does not identify the birth receipt")
    except (
        spawn_receipt.SpawnExitedError,
        spawn_receipt.SpawnEvidenceInvalidError,
    ) as exc:
        raise ReleaseRejectedError(f"normal birth adoption failed closed: {exc}") from exc
    return record


def await_normal_service_ready(
    conn: psycopg.Connection,
    context: PreparedObservation,
    selector: SelectorReadback,
    prepared: PreparedService,
    record: SessionRecord,
) -> NormalServiceReadback:
    """Observe one exact running service until it is fully ready.

    Every round revalidates the same pending authority before observing; the
    loop is bounded by the prepared challenge and never renews it. A candidate
    that exits before readiness refuses — its dead identity is the caller's
    adjudication input, never a silent retry here.
    """
    while datetime.now(UTC) < context.challenge.valid_until:
        with pending_transaction(conn, context):
            require_pending_candidate_start(
                conn, context.operation, context.challenge.challenge, selector, prepared.identity
            )
        try:
            result = observe_normal_service(prepared, selector, context, record)
        except (OSError, psutil.Error, ReleaseRejectedError):
            if (
                observe_process(
                    ExpectedProcess(
                        pid=record.pid, create_time=record.create_time, starttime=record.starttime
                    )
                )
                != "alive"
            ):
                raise ReleaseRejectedError("normal candidate exited before readiness") from None
            time.sleep(
                min(
                    0.2, max(0, (context.challenge.valid_until - datetime.now(UTC)).total_seconds())
                )
            )
            continue
        with pending_transaction(conn, context):
            require_pending_candidate_start(
                conn, context.operation, context.challenge.challenge, selector, prepared.identity
            )
        return result
    raise ReleaseRejectedError("normal service did not become ready within its original challenge")


def start_normal_service(
    conn: psycopg.Connection,
    context: PreparedObservation,
    selector: SelectorReadback,
    prepared: PreparedService,
    *,
    attempt: SpawnAttempt,
    generation: str,
) -> NormalServiceReadback:
    """The updater's exact gated service start; no agent permission.

    The caller already adjudicated any displaced attempt (I8) and wrote the
    journal ``starting`` entry carrying ``attempt`` before this call — the
    journal is the durable intent, and this function is only the effect plus
    its verification: fresh authority, the per-session gate, the pre-exec
    birth receipt, the record cross-check, then readiness. Every adjudicated
    non-alive or ambiguous spawn outcome refuses the release here; it never
    retries.
    """
    home = Path(selector.unit.home)
    command = _require_attempt(attempt, prepared, home, generation)
    with pending_transaction(conn, context):
        require_pending_candidate_start(
            conn, context.operation, context.challenge.challenge, selector, prepared.identity
        )
    if datetime.now(UTC) >= context.challenge.valid_until:
        raise ReleaseRejectedError("normal start challenge expired before spawn")
    backend = get_backend()
    if backend.has_session(prepared.identity.session):
        raise ReleaseRejectedError("normal start refuses an existing or unaccounted session")
    remaining = (context.challenge.valid_until - datetime.now(UTC)).total_seconds()
    wait_budget = min(settings.gateway.update_spawn_ambiguity_wait_seconds, max(0.0, remaining / 2))
    try:
        receipt = spawn_receipt.execute_gated_spawn(
            backend,
            name=prepared.identity.session,
            command=command,
            workdir=prepared.cwd,
            env=prepared.environment,
            home=home,
            generation=generation,
            machine=selector.unit.machine,
            nonce=attempt.nonce,
            wait_budget=wait_budget,
        )
        record = adopt_birth_record(home, prepared, receipt, generation=generation)
    except (
        spawn_receipt.SpawnRefusedError,
        spawn_receipt.SpawnNotCompletedError,
        spawn_receipt.SpawnExitedError,
        spawn_receipt.SpawnAmbiguousError,
        spawn_receipt.SpawnEvidenceInvalidError,
    ) as exc:
        raise ReleaseRejectedError(f"exact normal service spawn failed closed: {exc}") from exc
    return await_normal_service_ready(conn, context, selector, prepared, record)
