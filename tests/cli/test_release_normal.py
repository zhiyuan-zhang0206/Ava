"""Normal updater producer contracts; actual cold launch remains a CI gate."""

import hashlib
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest

from cli.commands import _update_normal_release as normal
from cli.commands._release_selector import read_selector, selector_bytes
from cli.commands._release_services import _command
from ops.spec import ServiceSpec
from shared.managed_writer_activation import UnitActivationReadback
from shared.managed_writer_observation import ExpectedProcess
from shared.managed_writer_publication import (
    CandidateUnitPlan,
    NormalService,
    NormalServiceReadback,
    PublishedUnit,
    SelectorReadback,
)
from shared.runtime_publication_input import read_publication_selector
from shared.runtime_release import ReleaseRejectedError, VerifiedRelease


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


def test_normal_activation_is_disabled_before_any_recovery_write() -> None:
    from unittest.mock import Mock

    from cli.commands import _update_normal_release as normal

    with pytest.raises(ReleaseRejectedError, match="checked crash recovery"):
        normal.execute_normal_release(Mock(spec=normal.PreparedNormalRelease), "generation")


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

    monkeypatch.setattr(normal, "probe_bootstrap", forbidden)
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


# --- P3/P4 effect seat: wiring, order and the gate-before-seat posture ---------


class _StubConnection:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_args: object) -> None:
        return None


def _stub_connect(*_args: object, **_kwargs: object) -> _StubConnection:
    return _StubConnection()


def _stub_transaction(_conn: object, _context: object) -> AbstractContextManager[None]:
    return nullcontext()


def _prepared_plan(services: tuple[object, ...], *, previous: str | None = None) -> Mock:
    plan = Mock(spec=normal.PreparedNormalRelease)
    plan.context = Mock()
    plan.context.operation = Mock()
    plan.context.challenge = Mock()
    plan.context.challenge.challenge = uuid4()
    plan.context.challenge.valid_until = datetime.now(UTC) + timedelta(minutes=10)
    plan.projection = Mock()
    plan.projection.db_url.get_secret_value.return_value = "postgresql://example"
    plan.request = Mock()
    plan.request.previous_selector = previous
    plan.request.unit = Mock()
    plan.services = services
    return plan


