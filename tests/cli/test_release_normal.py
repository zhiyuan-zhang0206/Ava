"""Normal updater producer contracts; actual cold launch remains a CI gate.

The checked chain (#4117 S3) is driven directly here: these tests call
``_drive_checked_normal_release`` and ``start_normal_service`` the way the
prove scripts will. Journal writes go
through the real ``updater_handoff`` writer against a unit-local ``$AVA_HOME``,
so the ownership, monotonic-transition and I8 ``replaces`` checks are exercised
for real instead of mocked.
"""

import hashlib
import json
import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast
from unittest.mock import Mock
from uuid import UUID, uuid4

import psutil
import psycopg
import pytest

from cli.commands import _release_services as release_services
from cli.commands import _update_normal_release as normal
from cli.commands._release_selector import read_selector, selector_bytes
from cli.commands._release_services import PreparedService, _command, normal_spawn_command
from ops.spec import ServiceSpec
from services.agent_ops.bootstrap import PreparedObservation
from shared import spawn_receipt
from shared.config import settings
from shared.managed_writer_activation import UnitActivationReadback
from shared.managed_writer_barrier import RolloutIdentity
from shared.managed_writer_observation import (
    ExpectedProcess,
    ExpectedUnitWriters,
    ObservationChallenge,
    ProcessVerdict,
)
from shared.managed_writer_publication import (
    CandidateUnitPlan,
    NormalService,
    NormalServiceReadback,
    PublishedUnit,
    SelectorReadback,
)
from shared.proc_tree import stable_create_time
from shared.runtime_publication_input import read_publication_selector
from shared.runtime_release import ReleaseRejectedError, VerifiedRelease
from shared.session_record import SessionRecord
from shared.updater_recovery import (
    NormalReleaseRecoveryJournal,
    SpawnAttempt,
    SpawnVerdict,
)


def test_selector_writer_and_pending_plan_share_exact_bytes(tmp_path: Path) -> None:
    import hashlib

    unit = PublishedUnit(
        machine="test",
        home=str(tmp_path),
        prepared_receipt_digest="d" * 64,
        artifact_digest="a" * 64,
        manifest_digest="b" * 64,
        inventory_digest="c" * 64,
    )
    encoded = selector_bytes(unit)
    selector = read_publication_selector(encoded)
    assert selector is not None
    assert selector.prepared_receipt_digest == unit.prepared_receipt_digest
    service = NormalService(
        session="ava-ops",
        module="services.agent_ops.daemon",
        executable=str(tmp_path / "releases" / unit.artifact_digest / "python/bin/python"),
        entrypoint=str(tmp_path / "releases" / unit.artifact_digest / "venv/ops.py"),
        command_digest="d" * 64,
    )
    plan = CandidateUnitPlan(
        unit=unit,
        services=(service,),
        previous_selector_digest=None,
        selector_digest=hashlib.sha256(encoded).hexdigest(),
    )
    assert plan.unit == unit


def test_selector_reader_refuses_symlink_ancestor(tmp_path: Path) -> None:
    actual = tmp_path / "unit"
    (actual / "releases").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ReleaseRejectedError, match="canonical"):
        read_selector(alias)


def test_unknown_native_service_rejected_before_start(tmp_path: Path) -> None:
    root = tmp_path / "image"
    root.mkdir()
    executable = root / "unknown-service"
    executable.write_bytes(b"not executed")
    image = VerifiedRelease("a" * 64, "b" * 64, root, executable, root)
    spec = ServiceSpec(
        session="unknown",
        cmd=str(executable),
        capabilities=frozenset({"gateway"}),
        requires_db=False,
        curl_url="http://127.0.0.1:12345/healthz",
    )
    with pytest.raises(ReleaseRejectedError, match="readiness adapter"):
        _command(spec, image)


def test_mutable_executable_rejected_before_probe(tmp_path: Path) -> None:
    root = tmp_path / "image"
    root.mkdir()
    executable = tmp_path / "mutable"
    executable.write_bytes(b"not executed")
    image = VerifiedRelease("a" * 64, "b" * 64, root, executable, root)
    spec = ServiceSpec(
        session="otel-collector",
        cmd=str(executable),
        capabilities=frozenset({"gateway"}),
        requires_db=False,
    )
    with pytest.raises(ReleaseRejectedError, match="outside"):
        _command(spec, image)


@pytest.mark.parametrize(
    ("session", "command", "message"),
    [
        ("gateway", "{executable} -m", "module is absent"),
        ("frontend", "{executable}", "entry point is absent"),
    ],
)
def test_malformed_service_commands_are_controlled_refusals(
    tmp_path: Path, session: str, command: str, message: str
) -> None:
    root = tmp_path / "image"
    root.mkdir()
    executable = root / "python"
    executable.write_bytes(b"not executed")
    image = VerifiedRelease("a" * 64, "b" * 64, root, executable, root)
    spec = ServiceSpec(
        session=session,
        cmd=command.format(executable=executable),
        capabilities=frozenset({"gateway"}),
        requires_db=False,
        curl_url="http://127.0.0.1:12345/healthz",
    )
    with pytest.raises(ReleaseRejectedError, match=message):
        _command(spec, image)


