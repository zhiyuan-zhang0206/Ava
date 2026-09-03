"""Verified restricted-ops handoff inside the existing per-unit updater.

This is not normal service activation. A source-mode predecessor, another live
updater, or an unclassified launcher is refused while the predecessor serves.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shlex
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import psutil
import psycopg
from pydantic import Field

from cli.commands._release_inventory import (
    _regular_bytes,
    revalidate_bootstrap_inventory,
    revalidate_prepared_inventory,
)
from services.agent_ops.bootstrap import (
    BootstrapRuntimeIdentity,
    ObserverProjection,
    PreparedObservation,
    read_prepared_context,
    validate_operation,
)
from shared import updater_handoff
from shared.log import logger
from shared.managed_writer_barrier import EvidenceModel
from shared.managed_writer_observation import (
    ExpectedProcess,
    ExpectedSession,
    observe_process,
)
from shared.native_job_observation import read_crontab
from shared.runtime_release import (
    ReleaseRejectedError,
    VerifiedRelease,
    file_sha256,
    verify_release,
)
from shared.session_backend import get_backend
from shared.session_record import SessionRecord


class BootstrapHopRequest(EvidenceModel):
    """Private invocation references, not a publication or a readiness token."""

    candidate_context: str
    recovery_context: str
    inventory_receipt: str
    predecessor: ExpectedProcess
    normal_release_path: str | None = None


class BootstrapPhase(EvidenceModel):
    """Observed timings only; never lease or readiness authority."""

    stage: str
    observed_at: datetime
    monotonic_s: float
    pid: int
    elapsed_s: float | None


class BootstrapJournal(EvidenceModel):
    request: str
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    inventory_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_context_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    recovery_context_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage: Literal[
        "prepared",
        "cron_quiesced",
        "old_stopped",
        "candidate_starting",
        "candidate_started",
        "candidate_ready",
        "recovering",
        "recovered",
    ]
    cron: str = Field(max_length=65536)
    phases: tuple[BootstrapPhase, ...] = Field(default=(), max_length=64)


@dataclass(frozen=True)
class PreparedBootstrapHop:
    request_path: Path
    request: BootstrapHopRequest
    candidate: PreparedObservation
    recovery: PreparedObservation
    image: VerifiedRelease
    recovery_image: VerifiedRelease
    projection: ObserverProjection
    old_session: ExpectedSession
    resume_generation: str | None = None
    journal: BootstrapJournal | None = None
    validation_seconds: float = 0.0
    predecessor_handoff: updater_handoff.UpdaterHandoffSnapshot | None = None


def _private_reference(text: str, home: Path) -> Path:
    path = Path(text)
    if (
        not path.is_absolute()
        or path.resolve(strict=True) != path
        or path.parent != home / "run"
        or stat.S_IMODE(path.stat().st_mode) != 0o600
        or path.stat().st_uid != os.getuid()
    ):
        raise ReleaseRejectedError("bootstrap hop requires a private canonical unit reference")
    return path


def _verify_image(context: PreparedObservation) -> VerifiedRelease:
    return verify_release(
        Path(context.expected.home) / "releases",
        context.expected.artifact_digest,
        manifest_digest=context.expected.manifest_digest,
        platform_tag=platform.platform(),
        schema_digest=context.schema_digest,
    )


def bootstrap_command(image: VerifiedRelease, context: Path) -> list[str]:
    return [
        str(image.interpreter),
        "-I",
        "-B",
        "-m",
        "services.agent_ops.daemon",
        "--bootstrap-observation",
        str(context),
    ]


def probe_bootstrap(
    context: PreparedObservation,
    projection: ObserverProjection,
    *,
    verified_image: VerifiedRelease | None = None,
) -> str:
    """Challenge the actual same endpoint; ordinary health or an ACK is insufficient."""
    validate_operation(context, projection)
    request = urllib.request.Request(
        f"http://127.0.0.1:{projection.ops_port}/ops/bootstrap-observation",
        data=json.dumps({"challenge": str(context.challenge.challenge)}).encode(),
        headers={
            "Authorization": "Bearer " + projection.cluster_secret.get_secret_value(),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 — fixed authenticated loopback HTTP endpoint.
        body = response.read(64 * 1024 + 1)
    if len(body) > 64 * 1024:
        raise ReleaseRejectedError("bootstrap readback exceeds its budget")
    raw = json.loads(body)
    identity = BootstrapRuntimeIdentity.model_validate_json(json.dumps(raw["runtime"]))
    # A plan already performed complete verification before quiesce. Reuse that
    # invocation-local result while challenging the running process, not a
    # process-global cache or a new traversal inside the readiness deadline.
    image = verified_image if verified_image is not None else _verify_image(context)
    expected_root = Path(context.expected.home) / "releases" / context.expected.artifact_digest
    if (
        image.root != expected_root
        or image.root.resolve(strict=True) != expected_root
        or image.digest != context.expected.artifact_digest
        or image.manifest_digest != context.expected.manifest_digest
        or file_sha256(image.root / "manifest.json") != image.manifest_digest
    ):
        raise ReleaseRejectedError("bootstrap image differs from the verified invocation")
    if (
        raw["mode"] != "bootstrap_observation"
        or raw["full_ready"] is not False
        or raw["challenge"] != str(context.challenge.challenge)
        or raw["unit"] != context.expected.unit().model_dump(mode="json")
        or not isinstance(raw["observer_instance"], str)
        or not raw["observer_instance"]
        or identity.home != context.expected.home
        or identity.artifact_digest != context.expected.artifact_digest
        or identity.manifest_digest != context.expected.manifest_digest
        or not Path(identity.module).is_relative_to(image.root / "venv")
        or Path(identity.module).resolve(strict=True) != Path(identity.module)
        or observe_process(identity.process) != "alive"
    ):
        raise ReleaseRejectedError("bootstrap endpoint returned another runtime identity")
    record = json.loads(_regular_bytes(Path(identity.home) / "run/sessions/ava-ops.json"))
    recorded = ExpectedProcess.model_validate_json(
        json.dumps(
            {
                "pid": record["pid"],
                "create_time": record["create_time"],
                "starttime": record["starttime"],
            }
        )
    )
    if identity.process != recorded:
        raise ReleaseRejectedError("bootstrap responder is not the exact native exec session")
    validate_operation(context, projection)
    return raw["observer_instance"]


def prepare_bootstrap_hop(request_path: Path) -> PreparedBootstrapHop:  # noqa: PLR0915 — ordered read-only admission before any updater effect.
    """Validate image, unit, full receipt and live A before updater/service writes."""
    validation_started = time.monotonic()
    if sys.platform != "linux":
        raise ReleaseRejectedError("restricted updater hop has no native proof on this platform")
    request = BootstrapHopRequest.model_validate_json(_regular_bytes(request_path))
    # A live/unknown predecessor is already disqualifying. Refuse before the
    # expensive complete image reads; this grants no authority or side effect.
    if observe_process(request.predecessor) != "exited":
        raise ReleaseRejectedError("old orchestrator has not positively handed off")
    candidate = read_prepared_context(Path(request.candidate_context))
    home = Path(candidate.expected.home)
    _private_reference(str(request_path), home)
    candidate_path = _private_reference(request.candidate_context, home)
    recovery_path = _private_reference(request.recovery_context, home)
    receipt = _private_reference(request.inventory_receipt, home)
    recovery = read_prepared_context(recovery_path)
    if (
        recovery.operation != candidate.operation
        or recovery.expected.machine != candidate.expected.machine
        or recovery.expected.home != candidate.expected.home
        or recovery.expected.artifact_digest == candidate.expected.artifact_digest
    ):
        raise ReleaseRejectedError("bootstrap A/B images do not share the exact operation/unit")
    image = _verify_image(candidate)
    recovery_image = _verify_image(recovery)
    if not Path(__file__).resolve().is_relative_to(image.root / "venv"):
        raise ReleaseRejectedError("updater is not loaded from its verified candidate image")
    if observe_process(request.predecessor) != "exited":
        raise ReleaseRejectedError("old orchestrator has not positively handed off")
    projection = ObserverProjection.from_environment()
    from shared.config import settings

    if Path(settings.general.ava_home).resolve() != home:
        raise ReleaseRejectedError("updater session namespace differs from prepared unit")
    validate_operation(candidate, projection)
    validate_operation(recovery, projection)
    resume_generation, journal = _read_recovery(request_path, receipt)
    predecessor: updater_handoff.UpdaterHandoffSnapshot | None = None
    if journal is None:
        predecessor = updater_handoff.read()
        if (
            predecessor.status != "running"
            or predecessor.owner_pid != request.predecessor.pid
            or predecessor.owner_create_time != request.predecessor.create_time
            or updater_handoff.owner_is_live(predecessor)
        ):
            raise ReleaseRejectedError("existing updater handoff does not prove predecessor exit")
    if journal is not None and (
        journal.candidate_context_digest
        != hashlib.sha256(_regular_bytes(candidate_path)).hexdigest()
        or journal.recovery_context_digest
        != hashlib.sha256(_regular_bytes(recovery_path)).hexdigest()
    ):
        raise ReleaseRejectedError("bootstrap recovery contexts changed")
    if journal is None:
        with psycopg.connect(projection.db_url.get_secret_value(), connect_timeout=5) as conn:
            expected = revalidate_prepared_inventory(
                conn,
                image,
                home,
                candidate.expected.machine,
                receipt,
                schema_digest=candidate.schema_digest,
            )
    else:
        current_process, _current_kind = _recorded_observer_for(
            home,
            candidate,
            image,
            recovery_image,
            request,
        )
        current_session = ExpectedSession(name="ava-ops", process=current_process)
        with psycopg.connect(projection.db_url.get_secret_value(), connect_timeout=5) as conn:
            expected = revalidate_bootstrap_inventory(
                conn,
                image,
                home,
                candidate.expected.machine,
                receipt,
                current_session=current_session,
                schema_digest=candidate.schema_digest,
            )
    if expected != candidate.expected or len(expected.sessions) != 1:
        raise ReleaseRejectedError("restricted hop cannot stop ordinary or additional sessions")
    session = expected.sessions[0]
    if session.name != "ava-ops" or expected.processes != (session.process,):
        raise ReleaseRejectedError("restricted hop requires one exact existing ops session")
    wanted = bootstrap_command(recovery_image, recovery_path)
    if journal is None:
        if observe_process(session.process) != "alive":
            raise ReleaseRejectedError("restricted predecessor identity is not alive")
        if psutil.Process(session.process.pid).cmdline() != wanted:
            raise ReleaseRejectedError(
                "normal/source ops has no verified bootstrap recovery contract"
            )
        probe_bootstrap(recovery, projection, verified_image=recovery_image)
        record = json.loads(_regular_bytes(home / "run/sessions/ava-ops.json"))
        if shlex.split(record["cmd"]) != ["exec", *wanted]:
            raise ReleaseRejectedError("ops session record differs from the live restricted image")
    if candidate_path == recovery_path:
        raise ReleaseRejectedError("candidate and recovery contexts must remain separate")
    return PreparedBootstrapHop(
        request_path,
        request,
        candidate,
        recovery,
        image,
        recovery_image,
        projection,
        session,
        resume_generation,
        journal,
        time.monotonic() - validation_started,
        predecessor,
    )


def _read_recovery(request: Path, receipt: Path) -> tuple[str | None, BootstrapJournal | None]:
    try:
        recovery = updater_handoff.read_bootstrap_recovery()
    except updater_handoff.BootstrapRecoveryInvalidError as exc:
        raise ReleaseRejectedError("bootstrap recovery evidence is malformed") from exc
    if recovery is None:
        return None, None
    snapshot = updater_handoff.read()
    if snapshot.status != "running" or updater_handoff.owner_is_live(snapshot):
        raise ReleaseRejectedError("another updater still owns bootstrap recovery")
    journal = BootstrapJournal.model_validate_json(json.dumps(recovery["journal"]))
    if (
        journal.request != str(request)
        or journal.request_digest != hashlib.sha256(_regular_bytes(request)).hexdigest()
        or journal.inventory_digest != hashlib.sha256(_regular_bytes(receipt)).hexdigest()
        or snapshot.generation is None
        or recovery["generation"] != snapshot.generation
    ):
        raise ReleaseRejectedError("bootstrap recovery request or prepared receipt changed")
    return snapshot.generation, journal


def _cron_tables(plan: PreparedBootstrapHop) -> tuple[bytes, bytes]:
    """Accept only exact inventoried invocations of restricted A, not arbitrary jobs."""
    expected = plan.candidate.expected
    if not expected.launchers or any(item.kind != "crontab" for item in expected.launchers):
        raise ReleaseRejectedError("restricted hop requires proved native cron ownership")
    original = (
        plan.journal.cron.encode("utf-8")
        if plan.journal is not None
        else read_crontab(plan.candidate.challenge.valid_until)
    )
    retained: list[bytes] = []
    found: set[str] = set()
    wanted = bootstrap_command(plan.recovery_image, Path(plan.request.recovery_context))
    for encoded in original.splitlines(keepends=True):
        line = encoded.decode("utf-8").rstrip("\r\n")
        if not line.strip() or line.lstrip().startswith("#"):
            retained.append(encoded)
            continue
        digest = hashlib.sha256(line.encode()).hexdigest()
        fields = shlex.split(line, comments=True)
        if (
            digest not in {item.definition_digest for item in expected.launchers}
            or digest in found
            or fields != ["@reboot", f"AVA_HOME={expected.home}", *wanted]
        ):
            raise ReleaseRejectedError("unknown or non-bootstrap launcher blocks the hop")
        found.add(digest)
    if found != {item.definition_digest for item in expected.launchers}:
        raise ReleaseRejectedError("inventoried launcher is absent or changed")
    return original, b"".join(retained)


def _journal(plan: PreparedBootstrapHop, generation: str, stage: str, cron: bytes) -> None:
    """Replace one bounded versioned recovery envelope under exact ownership."""
    recovery = updater_handoff.read_bootstrap_recovery()
    previous = (
        BootstrapJournal.model_validate_json(json.dumps(recovery["journal"])).phases
        if recovery is not None
        else ()
    )
    now = time.monotonic()
    elapsed = (
        now - previous[-1].monotonic_s if previous and previous[-1].pid == os.getpid() else None
    )
    phase = BootstrapPhase(
        stage=stage,
        observed_at=datetime.now(UTC),
        monotonic_s=now,
        pid=os.getpid(),
        elapsed_s=elapsed,
    )
    journal = BootstrapJournal.model_validate(
        {
            "request": str(plan.request_path),
            "request_digest": hashlib.sha256(_regular_bytes(plan.request_path)).hexdigest(),
            "inventory_digest": hashlib.sha256(
                _regular_bytes(Path(plan.request.inventory_receipt))
            ).hexdigest(),
            "candidate_context_digest": hashlib.sha256(
                _regular_bytes(Path(plan.request.candidate_context))
            ).hexdigest(),
            "recovery_context_digest": hashlib.sha256(
                _regular_bytes(Path(plan.request.recovery_context))
            ).hexdigest(),
            "stage": stage,
            # Only the validated secret-free restricted-A command shape reaches here.
            "cron": cron.decode("utf-8"),
            "phases": (*previous, phase),
        }
    )
    updater_handoff.write_bootstrap_recovery(generation, journal.model_dump(mode="json"))
    logger.info(
        "bootstrap_hop_phase {stage} elapsed_s={elapsed} validation_s={validation}",
        stage=stage,
        elapsed=elapsed,
        validation=plan.validation_seconds,
    )


def _replace_cron(before: bytes, after: bytes, plan: PreparedBootstrapHop) -> None:
    until = plan.candidate.challenge.valid_until
    if read_crontab(until) != before:
        raise ReleaseRejectedError("cron changed before exact quiesce/restore")
    remaining = (until - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise ReleaseRejectedError("bootstrap hop expired before native action")
    validate_operation(plan.candidate, plan.projection)
    result = subprocess.run(
        ["/usr/bin/crontab", "-"],
        input=after,
        capture_output=True,
        timeout=min(5, remaining),
        check=False,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    )
    if result.returncode or read_crontab(until) != after:
        raise ReleaseRejectedError("native cron action has no matching independent readback")
    validate_operation(plan.candidate, plan.projection)


def _child_environment(plan: PreparedBootstrapHop) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(Path(plan.candidate.expected.home).parent),
        "AVA_HOME": plan.candidate.expected.home,
        "AVA_DB_URL": plan.projection.db_url.get_secret_value(),
        "AVA_CLUSTER_SECRET": plan.projection.cluster_secret.get_secret_value(),
        "AVA_OPS_HEALTH_PORT": str(plan.projection.ops_port),
        "AVA_TRANSPORT_ENCRYPTION": plan.projection.transport_encryption,
    }


def _start_observer(plan: PreparedBootstrapHop, image: VerifiedRelease, context_path: str) -> None:
    context = plan.candidate if image.digest == plan.image.digest else plan.recovery
    validate_operation(context, plan.projection)
    if not get_backend().new_session(
        plan.old_session.name,
        "exec " + shlex.join(bootstrap_command(image, Path(context_path))),
        Path(plan.candidate.expected.home),
        env=_child_environment(plan),
        login_shell=False,
    ):
        raise ReleaseRejectedError("exact bootstrap session launch was refused")


def _stop_old_observer(plan: PreparedBootstrapHop) -> None:
    if _recorded_observer(plan) != (plan.old_session.process, "A"):
        raise ReleaseRejectedError("native session no longer identifies restricted A")
    if observe_process(plan.old_session.process) != "alive":
        raise ReleaseRejectedError("restricted predecessor changed before stop")
    if not _signal_expected(plan, plan.old_session.process):
        raise ReleaseRejectedError("official exact session stop was refused")
    _wait_exited(plan, plan.old_session.process)


def _wait_exited(plan: PreparedBootstrapHop, process: ExpectedProcess) -> None:
    # Leave half the remaining challenge for observing/restarting the retained A.
    remaining = (plan.candidate.challenge.valid_until - datetime.now(UTC)).total_seconds()
    deadline = time.monotonic() + min(5, max(0, remaining / 2))
    while time.monotonic() < deadline:
        verdict = observe_process(process)
        if verdict == "exited":
            return
        if verdict != "alive":
            raise ReleaseRejectedError("session stop has unknown identity")
        time.sleep(0.05)
    raise ReleaseRejectedError("session did not stop within its recovery budget")


def _recorded_observer_for(
    home: Path,
    candidate: PreparedObservation,
    image: VerifiedRelease,
    recovery_image: VerifiedRelease,
    request: BootstrapHopRequest,
) -> tuple[ExpectedProcess, str]:
    """Only a real record plus matching live command can identify A or B."""
    path = home / "run/sessions/ava-ops.json"
    record = json.loads(_regular_bytes(path))
    process = ExpectedProcess.model_validate_json(
        json.dumps(
            {
                "pid": record["pid"],
                "create_time": record["create_time"],
                "starttime": record["starttime"],
            }
        )
    )
    command = shlex.split(record["cmd"])
    choices = {
        "A": bootstrap_command(recovery_image, Path(request.recovery_context)),
        "B": bootstrap_command(image, Path(request.candidate_context)),
    }
    matching = [kind for kind, argv in choices.items() if command == ["exec", *argv]]
    if len(matching) != 1 or record["cwd"] != candidate.expected.home:
        raise ReleaseRejectedError("session record is not either verified restricted image")
    verdict = observe_process(process)
    if verdict not in {"alive", "exited"}:
        raise ReleaseRejectedError("recorded bootstrap process identity is unknown")
    kind = matching[0]
    if verdict == "alive":
        actual = psutil.Process(process.pid)
        if actual.cmdline() != choices[kind] or actual.children(recursive=True):
            raise ReleaseRejectedError("bootstrap process has an unknown command or child")
        if observe_process(process) != "alive":
            raise ReleaseRejectedError("bootstrap identity changed during observation")
    return process, kind


def _recorded_observer(plan: PreparedBootstrapHop) -> tuple[ExpectedProcess, str]:
    return _recorded_observer_for(
        Path(plan.candidate.expected.home),
        plan.candidate,
        plan.image,
        plan.recovery_image,
        plan.request,
    )


def _candidate_start_is_ambiguous(
    stage: str,
    recorded: tuple[ExpectedProcess, str] | None,
    old: ExpectedProcess,
) -> bool:
    """A fork may have occurred until a different exact B record proves its outcome."""
    return stage == "candidate_starting" and (
        recorded is None or recorded[1] != "B" or recorded[0] == old
    )


def _signal_expected(plan: PreparedBootstrapHop, process: ExpectedProcess) -> bool:
    path = Path(plan.candidate.expected.home) / "run/sessions/ava-ops.json"
    record = SessionRecord(**json.loads(_regular_bytes(path)))
    if (record.pid, record.create_time, record.starttime) != (
        process.pid,
        process.create_time,
        process.starttime,
    ):
        raise ReleaseRejectedError("session record changed before exact native signal")
    validate_operation(plan.candidate, plan.projection)
    return get_backend().graceful_signal(plan.old_session.name, expected=record)


def _await_observer(plan: PreparedBootstrapHop, kind: Literal["A", "B"]) -> None:
    context = plan.recovery if kind == "A" else plan.candidate
    remaining = (context.challenge.valid_until - datetime.now(UTC)).total_seconds()
    # A fresh bootstrap verifies its entire image before binding. Reserve half
    # the outstanding challenge for compensation instead of imposing a five-
    # second cap that can expire during that mandatory cold verification.
    deadline = time.monotonic() + max(0, remaining / 2)
    while time.monotonic() < deadline:
        try:
            process, actual_kind = _recorded_observer(plan)
        except ReleaseRejectedError:
            # Native launch can still be in its shell-to-exec transition or a
            # bootstrap's platform probe. Unknown is never ready or killable;
            # only this read-only startup wait may retry it. Stop stays strict.
            time.sleep(0.05)
            continue
        if actual_kind != kind or observe_process(process) != "alive":
            raise ReleaseRejectedError("candidate did not retain its exact session identity")
        try:
            probe_bootstrap(
                context,
                plan.projection,
                verified_image=plan.recovery_image if kind == "A" else plan.image,
            )
            return
        except urllib.error.URLError:
            time.sleep(0.05)
    raise ReleaseRejectedError("bootstrap endpoint did not answer within its recovery budget")


def _restore_a(
    plan: PreparedBootstrapHop, generation: str, original: bytes, quiesced: bytes
) -> None:
    validate_operation(plan.recovery, plan.projection)
    process, kind = _recorded_observer(plan)
    if kind == "B" and observe_process(process) == "alive":
        # Never signal a name after the identity was substituted by another writer.
        if _recorded_observer(plan) != (process, kind):
            raise ReleaseRejectedError("candidate session changed before compensation")
        if not _signal_expected(plan, process):
            raise ReleaseRejectedError("candidate compensation stop was refused")
        _wait_exited(plan, process)
    _journal(plan, generation, "recovering", original)
    if observe_process(process) == "exited":
        _start_observer(plan, plan.recovery_image, plan.request.recovery_context)
    _await_observer(plan, "A")
    current = read_crontab(plan.candidate.challenge.valid_until)
    if current == quiesced:
        _replace_cron(quiesced, original, plan)
    elif current != original:
        raise ReleaseRejectedError("cron changed; recovery will not overwrite another writer")
    _journal(plan, generation, "recovered", original)


def _resume_hop(
    plan: PreparedBootstrapHop, generation: str, original: bytes, quiesced: bytes
) -> int:
    if plan.journal is None:
        raise ReleaseRejectedError("bootstrap resume requires its retained journal")
    process, kind = _recorded_observer(plan)
    if kind == "B" and observe_process(process) == "alive":
        _await_observer(plan, "B")
        if read_crontab(plan.candidate.challenge.valid_until) != quiesced:
            raise ReleaseRejectedError("candidate has an unquiesced or changed relauncher")
        _journal(plan, generation, "candidate_ready", original)
        return 3  # Bootstrap-only: deliberately not the normal updater success code.
    if plan.journal.stage == "candidate_starting" and process == plan.old_session.process:
        raise ReleaseRejectedError("candidate spawn outcome lacks a new exact session record")
    _restore_a(plan, generation, original, quiesced)
    return 1


def execute_bootstrap_hop(plan: PreparedBootstrapHop, generation: str) -> int:
    """Expose phase observations without starting the ordinary telemetry writer."""
    sink = logger.add(
        sys.stderr,
        format="{message}",
        level="INFO",
        filter=lambda record: record["message"].startswith("bootstrap_hop_phase "),
    )
    try:
        return _execute_bootstrap_hop(plan, generation)
    finally:
        logger.remove(sink)


def _execute_bootstrap_hop(plan: PreparedBootstrapHop, generation: str) -> int:
    """Prepared restricted A -> B only; never marks the unit normally ready."""
    validate_operation(plan.candidate, plan.projection)
    original_cron, quiesced_cron = _cron_tables(plan)
    if plan.journal is not None:
        return _resume_hop(plan, generation, original_cron, quiesced_cron)
    remaining = (plan.candidate.challenge.valid_until - datetime.now(UTC)).total_seconds()
    if remaining <= plan.validation_seconds:
        raise ReleaseRejectedError("remaining challenge cannot cover observed cold validation cost")
    _journal(plan, generation, "prepared", original_cron)
    try:
        _replace_cron(original_cron, quiesced_cron, plan)
        _journal(plan, generation, "cron_quiesced", original_cron)
        validate_operation(plan.candidate, plan.projection)
        _stop_old_observer(plan)
        _journal(plan, generation, "old_stopped", original_cron)
        _journal(plan, generation, "candidate_starting", original_cron)
        _start_observer(plan, plan.image, plan.request.candidate_context)
        _journal(plan, generation, "candidate_started", original_cron)
        _await_observer(plan, "B")
        _verify_quiesced(plan, quiesced_cron)
        _journal(plan, generation, "candidate_ready", original_cron)
        return 3
    except Exception:
        # A launch exception can occur after fork but before session publication.
        # Do not start another process when that outcome cannot be identified.
        recovery = updater_handoff.read_bootstrap_recovery()
        stage = (
            BootstrapJournal.model_validate_json(json.dumps(recovery["journal"])).stage
            if recovery is not None
            else ""
        )
        try:
            recorded = _recorded_observer(plan)
        except Exception:
            if _candidate_start_is_ambiguous(stage, None, plan.old_session.process):
                raise ReleaseRejectedError(
                    "candidate spawn is ambiguous; recovery journal retained"
                ) from None
            raise
        if _candidate_start_is_ambiguous(stage, recorded, plan.old_session.process):
            raise ReleaseRejectedError(
                "candidate spawn is ambiguous; recovery journal retained"
            ) from None
        _restore_a(plan, generation, original_cron, quiesced_cron)
        return 1


def _verify_quiesced(plan: PreparedBootstrapHop, quiesced_cron: bytes) -> None:
    if read_crontab(plan.candidate.challenge.valid_until) != quiesced_cron:
        raise ReleaseRejectedError("native launcher reappeared during candidate boot")
    if observe_process(plan.old_session.process) != "exited":
        raise ReleaseRejectedError("old restricted writer exit is no longer proved")