def _selector_readback(challenge: UUID, observed_at: datetime) -> SelectorReadback:
    return SelectorReadback(
        unit=PublishedUnit(
            machine="test",
            home="/test",
            prepared_receipt_digest="a" * 64,
            artifact_digest="b" * 64,
            manifest_digest="c" * 64,
            inventory_digest="d" * 64,
        ),
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


@pytest.mark.parametrize("previous", [None, "old-selector-bytes"])
def test_effect_seat_runs_migration_then_selector_then_services_then_readback(
    previous: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    previous_seen: list[bytes | None] = []
    challenge = uuid4()
    observed_at = datetime.now(UTC)
    selector = _selector_readback(challenge, observed_at)
    readbacks = {
        "ava-ops": _service_readback("ava-ops", challenge, observed_at),
        "ava-frontend": _service_readback("ava-frontend", challenge, observed_at),
    }
    services: list[Mock] = []
    for name in ("ava-ops", "ava-frontend"):
        prepared = Mock()
        prepared.name = name
        services.append(prepared)

    def record_migration(_conn: object, _operation: object, _challenge: object) -> None:
        calls.append("migration-receipt")

    def select_release(
        _conn: object, _context: object, _unit: object, previous_bytes: bytes | None
    ) -> SelectorReadback:
        calls.append("selector-cas")
        previous_seen.append(previous_bytes)
        return selector

    def start_service(_conn: object, _context: object, _selector: object, prepared: Mock) -> object:
        calls.append("start:" + prepared.name)
        return readbacks[prepared.name]

    recorded: list[UnitActivationReadback] = []

    def record_unit(
        _conn: object, _operation: object, _challenge: object, readback: UnitActivationReadback
    ) -> None:
        calls.append("unit-readback")
        recorded.append(readback)

    monkeypatch.setattr(normal.psycopg, "connect", _stub_connect)
    monkeypatch.setattr(normal, "pending_transaction", _stub_transaction)
    monkeypatch.setattr(normal, "record_pending_migration", record_migration)
    monkeypatch.setattr(normal, "select_pending_release", select_release)
    monkeypatch.setattr(normal, "start_normal_service", start_service)
    monkeypatch.setattr(normal, "record_pending_unit_readback", record_unit)

    result = normal._run_normal_release_effects(_prepared_plan(tuple(services), previous=previous))

    assert calls == [
        "migration-receipt",
        "selector-cas",
        "start:ava-ops",
        "start:ava-frontend",
        "unit-readback",
    ]
    expected = UnitActivationReadback(
        selector=selector, services=(readbacks["ava-ops"], readbacks["ava-frontend"])
    )
    assert recorded == [expected]
    assert result == expected
    assert previous_seen == [None if previous is None else previous.encode()]


def test_effect_seat_stops_after_a_step_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def record_migration(_conn: object, _operation: object, _challenge: object) -> None:
        calls.append("migration-receipt")

    def select_release(_conn: object, _context: object, _unit: object, _previous: object) -> None:
        raise ReleaseRejectedError("selector authority is absent")

    def start_service(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("no service may start after a step refusal")

    monkeypatch.setattr(normal.psycopg, "connect", _stub_connect)
    monkeypatch.setattr(normal, "pending_transaction", _stub_transaction)
    monkeypatch.setattr(normal, "record_pending_migration", record_migration)
    monkeypatch.setattr(normal, "select_pending_release", select_release)
    monkeypatch.setattr(normal, "start_normal_service", start_service)

    with pytest.raises(ReleaseRejectedError, match="selector authority is absent"):
        normal._run_normal_release_effects(_prepared_plan((Mock(),)))
    assert calls == ["migration-receipt"]


def test_execute_refuses_before_the_effect_seat(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(_plan: object) -> None:
        raise AssertionError("the effect seat must not run while the gate refuses")

    monkeypatch.setattr(normal, "_run_normal_release_effects", forbidden)
    with pytest.raises(ReleaseRejectedError, match="checked crash recovery"):
        normal.execute_normal_release(_prepared_plan(()), "generation")


def test_run_normal_release_routes_the_prepared_plan_into_execute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _prepared_plan(())
    plan.resume_generation = "generation-1"

    def stub_prepare(_path: object) -> Mock:
        return plan

    routed: list[tuple[object, str]] = []

    def spy_execute(routed_plan: object, generation: str) -> None:
        routed.append((routed_plan, generation))

    monkeypatch.setattr(normal, "prepare_normal_release", stub_prepare)
    monkeypatch.setattr(normal, "execute_normal_release", spy_execute)
    normal.run_normal_release(tmp_path / "normal-request.json")
    assert routed == [(plan, "generation-1")]


def test_continuation_routes_validated_identity_into_execute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    request_path = tmp_path / "bootstrap.json"
    request_path.write_bytes(b"{}")
    inventory = tmp_path / "inventory.json"
    inventory.write_bytes(b"inventory")
    candidate = tmp_path / "candidate.json"
    candidate.write_bytes(b"candidate")
    recovery = tmp_path / "recovery.json"
    recovery.write_bytes(b"recovery")

    hop = Mock()
    hop.request_path = request_path
    hop.request = Mock()
    hop.request.inventory_receipt = str(inventory)
    hop.request.candidate_context = str(candidate)
    hop.request.recovery_context = str(recovery)
    hop.request.normal_release_path = str(request_path)
    plan = Mock(spec=normal.PreparedNormalRelease)
    plan.request_path = request_path

    def digest(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    monkeypatch.setattr(
        normal.updater_handoff,
        "read_bootstrap_recovery",
        lambda: {
            "version": 1,
            "generation": "generation",
            "journal": {
                "request": str(request_path),
                "request_digest": digest(b"{}"),
                "inventory_digest": digest(b"inventory"),
                "candidate_context_digest": digest(b"candidate"),
                "recovery_context_digest": digest(b"recovery"),
                "normal_release_planned": True,
                "stage": "candidate_ready",
                "cron": "",
                "phases": [
                    {
                        "stage": "candidate_ready",
                        "observed_at": "2026-09-20T04:00:00+00:00",
                        "monotonic_s": 0.0,
                        "pid": 1,
                        "elapsed_s": None,
                    }
                ],
                "normal_release": None,
            },
        },
    )

    routed: list[str] = []

    def spy_execute(_plan: object, generation: object) -> None:
        routed.append("execute")

    monkeypatch.setattr(normal, "execute_normal_release", spy_execute)
    normal.continue_after_bootstrap(hop, plan, "generation")
    assert routed == ["execute"]