def test_normal_activation_enters_the_checked_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flip removed the fence: the entry drives the checked chain directly."""
    driven: list[tuple[object, str]] = []

    def drive(plan: object, generation: str) -> None:
        driven.append((plan, generation))

    monkeypatch.setattr(normal, "_drive_checked_normal_release", drive)
    plan = Mock(spec=normal.PreparedNormalRelease)
    normal.execute_normal_release(plan, "generation")
    assert driven == [(plan, "generation")]


@pytest.mark.parametrize(
    ("generation", "stage"),
    [
        ("replacement", "candidate_ready"),
        ("expected", "recovered"),
        ("expected", "candidate_started"),
    ],
)
def test_continuation_requires_same_actual_ready_handoff(
    monkeypatch: pytest.MonkeyPatch, generation: str, stage: str
) -> None:
    from unittest.mock import Mock

    from cli.commands import _update_normal_release as normal
    from cli.commands import _update_normal_release_standalone as standalone
    from cli.commands._update_bootstrap import PreparedBootstrapHop

    monkeypatch.setattr(
        normal.updater_handoff,
        "read_bootstrap_recovery",
        lambda: {
            "version": 1,
            "generation": generation,
            "journal": {
                "request": "/unit/run/bootstrap.json",
                "request_digest": "a" * 64,
                "inventory_digest": "b" * 64,
                "candidate_context_digest": "c" * 64,
                "recovery_context_digest": "d" * 64,
                "normal_release_planned": True,
                "stage": stage,
                "cron": "",
                "phases": [
                    {
                        "stage": stage,
                        "observed_at": "2026-09-04T00:00:00+00:00",
                        "monotonic_s": 0.0,
                        "pid": 1,
                        "elapsed_s": None,
                    }
                ],
                "normal_release": None,
            },
        },
    )

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("exit code alone must not authorize normal probing or service effects")

    monkeypatch.setattr(standalone, "probe_bootstrap", forbidden)
    monkeypatch.setattr(normal, "execute_normal_release", forbidden)
    with pytest.raises(ReleaseRejectedError, match="actual candidate-ready handoff"):
        normal.continue_after_bootstrap(
            Mock(spec=PreparedBootstrapHop), Mock(spec=normal.PreparedNormalRelease), "expected"
        )


def test_prepared_services_pin_dependency_order_before_mutable_roster_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from unittest.mock import Mock

    from cli.commands import _release_services as services
    from shared.runtime_publication_input import PreparedService as ReceiptService

    root = tmp_path / "releases" / ("a" * 64)
    image = VerifiedRelease("a" * 64, "b" * 64, root, root / "python", root)
    unit = PublishedUnit(
        machine="test",
        home=str(tmp_path),
        prepared_receipt_digest="d" * 64,
        artifact_digest="a" * 64,
        manifest_digest="b" * 64,
        inventory_digest="c" * 64,
    )
    specs = [
        ServiceSpec(
            session=name, cmd="unused", capabilities=frozenset({"gateway"}), requires_db=False
        )
        for name in ("z-dependency", "a-consumer", "disabled")
    ]
    roster: list[tuple[ServiceSpec, str | None]] = [
        (specs[0], None),
        (specs[1], None),
        (specs[2], "disabled"),
    ]
    receipt = Mock(
        inventory_digest=unit.inventory_digest,
        services=tuple(
            sorted(
                (
                    ReceiptService(session=spec.session, requires_db=False, gate=gate)
                    for spec, gate in roster
                ),
                key=lambda item: item.session,
            )
        ),
    )

    def verified(_unit: PublishedUnit, _schema: str) -> VerifiedRelease:
        return image

    def prefix() -> Path:
        return root / "venv"

    def read(_path: Path) -> bytes:
        return b"receipt parsed by boundary fake"

    def parsed(_body: bytes) -> object:
        return receipt

    def discover(_roles: object) -> list[tuple[ServiceSpec, str | None]]:
        return roster

    def roles() -> frozenset[str]:
        return frozenset({"gateway"})

    def command(spec: ServiceSpec, _image: VerifiedRelease) -> services.PreparedService:
        identity = NormalService(
            session=f"ava-{spec.session}",
            module=None,
            executable=str(root / "binary"),
            entrypoint=str(root / "binary"),
            command_digest="d" * 64,
        )
        return services.PreparedService(identity, spec, ("unused",), root, {})

    monkeypatch.setattr(services, "verify_unit_image", verified)
    monkeypatch.setattr(services, "runtime_venv", prefix)
    monkeypatch.setattr(services, "regular_bytes", read)
    monkeypatch.setattr(services.PreparationReceipt, "model_validate_json", parsed)
    monkeypatch.setattr(services, "services_for_capabilities_annotated", discover)
    monkeypatch.setattr(services, "machine_role", roles)
    monkeypatch.setattr(services, "_command", command)
    prepared = services.prepare_normal_services(unit, "e" * 64)
    roster.clear()
    assert [item.identity.session for item in prepared] == ["ava-z-dependency", "ava-a-consumer"]


# --- checked chain: fixtures and harness ---------------------------------------

GENERATION = "generation-1"


class _StubConnection:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_args: object) -> None:
        return None


def _stub_connect(*_args: object, **_kwargs: object) -> _StubConnection:
    return _StubConnection()


def _stub_transaction(_conn: object, _context: object) -> AbstractContextManager[None]:
    return nullcontext()


def _stub_connection() -> psycopg.Connection:
    """A connection-shaped stand-in; every call on it is monkeypatched anyway."""
    return cast("psycopg.Connection", _StubConnection())


def _no_record(_home: Path, _session: str) -> SessionRecord | None:
    return None


def _observe_as(verdict: ProcessVerdict) -> Callable[[ExpectedProcess], ProcessVerdict]:
    def observed(_process: ExpectedProcess) -> ProcessVerdict:
        return verdict

    return observed


def _await_birth_stub(
    outcome: spawn_receipt.SpawnOutcome,
) -> Callable[..., spawn_receipt.SpawnOutcome]:
    def adjudicated(*_args: object, **_kwargs: object) -> spawn_receipt.SpawnOutcome:
        return outcome

    return adjudicated


@pytest.fixture()
def unit_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """One unit home doubling as this process's $AVA_HOME (journal writers run real)."""
    home = tmp_path / "unit"
    (home / "run" / "sessions").mkdir(parents=True)
    monkeypatch.setattr(settings.general, "ava_home", home)
    return home


def _published_unit(home: Path) -> PublishedUnit:
    return PublishedUnit(
        machine="test",
        home=str(home),
        prepared_receipt_digest="d" * 64,
        artifact_digest="a" * 64,
        manifest_digest="b" * 64,
        inventory_digest="c" * 64,
    )


def _context(home: Path, challenge: UUID, valid_until: datetime) -> PreparedObservation:
    return PreparedObservation(
        expected=ExpectedUnitWriters(
            machine="test",
            home=str(home),
            artifact_digest="a" * 64,
            manifest_digest="b" * 64,
            processes=(),
            sessions=(),
            launchers=(),
        ),
        operation=RolloutIdentity(
            holder="holder",
            acquired_at=datetime.now(UTC) - timedelta(minutes=1),
            target_sha="e" * 40,
        ),
        challenge=ObservationChallenge(challenge=challenge, valid_until=valid_until),
        schema_digest="f" * 64,
    )


def _prepared_service(
    home: Path, session: str, *, argv: tuple[str, ...] | None = None
) -> PreparedService:
    return PreparedService(
        identity=NormalService(
            session=session,
            module="services.agent_ops.daemon",
            executable="/image/python",
            entrypoint="/image/ops.py",
            command_digest="d" * 64,
        ),
        spec=Mock(),
        argv=argv or ("/image/python", "-m", "services.agent_ops.daemon"),
        cwd=home / "releases" / ("a" * 64),
        environment={},
    )


def _prepared_plan(
    home: Path,
    services: tuple[PreparedService, ...],
    *,
    previous: str | None = None,
    request_path: Path | None = None,
    context: PreparedObservation | None = None,
) -> normal.PreparedNormalRelease:
    challenge = uuid4()
    context = context or _context(home, challenge, datetime.now(UTC) + timedelta(minutes=10))
    projection = Mock()
    projection.db_url.get_secret_value.return_value = "postgresql://example"
    return normal.PreparedNormalRelease(
        request_path=request_path or (home / "normal-request.json"),
        request=normal.NormalReleaseRequest(
            context_path=str(home / "context.json"),
            unit=_published_unit(home),
            previous_selector=previous,
            predecessor=ExpectedProcess(pid=424242, create_time=1.0),
        ),
        context=context,
        projection=projection,
        services=services,
        bootstrap=SessionRecord(
            pid=4242,
            create_time=2.0,
            cmd="exec /image/python -m services.agent_ops.daemon",
            cwd=str(home),
            started_at=1.0,
            starttime=99,
        ),
        resume_generation=GENERATION,
    )


def _selector_readback(
    unit: PublishedUnit, challenge: UUID, observed_at: datetime
) -> SelectorReadback:
    return SelectorReadback(
        unit=unit,
        challenge=challenge,
        previous_digest=None,
        current_digest="e" * 64,
        observed_at=observed_at,
        valid_until=observed_at + timedelta(minutes=10),
    )


def _service_readback(
    session: str, challenge: UUID, observed_at: datetime
) -> NormalServiceReadback:
    service = NormalService(
        session=session,
        module=None,
        executable="/image/python",
        entrypoint="/image/ops.py",
        command_digest="f" * 64,
    )
    process = ExpectedProcess(pid=41, create_time=1.0)
    return NormalServiceReadback(
        service=service,
        supervisor=process,
        child=process,
        loaded_module=None,
        executable="/image/python",
        entrypoint="/image/ops.py",
        artifact_digest="b" * 64,
        manifest_digest="c" * 64,
        readiness="normal",
        challenge=challenge,
        observed_at=observed_at,
        valid_until=observed_at + timedelta(minutes=10),
        observation_digest="1" * 64,
    )


