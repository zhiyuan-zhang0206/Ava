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
from shared.machine import machine_role
from shared.managed_writer_activation import (
    NormalServiceReadback,
    SelectorReadback,
    require_pending_candidate_start,
)
from shared.managed_writer_observation import ExpectedProcess, observe_process
from shared.managed_writer_publication import NormalService, PublishedUnit
from shared.runtime_interpreter import runtime_venv
from shared.runtime_publication_input import PreparationReceipt
from shared.runtime_release import ReleaseRejectedError, VerifiedRelease
from shared.runtime_service_identity import NormalRuntimeIdentity
from shared.session_backend import get_backend
from shared.session_env import forward_env_dict
from shared.session_record import SessionRecord, pid_starttime_ticks
from shared.verified_file import regular_bytes


@dataclass(frozen=True)
class PreparedService:
    identity: NormalService
    spec: ServiceSpec
    argv: tuple[str, ...]
    cwd: Path
    environment: dict[str, str]  # Private child transport only; never serialize to a receipt.


def _command(spec: ServiceSpec, image: VerifiedRelease) -> PreparedService:
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
        module = tokens[tokens.index("-m") + 1]
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
        create_time=process.create_time(),
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


def start_normal_service(
    conn: psycopg.Connection,
    context: PreparedObservation,
    selector: SelectorReadback,
    prepared: PreparedService,
) -> NormalServiceReadback:
    """The existing updater's exact service-only start; no agent permission."""
    with pending_transaction(conn, context):
        require_pending_candidate_start(
            conn, context.operation, context.challenge.challenge, selector, prepared.identity
        )
    if datetime.now(UTC) >= context.challenge.valid_until:
        raise ReleaseRejectedError("normal start challenge expired before spawn")
    backend = get_backend()
    if backend.has_session(prepared.identity.session):
        raise ReleaseRejectedError("normal start refuses an existing or unaccounted session")
    started = time.time()
    command = "exec " + shlex.join(prepared.argv)
    if not backend.new_session(
        prepared.identity.session,
        command,
        prepared.cwd,
        env=prepared.environment,
        login_shell=False,
    ):
        raise ReleaseRejectedError("normal service spawn failed")
    record = _record(Path(selector.unit.home), prepared.identity.session)
    if (
        record.started_at < started
        or record.started_at > time.time()
        or record.cwd != str(prepared.cwd)
        or record.cmd != command
    ):
        raise ReleaseRejectedError("session record does not identify this normal spawn attempt")
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