def _write_ops_record(home: Path, record: SessionRecord) -> None:
    (home / "run" / "sessions" / "ava-ops.json").write_text(json.dumps(asdict(record)))


def _seed_environment(
    home: Path,
    *,
    generation: str = GENERATION,
    request: str | None = None,
    request_digest: str = "1" * 64,
    inventory_digest: str = "2" * 64,
    candidate_context_digest: str = "3" * 64,
    recovery_context_digest: str = "4" * 64,
    normal_release: dict[str, object] | None = None,
) -> None:
    """A real running handoff owned by THIS process plus the candidate-ready envelope."""
    run = home / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / "updater-handoff.json").write_text(
        json.dumps(
            {
                "generation": generation,
                "expected_session": f"direct-updater:pid{os.getpid()}",
                "phase": "running",
                "created_at": "2026-09-20T00:00:00+00:00",
                "expires_at": "2030-01-01T00:00:00+00:00",
                "owner_pid": os.getpid(),
                "owner_create_time": stable_create_time(psutil.Process()),
            }
        )
    )
    (run / "updater-bootstrap-recovery.json").write_text(
        json.dumps(
            {
                "version": 1,
                "generation": generation,
                "journal": {
                    "request": request or str(home / "bootstrap.json"),
                    "request_digest": request_digest,
                    "inventory_digest": inventory_digest,
                    "candidate_context_digest": candidate_context_digest,
                    "recovery_context_digest": recovery_context_digest,
                    "normal_release_planned": True,
                    "stage": "candidate_ready",
                    "cron": "",
                    "phases": [
                        {
                            "stage": "candidate_ready",
                            "observed_at": "2026-09-20T00:00:00+00:00",
                            "monotonic_s": 0.0,
                            "pid": os.getpid(),
                            "elapsed_s": None,
                        }
                    ],
                    "normal_release": normal_release,
                },
            }
        )
    )


def _journal(home: Path) -> NormalReleaseRecoveryJournal | None:
    raw = json.loads((home / "run" / "updater-bootstrap-recovery.json").read_text())
    envelope = cast("dict[str, object]", raw)
    journal = cast("dict[str, object]", envelope["journal"])["normal_release"]
    if journal is None:
        return None
    return NormalReleaseRecoveryJournal.model_validate_json(json.dumps(journal))


NormalStage = Literal[
    "waiting", "selected", "bootstrap_stopped", "starting", "observed", "committed"
]


def _journal_for(
    plan: normal.PreparedNormalRelease,
    stage: NormalStage,
    *,
    readback: UnitActivationReadback | None = None,
    starting_session: str | None = None,
    starting_attempt: SpawnAttempt | None = None,
    replaces: SpawnVerdict | None = None,
) -> NormalReleaseRecoveryJournal:
    return NormalReleaseRecoveryJournal(
        request_path=str(plan.request_path),
        operation_context=normal._prepared_recovery(plan.context),
        unit=plan.request.unit,
        previous_selector=plan.request.previous_selector,
        stage=stage,
        readback=readback,
        starting_session=starting_session,
        starting_attempt=starting_attempt,
        replaces=replaces,
    )


def _attempt_for(
    home: Path,
    prepared: PreparedService,
    *,
    nonce: UUID | None = None,
    generation: str = GENERATION,
) -> SpawnAttempt:
    nonce = nonce or uuid4()
    session = prepared.identity.session
    return SpawnAttempt(
        nonce=nonce,
        session=session,
        cmd_digest=hashlib.sha256(normal_spawn_command(prepared).encode()).hexdigest(),
        cwd=str(prepared.cwd),
        spawn_lock_path=spawn_receipt.session_lock_path(home, generation, session)
        .relative_to(home)
        .as_posix(),
        receipt_path=spawn_receipt.receipt_path(home, generation, session, nonce)
        .relative_to(home)
        .as_posix(),
        recorded_at=datetime.now(UTC),
    )


def _birth_receipt(
    home: Path, attempt: SpawnAttempt, *, pid: int = 5150, starttime: int = 456
) -> spawn_receipt.SpawnReceipt:
    return spawn_receipt.SpawnReceipt(
        kind="birth",
        nonce=attempt.nonce,
        session=attempt.session,
        home=str(home),
        machine="test",
        cmd_digest=attempt.cmd_digest,
        cwd=attempt.cwd,
        pid=pid,
        create_time=123.5,
        starttime=starttime,
        captured_at=datetime.now(UTC),
    )


def _record_for(
    attempt: SpawnAttempt, receipt: spawn_receipt.SpawnReceipt, prepared: PreparedService
) -> SessionRecord:
    assert (
        receipt.pid is not None and receipt.create_time is not None
    )  # a birth receipt carries identity
    return SessionRecord(
        pid=receipt.pid,
        create_time=receipt.create_time,
        cmd=normal_spawn_command(prepared),
        cwd=str(prepared.cwd),
        started_at=1.0,
        starttime=receipt.starttime,
    )


def publication_state_of(state: object) -> Callable[[object], object]:
    def publication(_conn: object) -> object:
        return state

    return publication


def _pending_state(plan: normal.PreparedNormalRelease) -> Mock:
    pending = Mock()
    pending.operation = plan.context.operation
    pending.challenge = plan.context.challenge.challenge
    state = Mock()
    state.pending = pending
    state.current = None
    return state


class _ChainRig:
    """One chain drive's stubs: database readers, journal spy, effect recorders."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        home: Path,
        plan: normal.PreparedNormalRelease,
        *,
        selector: SelectorReadback,
        readbacks: dict[str, NormalServiceReadback],
        db_readbacks: tuple[UnitActivationReadback, ...] = (),
        publication: object = None,
    ) -> None:
        self.home = home
        self.plan = plan
        self.selector = selector
        self.readbacks = readbacks
        self.db_readbacks = db_readbacks
        self.publication = publication if publication is not None else _pending_state(plan)
        self.calls: list[str] = []
        self.stages: list[str] = []
        self.starts: list[tuple[str, str | None, bool]] = []
        self.attempts: list[tuple[str, UUID]] = []
        self.adopted: list[tuple[str, SessionRecord]] = []
        self.landed: list[UnitActivationReadback] = []
        real_write = normal._write_normal_journal

        def spy_write(
            generation: str, journal: NormalReleaseRecoveryJournal
        ) -> NormalReleaseRecoveryJournal:
            self.stages.append(journal.stage)
            return real_write(generation, journal)

        def publication_state(_conn: object) -> object:
            return self.publication

        monkeypatch.setattr(normal, "_write_normal_journal", spy_write)
        monkeypatch.setattr(normal.psycopg, "connect", _stub_connect)
        monkeypatch.setattr(normal, "pending_transaction", _stub_transaction)
        monkeypatch.setattr(normal, "_locked_publication", publication_state)
        monkeypatch.setattr(normal, "record_pending_migration", self._migration)
        monkeypatch.setattr(normal, "select_pending_release", self._select)
        monkeypatch.setattr(normal, "record_pending_unit_readback", self._land)
        monkeypatch.setattr(normal, "read_pending_unit_readbacks", self._read)
        monkeypatch.setattr(normal, "start_normal_service", self._start)
        monkeypatch.setattr(normal, "await_normal_service_ready", self._ready)
        monkeypatch.setattr(normal, "_stop_bootstrap_checked", self._stop)

    def _migration(self, _conn: object, _operation: object, _challenge: object) -> None:
        self.calls.append("migration-receipt")

    def _select(
        self, _conn: object, _context: object, _unit: object, _previous: object
    ) -> SelectorReadback:
        self.calls.append("selector-cas")
        return self.selector

    def _stop(self, _plan: object) -> None:
        self.calls.append("stop-bootstrap")

    def _read(
        self, _conn: object, _operation: object, _challenge: object
    ) -> tuple[UnitActivationReadback, ...]:
        self.calls.append("read-readbacks")
        return self.db_readbacks

    def _land(
        self,
        _conn: object,
        _operation: object,
        _challenge: object,
        readback: UnitActivationReadback,
    ) -> None:
        self.calls.append("unit-readback")
        self.landed.append(readback)

    def _start(
        self,
        _conn: object,
        _context: object,
        _selector: object,
        prepared: PreparedService,
        *,
        attempt: SpawnAttempt,
        generation: str,
    ) -> NormalServiceReadback:
        self.calls.append("start:" + prepared.identity.session)
        current = _journal(self.home)
        assert current is not None and current.starting_attempt is not None
        self.starts.append(
            (
                prepared.identity.session,
                current.replaces,
                current.starting_attempt.nonce == attempt.nonce,
            )
        )
        self.attempts.append((prepared.identity.session, attempt.nonce))
        return self.readbacks[prepared.identity.session]

    def _ready(
        self,
        _conn: object,
        _context: object,
        _selector: object,
        prepared: PreparedService,
        record: SessionRecord,
    ) -> NormalServiceReadback:
        self.calls.append("ready:" + prepared.identity.session)
        self.adopted.append((prepared.identity.session, record))
        return self.readbacks[prepared.identity.session]

    def drive(self) -> UnitActivationReadback:
        return normal._drive_checked_normal_release(self.plan, GENERATION)


def _two_service_setup(
    home: Path,
) -> tuple[normal.PreparedNormalRelease, dict[str, NormalServiceReadback], SelectorReadback]:
    services = (_prepared_service(home, "ava-ops"), _prepared_service(home, "ava-frontend"))
    plan = _prepared_plan(home, services)
    challenge = plan.context.challenge.challenge
    observed_at = datetime.now(UTC)
    readbacks = {
        "ava-ops": _service_readback("ava-ops", challenge, observed_at),
        "ava-frontend": _service_readback("ava-frontend", challenge, observed_at),
    }
    selector = _selector_readback(plan.request.unit, challenge, observed_at)
    return plan, readbacks, selector


# --- checked chain: fresh pass, re-entry and refusal ---------------------------


def test_checked_chain_runs_every_stage_and_lands_the_readback(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    _seed_environment(unit_home)
    plan, readbacks, selector = _two_service_setup(unit_home)
    rig = _ChainRig(monkeypatch, unit_home, plan, selector=selector, readbacks=readbacks)

    result = rig.drive()

    assert rig.calls == [
        "migration-receipt",
        "selector-cas",
        "selector-cas",
        "stop-bootstrap",
        "selector-cas",
        "start:ava-ops",
        "start:ava-frontend",
        "read-readbacks",
        "unit-readback",
    ]
    assert rig.stages == [
        "waiting",
        "selected",
        "bootstrap_stopped",
        "starting",
        "starting",
        "observed",
    ]
    assert rig.starts == [("ava-ops", None, True), ("ava-frontend", "spawned_alive", True)]
    expected = UnitActivationReadback(
        selector=selector, services=(readbacks["ava-frontend"], readbacks["ava-ops"])
    )
    assert rig.landed == [expected]
    assert result == expected
    journal = _journal(unit_home)
    assert journal is not None and journal.stage == "observed" and journal.readback == expected


def test_checked_chain_converges_on_reentry_without_extra_effects(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    _seed_environment(unit_home)
    plan, readbacks, selector = _two_service_setup(unit_home)
    first = _ChainRig(monkeypatch, unit_home, plan, selector=selector, readbacks=readbacks)
    expected = first.drive()

    retained = _journal(unit_home)
    assert retained is not None and retained.readback == expected
    second = _ChainRig(
        monkeypatch,
        unit_home,
        plan,
        selector=selector,
        readbacks=readbacks,
        db_readbacks=(expected,),
    )
    assert second.drive() == expected
    assert second.calls == ["read-readbacks"]
    assert second.stages == []
    assert second.landed == []
    assert _journal(unit_home) == retained


@pytest.mark.parametrize(
    "stage", [cast("NormalStage", "selected"), cast("NormalStage", "bootstrap_stopped")]
)
def test_checked_chain_resumes_from_a_retained_stage(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path, stage: NormalStage
) -> None:
    plan, readbacks, selector = _two_service_setup(unit_home)
    journal = _journal_for(plan, stage)
    _seed_environment(unit_home, normal_release=journal.model_dump(mode="json"))
    rig = _ChainRig(monkeypatch, unit_home, plan, selector=selector, readbacks=readbacks)

    result = rig.drive()

    assert "migration-receipt" not in rig.calls
    if stage == "selected":
        assert rig.calls[:3] == ["selector-cas", "stop-bootstrap", "selector-cas"]
    else:
        assert rig.calls[0] == "selector-cas"
        assert "stop-bootstrap" not in rig.calls
    assert rig.landed == [result]
    assert result.services == (readbacks["ava-frontend"], readbacks["ava-ops"])


def test_checked_chain_reenters_from_waiting_behind_a_written_selector(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    """INJ-3 window: the selector CAS landed but the journal still reads waiting.

    Re-entry re-proves the selector idempotently (the CAS replay) and still
    converges to the same readback without duplicate effects.
    """
    plan, readbacks, selector = _two_service_setup(unit_home)
    journal = _journal_for(plan, "waiting")
    _seed_environment(unit_home, normal_release=journal.model_dump(mode="json"))
    rig = _ChainRig(monkeypatch, unit_home, plan, selector=selector, readbacks=readbacks)

    result = rig.drive()

    assert rig.calls[0] == "migration-receipt"
    assert "selector-cas" in rig.calls
    assert "stop-bootstrap" in rig.calls
    assert result.services == (readbacks["ava-frontend"], readbacks["ava-ops"])
    assert rig.landed == [result]


def test_checked_chain_requires_the_unit_session_namespace(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    _seed_environment(unit_home)
    plan, readbacks, selector = _two_service_setup(unit_home)
    monkeypatch.setattr(settings.general, "ava_home", unit_home / "elsewhere")
    _ChainRig(monkeypatch, unit_home, plan, selector=selector, readbacks=readbacks)
    with pytest.raises(ReleaseRejectedError, match="namespace"):
        normal._drive_checked_normal_release(plan, GENERATION)
    assert _journal(unit_home) is None


def test_checked_chain_refuses_a_journal_for_another_plan(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    plan, readbacks, selector = _two_service_setup(unit_home)
    foreign = _journal_for(plan, "waiting").model_copy(
        update={"request_path": str(unit_home / "another-request.json")}
    )
    _seed_environment(unit_home, normal_release=foreign.model_dump(mode="json"))
    rig = _ChainRig(monkeypatch, unit_home, plan, selector=selector, readbacks=readbacks)
    with pytest.raises(ReleaseRejectedError, match="differs from its prepared plan"):
        rig.drive()
    assert rig.stages == []


def test_checked_chain_refuses_before_the_database_without_a_budget(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    _seed_environment(unit_home)
    context = _context(unit_home, uuid4(), datetime.now(UTC) + timedelta(seconds=1))
    plan = _prepared_plan(unit_home, (), context=context)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("an expired budget must refuse before any connection")

    monkeypatch.setattr(normal.psycopg, "connect", forbidden)
    with pytest.raises(ReleaseRejectedError, match="connection budget"):
        normal._drive_checked_normal_release(plan, GENERATION)


# --- checked chain: I8 slot adjudication --------------------------------------


def test_ambiguous_slot_attempt_refuses_with_zero_journal_writes(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    plan, readbacks, selector = _two_service_setup(unit_home)
    attempt = _attempt_for(unit_home, plan.services[0])
    journal = _journal_for(plan, "starting", starting_session="ava-ops", starting_attempt=attempt)
    _seed_environment(unit_home, normal_release=journal.model_dump(mode="json"))
    monkeypatch.setattr(
        spawn_receipt,
        "await_birth",
        _await_birth_stub(spawn_receipt.SpawnOutcome("ambiguous", None, "gate is held")),
    )
    rig = _ChainRig(monkeypatch, unit_home, plan, selector=selector, readbacks=readbacks)

    with pytest.raises(ReleaseRejectedError, match="ambiguous"):
        rig.drive()
    assert rig.stages == []
    assert rig.starts == []


def test_dead_slot_attempt_is_displaced_with_its_verdict(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    plan, readbacks, selector = _two_service_setup(unit_home)
    attempt = _attempt_for(unit_home, plan.services[0])
    journal = _journal_for(plan, "starting", starting_session="ava-ops", starting_attempt=attempt)
    _seed_environment(unit_home, normal_release=journal.model_dump(mode="json"))
    receipt = _birth_receipt(unit_home, attempt)
    monkeypatch.setattr(
        spawn_receipt,
        "await_birth",
        _await_birth_stub(spawn_receipt.SpawnOutcome("spawned_dead", receipt, "exited")),
    )
    monkeypatch.setattr(spawn_receipt, "read_session_record", _no_record)
    rig = _ChainRig(monkeypatch, unit_home, plan, selector=selector, readbacks=readbacks)

    result = rig.drive()

    assert rig.starts[0] == ("ava-ops", "spawned_dead", True)
    assert rig.attempts[0][1] != attempt.nonce
    assert result.services == (readbacks["ava-frontend"], readbacks["ava-ops"])


def test_a_dead_slot_after_another_spawn_still_gets_its_fresh_attempt(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    plan, readbacks, selector = _two_service_setup(unit_home)
    slot_prepared = plan.services[1]
    attempt = _attempt_for(unit_home, slot_prepared)
    journal = _journal_for(
        plan, "starting", starting_session="ava-frontend", starting_attempt=attempt
    )
    _seed_environment(unit_home, normal_release=journal.model_dump(mode="json"))
    monkeypatch.setattr(
        spawn_receipt,
        "await_birth",
        _await_birth_stub(
            spawn_receipt.SpawnOutcome("spawned_dead", _birth_receipt(unit_home, attempt), "exited")
        ),
    )
    monkeypatch.setattr(spawn_receipt, "read_session_record", _no_record)
    rig = _ChainRig(monkeypatch, unit_home, plan, selector=selector, readbacks=readbacks)

    result = rig.drive()

    # The adopted-verdict path must not leak onto the slot's own service: the
    # dead attempt is displaced by a fresh one after the earlier service spawns.
    assert rig.starts == [
        ("ava-ops", "spawned_dead", True),
        ("ava-frontend", "spawned_alive", True),
    ]
    assert rig.attempts[1][1] != attempt.nonce
    assert result.services == (readbacks["ava-frontend"], readbacks["ava-ops"])


def test_alive_slot_attempt_is_adopted_and_only_others_spawn(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    plan, readbacks, selector = _two_service_setup(unit_home)
    prepared = plan.services[0]
    attempt = _attempt_for(unit_home, prepared)
    journal = _journal_for(plan, "starting", starting_session="ava-ops", starting_attempt=attempt)
    _seed_environment(unit_home, normal_release=journal.model_dump(mode="json"))
    receipt = _birth_receipt(unit_home, attempt)
    record = _record_for(attempt, receipt, prepared)

    def record_for_session(_home: Path, session: str) -> SessionRecord | None:
        return record if session == "ava-ops" else None

    monkeypatch.setattr(
        spawn_receipt,
        "await_birth",
        _await_birth_stub(spawn_receipt.SpawnOutcome("spawned_alive", receipt, "alive")),
    )
    monkeypatch.setattr(spawn_receipt, "read_session_record", record_for_session)
    rig = _ChainRig(monkeypatch, unit_home, plan, selector=selector, readbacks=readbacks)

    result = rig.drive()

    assert rig.adopted[0] == ("ava-ops", record)
    assert rig.starts == [("ava-frontend", "spawned_alive", True)]
    assert rig.stages == ["starting", "observed"]
    assert result.services == (readbacks["ava-frontend"], readbacks["ava-ops"])


def test_a_live_recorded_service_is_re_observed_not_respawned(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    _seed_environment(unit_home)
    plan, readbacks, selector = _two_service_setup(unit_home)
    ops = plan.services[0]
    live_record = SessionRecord(
        pid=777,
        create_time=3.0,
        cmd=normal_spawn_command(ops),
        cwd=str(ops.cwd),
        started_at=1.0,
        starttime=88,
    )

    def read_record(_home: Path, session: str) -> SessionRecord | None:
        return live_record if session == "ava-ops" else None

    monkeypatch.setattr(spawn_receipt, "read_session_record", read_record)
    monkeypatch.setattr(normal, "observe_process", _observe_as("alive"))
    rig = _ChainRig(monkeypatch, unit_home, plan, selector=selector, readbacks=readbacks)

    result = rig.drive()

    assert rig.adopted == [("ava-ops", live_record)]
    assert rig.starts == [("ava-frontend", None, True)]
    assert result.services == (readbacks["ava-frontend"], readbacks["ava-ops"])


def test_alive_slot_attempt_repairs_a_missing_record_from_the_receipt(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    plan, readbacks, selector = _two_service_setup(unit_home)
    prepared = plan.services[0]
    attempt = _attempt_for(unit_home, prepared)
    journal = _journal_for(plan, "starting", starting_session="ava-ops", starting_attempt=attempt)
    _seed_environment(unit_home, normal_release=journal.model_dump(mode="json"))
    receipt = _birth_receipt(unit_home, attempt)
    record = _record_for(attempt, receipt, prepared)
    repaired: list[tuple[object, ...]] = []

    def repair(
        home: Path,
        session: str,
        birth: spawn_receipt.SpawnReceipt,
        *,
        command: str,
        cwd: Path,
        generation: str,
    ) -> SessionRecord:
        repaired.append((home, session, birth, command, cwd, generation))
        return record

    monkeypatch.setattr(
        spawn_receipt,
        "await_birth",
        _await_birth_stub(spawn_receipt.SpawnOutcome("spawned_alive", receipt, "alive")),
    )
    monkeypatch.setattr(spawn_receipt, "read_session_record", _no_record)
    monkeypatch.setattr(spawn_receipt, "write_recovered_record", repair)
    rig = _ChainRig(monkeypatch, unit_home, plan, selector=selector, readbacks=readbacks)

    rig.drive()

    assert repaired == [
        (
            unit_home,
            "ava-ops",
            receipt,
            normal_spawn_command(prepared),
            prepared.cwd,
            GENERATION,
        )
    ]
    assert rig.adopted[0] == ("ava-ops", record)


def test_damaged_record_is_never_missing(monkeypatch: pytest.MonkeyPatch, unit_home: Path) -> None:
    plan, readbacks, selector = _two_service_setup(unit_home)
    prepared = plan.services[0]
    attempt = _attempt_for(unit_home, prepared)
    journal = _journal_for(plan, "starting", starting_session="ava-ops", starting_attempt=attempt)
    _seed_environment(unit_home, normal_release=journal.model_dump(mode="json"))
    receipt = _birth_receipt(unit_home, attempt)
    monkeypatch.setattr(
        spawn_receipt,
        "await_birth",
        _await_birth_stub(spawn_receipt.SpawnOutcome("spawned_alive", receipt, "alive")),
    )

    def damaged(_home: Path, _session: str) -> SessionRecord:
        raise spawn_receipt.SpawnEvidenceInvalidError("record is malformed")

    monkeypatch.setattr(spawn_receipt, "read_session_record", damaged)
    rig = _ChainRig(monkeypatch, unit_home, plan, selector=selector, readbacks=readbacks)

    with pytest.raises(ReleaseRejectedError, match="failed closed"):
        rig.drive()
    assert rig.stages == []
    assert rig.starts == []


def test_replacing_a_starting_slot_requires_a_fresh_nonce_and_its_witness(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    plan, _readbacks, _selector = _two_service_setup(unit_home)
    prepared = plan.services[0]
    journal = _journal_for(plan, "bootstrap_stopped")
    _seed_environment(unit_home, normal_release=journal.model_dump(mode="json"))

    # The retained slot is empty: a first start may not name a displaced verdict.
    with pytest.raises(ReleaseRejectedError, match="displaces no attempt verdict"):
        normal._write_starting(
            GENERATION, journal, _attempt_for(unit_home, prepared), "spawned_alive"
        )
    # A legal first start, then the same nonce again: a replacement needs a new one.
    first = _attempt_for(unit_home, prepared)
    started = normal._write_starting(GENERATION, journal, first, None)
    assert started.starting_attempt is not None
    with pytest.raises(ReleaseRejectedError, match="fresh attempt nonce"):
        normal._write_starting(
            GENERATION,
            started,
            _attempt_for(unit_home, prepared, nonce=first.nonce),
            "spawned_alive",
        )


# --- checked chain: stop stage -------------------------------------------------


def test_stop_bootstrap_signals_only_the_exact_retained_identity(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    plan = _prepared_plan(unit_home, ())
    record = plan.bootstrap
    _write_ops_record(unit_home, record)
    calls: list[object] = []
    backend = Mock()

    def signal(name: str, *, expected: SessionRecord) -> bool:
        calls.append(("signal", name, expected))
        return True

    def validate(_context: object, _projection: object) -> None:
        calls.append("validate")

    def waited(_plan: object, _process: object) -> None:
        calls.append("wait")

    backend.graceful_signal = signal
    monkeypatch.setattr(normal, "observe_process", _observe_as("alive"))
    monkeypatch.setattr(normal, "validate_operation", validate)
    monkeypatch.setattr(normal, "get_backend", lambda: backend)
    monkeypatch.setattr(normal, "_wait_bootstrap_stopped", waited)

    normal._stop_bootstrap_checked(plan)

    assert calls == ["validate", ("signal", "ava-ops", record), "wait"]


@pytest.mark.parametrize("verdict", ["exited", "identity_mismatch"])
def test_stop_bootstrap_is_complete_without_a_signal_when_the_process_is_gone(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path, verdict: ProcessVerdict
) -> None:
    plan = _prepared_plan(unit_home, ())
    _write_ops_record(unit_home, plan.bootstrap)
    monkeypatch.setattr(normal, "observe_process", _observe_as(verdict))

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a gone process takes no signal and no validation")

    monkeypatch.setattr(normal, "validate_operation", forbidden)
    monkeypatch.setattr(normal, "get_backend", forbidden)
    normal._stop_bootstrap_checked(plan)


def test_stop_bootstrap_refuses_unknown_or_changed_identities(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    plan = _prepared_plan(unit_home, ())
    with pytest.raises(ReleaseRejectedError, match="no ava-ops session record"):
        normal._stop_bootstrap_checked(plan)
    _write_ops_record(unit_home, plan.bootstrap)
    monkeypatch.setattr(normal, "observe_process", _observe_as("unknown"))
    with pytest.raises(ReleaseRejectedError, match="unknown bootstrap identity"):
        normal._stop_bootstrap_checked(plan)
    changed = replace(plan.bootstrap, pid=1717)
    _write_ops_record(unit_home, changed)
    with pytest.raises(ReleaseRejectedError, match="no longer identifies"):
        normal._stop_bootstrap_checked(plan)


def test_wait_bootstrap_stopped_accepts_exit_and_rejects_unknown(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    plan = _prepared_plan(unit_home, ())
    process = ExpectedProcess(pid=4242, create_time=2.0)
    kinds: Iterator[ProcessVerdict] = iter(("alive", "exited"))

    def next_kind(_process: ExpectedProcess) -> ProcessVerdict:
        return next(kinds)

    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(normal, "observe_process", next_kind)
    monkeypatch.setattr(normal.time, "sleep", no_sleep)
    normal._wait_bootstrap_stopped(plan, process)

    kinds = iter(("alive", "identity_mismatch"))
    normal._wait_bootstrap_stopped(plan, process)

    monkeypatch.setattr(normal, "observe_process", _observe_as("unknown"))
    with pytest.raises(ReleaseRejectedError, match="unknown identity"):
        normal._wait_bootstrap_stopped(plan, process)


# --- checked chain: readback landing and commit seat ---------------------------


def _observed_journal(home: Path) -> tuple[normal.PreparedNormalRelease, UnitActivationReadback]:
    plan = _prepared_plan(home, ())
    challenge = plan.context.challenge.challenge
    observed_at = datetime.now(UTC)
    selector = _selector_readback(plan.request.unit, challenge, observed_at)
    readback = UnitActivationReadback(
        selector=selector, services=(_service_readback("ava-ops", challenge, observed_at),)
    )
    journal = _journal_for(plan, "observed", readback=readback)
    _seed_environment(home, normal_release=journal.model_dump(mode="json"))
    return plan, readback


def test_land_readback_follows_the_database_first_rule(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    plan, readback = _observed_journal(unit_home)
    monkeypatch.setattr(normal, "pending_transaction", _stub_transaction)
    reads: list[tuple[UnitActivationReadback, ...]] = [()]
    recorded: list[UnitActivationReadback] = []

    def read_readbacks(*_args: object) -> tuple[UnitActivationReadback, ...]:
        return reads[0]

    def record_readback(
        _conn: object, _operation: object, _challenge: object, item: UnitActivationReadback
    ) -> None:
        recorded.append(item)

    def publication_state(_conn: object) -> object:
        return _pending_state(plan)

    monkeypatch.setattr(normal, "read_pending_unit_readbacks", read_readbacks)
    monkeypatch.setattr(normal, "record_pending_unit_readback", record_readback)
    monkeypatch.setattr(normal, "_locked_publication", publication_state)
    journal = _journal(unit_home)
    assert journal is not None

    assert normal._land_readback(_stub_connection(), plan, journal) == readback
    assert recorded == [readback]

    reads[0] = (readback,)
    assert normal._land_readback(_stub_connection(), plan, journal) == readback
    assert recorded == [readback]

    altered = readback.model_copy(
        update={"selector": readback.selector.model_copy(update={"current_digest": "9" * 64})}
    )
    reads[0] = (altered,)
    with pytest.raises(ReleaseRejectedError, match="database readback differs"):
        normal._land_readback(_stub_connection(), plan, journal)


def test_land_readback_ends_when_the_publication_already_consumed_it(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    plan, readback = _observed_journal(unit_home)
    state = Mock()
    state.pending = None
    state.current = Mock(
        operation=plan.context.operation, activation_challenge=plan.context.challenge.challenge
    )
    monkeypatch.setattr(normal, "pending_transaction", _stub_transaction)
    monkeypatch.setattr(normal, "_locked_publication", publication_state_of(state))

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a consumed publication has no pending readbacks to land")

    monkeypatch.setattr(normal, "read_pending_unit_readbacks", forbidden)
    monkeypatch.setattr(normal, "record_pending_unit_readback", forbidden)
    journal = _journal(unit_home)
    assert journal is not None
    assert normal._land_readback(_stub_connection(), plan, journal) == readback

    state.current = None
    with pytest.raises(ReleaseRejectedError, match="no matching publication"):
        normal._land_readback(_stub_connection(), plan, journal)


def test_commit_seat_requires_the_exact_current_publication(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    plan, readback = _observed_journal(unit_home)
    state = Mock()
    state.pending = None
    state.current = Mock(
        operation=plan.context.operation,
        activation_challenge=uuid4(),
        units=(plan.request.unit,),
    )
    monkeypatch.setattr(normal.psycopg, "connect", _stub_connect)
    monkeypatch.setattr(normal, "pending_transaction", _stub_transaction)
    monkeypatch.setattr(normal, "_locked_publication", publication_state_of(state))

    with pytest.raises(ReleaseRejectedError, match="current publication"):
        normal.commit_normal_release_after_publication(plan, GENERATION)
    state.pending = Mock()
    state.current = Mock(
        operation=plan.context.operation,
        activation_challenge=plan.context.challenge.challenge,
        units=(plan.request.unit,),
    )
    with pytest.raises(ReleaseRejectedError, match="current publication"):
        normal.commit_normal_release_after_publication(plan, GENERATION)

    state.pending = None
    state.current = Mock(
        operation=plan.context.operation,
        activation_challenge=plan.context.challenge.challenge,
        units=(plan.request.unit,),
    )
    assert normal.commit_normal_release_after_publication(plan, GENERATION) == readback
    journal = _journal(unit_home)
    assert journal is not None and journal.stage == "committed"
    assert normal.commit_normal_release_after_publication(plan, GENERATION) == readback


# --- start_normal_service: attempt binding, gated spawn, adoption --------------


def _start_environment(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> tuple[normal.PreparedNormalRelease, PreparedService, SpawnAttempt, SelectorReadback]:
    prepared = _prepared_service(unit_home, "ava-ops")
    plan = _prepared_plan(unit_home, (prepared,))
    attempt = _attempt_for(unit_home, prepared)
    selector = _selector_readback(
        plan.request.unit, plan.context.challenge.challenge, datetime.now(UTC)
    )

    def no_authority(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(release_services, "pending_transaction", _stub_transaction)
    monkeypatch.setattr(release_services, "require_pending_candidate_start", no_authority)
    return plan, prepared, attempt, selector


def test_start_normal_service_requires_the_bound_attempt(unit_home: Path) -> None:
    prepared = _prepared_service(unit_home, "ava-ops")
    plan = _prepared_plan(unit_home, (prepared,))
    attempt = _attempt_for(unit_home, prepared)
    selector = _selector_readback(
        plan.request.unit, plan.context.challenge.challenge, datetime.now(UTC)
    )
    wrong_session = attempt.model_copy(update={"session": "ava-frontend"})
    with pytest.raises(ReleaseRejectedError, match="bind its prepared service"):
        release_services.start_normal_service(
            _stub_connection(),
            plan.context,
            selector,
            prepared,
            attempt=wrong_session,
            generation=GENERATION,
        )
    wrong_path = attempt.model_copy(
        update={"receipt_path": "run/updater-spawn/x/other.receipt.json"}
    )
    with pytest.raises(ReleaseRejectedError, match="bind its prepared service"):
        release_services.start_normal_service(
            _stub_connection(),
            plan.context,
            selector,
            prepared,
            attempt=wrong_path,
            generation=GENERATION,
        )


@pytest.mark.parametrize(
    "error",
    [
        spawn_receipt.SpawnRefusedError,
        spawn_receipt.SpawnNotCompletedError,
        spawn_receipt.SpawnExitedError,
        spawn_receipt.SpawnAmbiguousError,
    ],
)
def test_start_normal_service_maps_every_gated_spawn_outcome(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path, error: type[Exception]
) -> None:
    plan, prepared, attempt, selector = _start_environment(monkeypatch, unit_home)

    def only_free(_name: str) -> bool:
        return False

    def refused(*_args: object, **_kwargs: object) -> spawn_receipt.SpawnReceipt:
        raise error("adjudicated")

    monkeypatch.setattr(release_services, "get_backend", lambda: Mock(has_session=only_free))
    monkeypatch.setattr(release_services.spawn_receipt, "execute_gated_spawn", refused)
    with pytest.raises(ReleaseRejectedError, match="failed closed"):
        release_services.start_normal_service(
            _stub_connection(),
            plan.context,
            selector,
            prepared,
            attempt=attempt,
            generation=GENERATION,
        )


def test_start_normal_service_refuses_an_existing_session(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    plan, prepared, attempt, selector = _start_environment(monkeypatch, unit_home)

    def all_taken(_name: str) -> bool:
        return True

    monkeypatch.setattr(release_services, "get_backend", lambda: Mock(has_session=all_taken))
    with pytest.raises(ReleaseRejectedError, match="existing or unaccounted"):
        release_services.start_normal_service(
            _stub_connection(),
            plan.context,
            selector,
            prepared,
            attempt=attempt,
            generation=GENERATION,
        )


def test_start_normal_service_repairs_a_missing_record_and_awaits_readiness(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    plan, prepared, attempt, selector = _start_environment(monkeypatch, unit_home)
    receipt = _birth_receipt(unit_home, attempt)
    record = _record_for(attempt, receipt, prepared)
    observed_at = datetime.now(UTC)
    readback = _service_readback("ava-ops", plan.context.challenge.challenge, observed_at)
    spawn_calls: list[dict[str, object]] = []
    budgets: list[float] = []

    def gated(*_args: object, **kwargs: object) -> spawn_receipt.SpawnReceipt:
        spawn_calls.append(kwargs)
        budgets.append(cast("float", kwargs["wait_budget"]))
        return receipt

    def only_free(_name: str) -> bool:
        return False

    def no_record(*_args: object) -> SessionRecord | None:
        return None

    repaired: list[tuple[object, ...]] = []

    def repair(
        home: Path,
        session: str,
        birth: spawn_receipt.SpawnReceipt,
        *,
        command: str,
        cwd: Path,
        generation: str,
    ) -> SessionRecord:
        repaired.append((home, session, birth, command, cwd, generation))
        return record

    def ready(*_args: object, **_kwargs: object) -> NormalServiceReadback:
        return readback

    monkeypatch.setattr(release_services, "get_backend", lambda: Mock(has_session=only_free))
    monkeypatch.setattr(release_services.spawn_receipt, "execute_gated_spawn", gated)
    monkeypatch.setattr(release_services.spawn_receipt, "read_session_record", no_record)
    monkeypatch.setattr(release_services.spawn_receipt, "write_recovered_record", repair)
    monkeypatch.setattr(release_services, "await_normal_service_ready", ready)

    result = release_services.start_normal_service(
        _stub_connection(),
        plan.context,
        selector,
        prepared,
        attempt=attempt,
        generation=GENERATION,
    )

    assert result == readback
    assert budgets and 0 < budgets[0] <= settings.gateway.update_spawn_ambiguity_wait_seconds
    assert spawn_calls == [
        {
            "name": "ava-ops",
            "command": normal_spawn_command(prepared),
            "workdir": prepared.cwd,
            "env": prepared.environment,
            "home": unit_home,
            "generation": GENERATION,
            "machine": "test",
            "nonce": attempt.nonce,
            "wait_budget": budgets[0],
        }
    ]
    assert repaired == [
        (
            unit_home,
            "ava-ops",
            receipt,
            normal_spawn_command(prepared),
            prepared.cwd,
            GENERATION,
        )
    ]


def test_start_normal_service_refuses_a_mismatched_record(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    plan, prepared, attempt, selector = _start_environment(monkeypatch, unit_home)
    receipt = _birth_receipt(unit_home, attempt)
    stranger = replace(_record_for(attempt, receipt, prepared), pid=6060)

    def only_free(_name: str) -> bool:
        return False

    def gated(*_args: object, **_kwargs: object) -> spawn_receipt.SpawnReceipt:
        return receipt

    def stranger_record(*_args: object) -> SessionRecord:
        return stranger

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a mismatched record must never reach readiness")

    monkeypatch.setattr(release_services, "get_backend", lambda: Mock(has_session=only_free))
    monkeypatch.setattr(release_services.spawn_receipt, "execute_gated_spawn", gated)
    monkeypatch.setattr(release_services.spawn_receipt, "read_session_record", stranger_record)
    monkeypatch.setattr(release_services, "await_normal_service_ready", forbidden)
    with pytest.raises(ReleaseRejectedError, match="does not identify the birth"):
        release_services.start_normal_service(
            _stub_connection(),
            plan.context,
            selector,
            prepared,
            attempt=attempt,
            generation=GENERATION,
        )


def test_ready_loop_retries_transient_observation_failures(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    plan, prepared, attempt, selector = _start_environment(monkeypatch, unit_home)
    observed_at = datetime.now(UTC)
    readback = _service_readback("ava-ops", plan.context.challenge.challenge, observed_at)
    record = _record_for(attempt, _birth_receipt(unit_home, attempt), prepared)
    attempts: list[str] = []

    def observe(*_args: object) -> NormalServiceReadback:
        attempts.append("observe")
        if len(attempts) == 1:
            raise ReleaseRejectedError("not ready yet")
        return readback

    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(release_services, "observe_normal_service", observe)
    monkeypatch.setattr(release_services, "observe_process", _observe_as("alive"))
    monkeypatch.setattr(release_services.time, "sleep", no_sleep)
    result = release_services.await_normal_service_ready(
        _stub_connection(), plan.context, selector, prepared, record
    )
    assert result == readback
    assert attempts == ["observe", "observe"]

    def still_not_ready(*_args: object) -> NormalServiceReadback:
        raise ReleaseRejectedError("not ready yet")

    monkeypatch.setattr(release_services, "observe_normal_service", still_not_ready)
    monkeypatch.setattr(release_services, "observe_process", _observe_as("exited"))
    with pytest.raises(ReleaseRejectedError, match="exited before readiness"):
        release_services.await_normal_service_ready(
            _stub_connection(), plan.context, selector, prepared, record
        )


# --- continuation entry: identity rebinding -----------------------------------


def test_continuation_rebinds_the_current_ops_record_before_execute(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    request_path = unit_home / "bootstrap.json"
    request_path.write_bytes(b"{}")
    inventory = unit_home / "inventory.json"
    inventory.write_bytes(b"inventory")
    candidate = unit_home / "candidate.json"
    candidate.write_bytes(b"candidate")
    recovery = unit_home / "recovery.json"
    recovery.write_bytes(b"recovery")
    _seed_environment(
        unit_home,
        request=str(request_path),
        request_digest=hashlib.sha256(b"{}").hexdigest(),
        inventory_digest=hashlib.sha256(b"inventory").hexdigest(),
        candidate_context_digest=hashlib.sha256(b"candidate").hexdigest(),
        recovery_context_digest=hashlib.sha256(b"recovery").hexdigest(),
    )
    hop = Mock()
    hop.request_path = request_path
    hop.request = Mock()
    hop.request.inventory_receipt = str(inventory)
    hop.request.candidate_context = str(candidate)
    hop.request.recovery_context = str(recovery)
    hop.request.normal_release_path = str(unit_home / "normal-request.json")
    plan = _prepared_plan(unit_home, ())
    current = replace(plan.bootstrap, pid=5150)
    _write_ops_record(unit_home, current)
    captured: list[normal.PreparedNormalRelease] = []

    def capture(routed: normal.PreparedNormalRelease, _generation: str) -> None:
        captured.append(routed)

    monkeypatch.setattr(normal, "execute_normal_release", capture)

    normal.continue_after_bootstrap(hop, plan, GENERATION)

    assert len(captured) == 1
    assert captured[0].bootstrap == current
    assert captured[0].bootstrap != plan.bootstrap


def test_continuation_resumes_a_retained_normal_journal(
    monkeypatch: pytest.MonkeyPatch, unit_home: Path
) -> None:
    request_path = unit_home / "bootstrap.json"
    request_path.write_bytes(b"{}")
    inventory = unit_home / "inventory.json"
    inventory.write_bytes(b"inventory")
    candidate = unit_home / "candidate.json"
    candidate.write_bytes(b"candidate")
    recovery = unit_home / "recovery.json"
    recovery.write_bytes(b"recovery")
    plan = _prepared_plan(unit_home, ())
    retained = _journal_for(plan, "selected")
    _seed_environment(
        unit_home,
        request=str(request_path),
        request_digest=hashlib.sha256(b"{}").hexdigest(),
        inventory_digest=hashlib.sha256(b"inventory").hexdigest(),
        candidate_context_digest=hashlib.sha256(b"candidate").hexdigest(),
        recovery_context_digest=hashlib.sha256(b"recovery").hexdigest(),
        normal_release=retained.model_dump(mode="json"),
    )
    hop = Mock()
    hop.request_path = request_path
    hop.request = Mock()
    hop.request.inventory_receipt = str(inventory)
    hop.request.candidate_context = str(candidate)
    hop.request.recovery_context = str(recovery)
    hop.request.normal_release_path = str(plan.request_path)
    _write_ops_record(unit_home, plan.bootstrap)
    captured: list[str] = []

    def capture(_plan: normal.PreparedNormalRelease, _generation: str) -> None:
        captured.append("execute")

    monkeypatch.setattr(normal, "execute_normal_release", capture)

    normal.continue_after_bootstrap(hop, plan, GENERATION)

    assert captured == ["execute"]
